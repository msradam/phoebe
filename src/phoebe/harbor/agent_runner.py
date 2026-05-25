# /// script
# dependencies = [
#   "apache-burr>=0.42,<0.43",
#   "fastmcp>=3.3,<3.4",
#   "pydantic>=2,<3",
#   "litellm>=1.84,<2",
#   "mcp>=1.9.0",
# ]
# ///
# litellm is >=1.84 (not the 1.83.10 the default o11y agent pins): fastmcp
# requires python-dotenv>=1.1.0, which litellm 1.83.10 hard-pins to 1.0.1.
# 1.84+ relaxes that, letting fastmcp + litellm coexist in one env.
# Note: theodosia's source is vendored into /app/theodosia by the Harbor
# agent's setup() (its apache-burr[tracking] extra pulls psutil, which has
# no wheel for the gcc-less image). /app is on sys.path, so
# `import theodosia` resolves to the vendored copy.
"""phoebe agent runner (single-surface via Theodosia upstream), Harbor container.

The agent sees ONLY the phoebe actions. The query actions drive Grafana
through Theodosia's upstream mechanism: the runner binds an upstream manager
that wraps its Grafana MCP session, so call_upstream("grafana", tool, args)
inside an FSM action reaches Grafana. The Grafana tools are never exposed
to the agent. Every query happens inside an action, so it is a single,
ledger-honest surface that drives reliably even on a 70B model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, "/app")

from theodosia.upstream import bind_upstream, call_upstream  # noqa: E402

from phoebe import build_application  # noqa: E402


def scenario_clock_iso() -> str:
    return os.environ.get("O11Y_SCENARIO_TIME_ISO") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )


def normalize_model_name(model_name: str) -> str:
    if model_name.startswith("google/"):
        return f"gemini/{model_name.split('/', 1)[1]}"
    return model_name


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw": arguments}
    return {"_raw": str(arguments)}


def _extract(res: Any) -> Any:
    if getattr(res, "structured_content", None) is not None:
        return res.structured_content
    if getattr(res, "content", None):
        parts = [getattr(c, "text", "") for c in res.content if getattr(c, "text", "")]
        if parts:
            text = "\n".join(parts)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return None


class _SessionUpstream:
    """Bind the runner's open Grafana MCP session as a Theodosia upstream, so
    the FSM's call_upstream('grafana', tool, args) routes to it."""

    def __init__(self, session: Any):
        self._session = session

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        res = await self._session.call_tool(tool, args or {})
        return _extract(res)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


# Some models (Kimi K2.x) emit tool calls in their native token format inside
# the message *content* instead of the structured tool_calls field when routed
# through an OpenAI-compatible endpoint. Recover those so a parse miss does not
# silently end the run. Format:
#   <|tool_call_begin|>functions.NAME:IDX<|tool_call_argument_begin|>{json}<|tool_call_end|>
_LEAKED_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*(?:functions\.)?([A-Za-z0-9_]+)\s*:?\s*\d*\s*"
    r"<\|tool_call_argument_begin\|>(.*?)<\|tool_call_end\|>",
    re.DOTALL,
)


def extract_tool_calls(msg: Any) -> list[ToolCall]:
    """Normalize a model message into tool calls, from the structured
    tool_calls field when present, else by parsing leaked native-format
    tool-call tokens out of the content."""
    structured = getattr(msg, "tool_calls", None) or []
    calls: list[ToolCall] = []
    for i, tc in enumerate(structured):
        fn = tc.function
        calls.append(
            ToolCall(
                id=getattr(tc, "id", None) or f"call_{i}",
                name=fn.name or "",
                arguments=parse_tool_arguments(fn.arguments),
            )
        )
    if calls:
        return calls
    content = getattr(msg, "content", None) or ""
    for i, m in enumerate(_LEAKED_CALL_RE.finditer(content)):
        calls.append(
            ToolCall(
                id=f"leaked_{i}",
                name=m.group(1),
                arguments=parse_tool_arguments(m.group(2).strip()),
            )
        )
    return calls


def assistant_message(content: str | None, calls: list[ToolCall]) -> dict[str, Any]:
    """Rebuild a well-formed assistant message from normalized calls, so the
    transcript stays valid whether the calls were structured or leaked."""
    text = content or ""
    if "<|tool_call" in text:
        text = _LEAKED_CALL_RE.sub("", text)
        text = re.sub(r"<\|tool_calls?_section_(?:begin|end)\|>", "", text).strip()
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in calls
        ],
    }


# == FSM control tools ===============================================
# These three are the FSM's own actions: open the case, move through the
# phases, and conclude. They are added alongside the real Grafana toolset,
# which the runner discovers at connect time and passes through (see
# build_tools / run_agent). The FSM gates the phases and termination; it does
# not restrict which Grafana tool the agent calls.

FSM_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "start_investigation",
            "description": "Open the investigation. Call this first. The server discovers the Grafana datasources and the telemetry schema and reports them in state.current_prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_description": {"type": "string"},
                    "scenario_time": {"type": "string"},
                },
                "required": ["incident_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_phase",
            "description": "triage -> diagnose -> verify. diagnose needs >=1 finding; verify needs >=2 distinct backends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "enum": ["triage", "diagnose", "verify"]},
                    "rationale": {"type": "string"},
                },
                "required": ["to", "rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conclude",
            "description": "Finish. Requires phase=='verify', >=2 backends, and a verify-phase finding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_service": {"type": "string", "description": "The root-cause service."},
                    "root_cause": {"type": "string"},
                    "final_answer": {
                        "type": "string",
                        "description": (
                            "Markdown response the grader reads. Lead with the conclusion, then "
                            "evidence. Include: a number quantified from a query (count/rate/share, "
                            "not estimated); the blast radius (which services are affected and which "
                            "are not; isolated vs broad); the primary service vs downstream cascade; "
                            "a coarse timestamp; and a representative trace ID if traces were queried."
                        ),
                    },
                    "cascade_services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Downstream services affected as a knock-on, not the root cause.",
                    },
                },
                "required": ["primary_service", "root_cause", "final_answer"],
            },
        },
    },
]
_FSM_ACTION_NAMES = {t["function"]["name"] for t in FSM_TOOLS}


def _valid_next_actions(app: Any) -> list[str]:
    from burr.core.action import Condition

    prior = app.state.get("__PRIOR_STEP")
    if prior is None:
        entry = app.graph.entrypoint
        return [entry.name if hasattr(entry, "name") else str(entry)]
    valid: list[str] = []
    for t in app.graph.transitions:
        if t.from_.name != prior:
            continue
        try:
            if t.condition.run(app.state)[Condition.KEY]:
                valid.append(t.to.name)
        except Exception:
            continue
    return valid


async def step_fsm(app: Any, action: str, inputs: dict[str, Any]) -> dict[str, Any]:
    legal = _valid_next_actions(app)
    if action not in legal:
        return {
            "error": "invalid_transition",
            "requested": action,
            "valid_next_actions": legal,
            "message": f"action {action!r} not reachable now. Reachable: {legal}.",
        }
    target = app.graph.get_action(action)
    if target is None:
        return {"error": "unknown_action", "requested": action, "valid_next_actions": legal}
    original = app.get_next_action
    app.get_next_action = lambda: target  # type: ignore[method-assign]
    try:
        _a, _r, new_state = await app.astep(inputs=inputs)
    except Exception as e:  # noqa: BLE001
        return {"error": "action_error", "requested": action, "error_message": str(e)}
    finally:
        app.get_next_action = original  # type: ignore[method-assign]
    sv = {k: v for k, v in new_state.get_all().items() if not k.startswith("__")}
    findings = sv.get("findings") or []
    last = findings[-1] if findings else None
    return {
        "ok": True,
        "action_executed": action,
        "valid_next_actions": _valid_next_actions(app),
        "state": {
            "phase": sv.get("phase"),
            "distinct_backends": sv.get("distinct_backends"),
            "n_findings": len(findings),
            "current_prompt": sv.get("current_prompt"),
            "final_answer_set": sv.get("final_answer") is not None,
        },
        "result": (last or {}).get("result_summary") if action == "record_probe" else None,
    }


def atif_steps_from_messages(
    messages: list[dict[str, Any]], final_answer: str = ""
) -> list[dict[str, Any]]:
    """Convert the OpenAI-style message list into ATIF steps that o11y-bench's
    transcript parser reads (source / message / tool_calls / observation). Tool
    results attach to the preceding agent step as observations, and the final
    answer is appended as the agent's closing message so the grader sees it."""
    steps: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role in ("user", "system"):
            steps.append({"source": role, "message": m.get("content") or ""})
        elif role == "assistant":
            tool_calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                tool_calls.append(
                    {
                        "tool_call_id": tc.get("id", ""),
                        "function_name": fn.get("name", ""),
                        "arguments": args,
                    }
                )
            steps.append(
                {
                    "source": "agent",
                    "message": (m.get("content") or "") or ("(tool use)" if tool_calls else ""),
                    "tool_calls": tool_calls,
                }
            )
        elif role == "tool" and steps and steps[-1].get("source") == "agent":
            obs = steps[-1].setdefault("observation", {"results": []})
            obs["results"].append(
                {"source_call_id": m.get("tool_call_id", ""), "content": str(m.get("content", ""))}
            )
    if final_answer:
        steps.append({"source": "agent", "message": final_answer})
    return steps


def fsm_terminated(app: Any) -> bool:
    try:
        return app.state.get("final_answer") not in (None, "")
    except Exception:
        return False


MAX_STEPS = 50
MAX_NO_CALL_TURNS = 3
_NUDGE = (
    "You did not call a tool. Do not answer in prose. Advance the investigation by "
    "calling one tool: a Grafana query (Prometheus / Loki / Tempo) or advance_phase, "
    "and once phase=='verify' with findings from >=2 distinct backends, call conclude(...). "
    "Read state.current_prompt for the valid next actions and the discovered schema."
)


def backend_for(tool: str) -> str | None:
    """Map a Grafana tool name to a telemetry backend for the cross-reference
    gate. Non-telemetry tools (dashboards, annotations, alerting) return None."""
    t = (tool or "").lower()
    if "prometheus" in t:
        return "prometheus"
    if "loki" in t:
        return "loki"
    if "tempo" in t or "trace" in t:
        return "tempo"
    return None


def _result_summary(result: Any, limit: int = 1200) -> str:
    s = result if isinstance(result, str) else json.dumps(result, default=str)
    return s[:limit]


def _case_open(app: Any) -> bool:
    try:
        s = app.state
        return bool(s.get("incident_description")) and s.get("final_answer") in (None, "")
    except Exception:
        return False


async def discover_grafana_tools(session: Any) -> list[dict[str, Any]]:
    """Convert the upstream Grafana MCP tools into OpenAI function defs so the
    agent has the full toolset, the same surface the bench's base agent gets."""
    result = await session.list_tools()
    defs: list[dict[str, Any]] = []
    for t in result.tools:
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (getattr(t, "description", "") or "")[:300],
                    "parameters": schema,
                },
            }
        )
    return defs


async def drive_investigation(
    complete: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    fsm_app: Any,
    messages: list[dict[str, Any]],
    *,
    call_grafana: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    infer_backend: Callable[[str], str | None] = backend_for,
    max_steps: int = MAX_STEPS,
    log: Callable[[str], None] = lambda _s: None,
) -> dict[str, Any]:
    """Drive the FSM by calling ``complete(messages)`` for each turn until the
    FSM terminates or max_steps. ``complete`` returns a model message (with
    ``.content`` and optionally ``.tool_calls``). Pure of any LLM/transport
    dependency so it is testable with a scripted ``complete`` and a mock
    upstream bound on the ContextVar.
    """
    steps_log: list[dict[str, Any]] = []
    total_tool_calls = 0
    step = 0
    consecutive_no_calls = 0
    while step < max_steps:
        step += 1
        msg = await complete(messages)
        calls = extract_tool_calls(msg)
        if not calls:
            content = getattr(msg, "content", "") or ""
            steps_log.append({"step": step, "type": "assistant", "content": content})
            consecutive_no_calls += 1
            # A reasoning model intermittently returns an empty or prose-only turn.
            # Do not treat that as completion: keep any content, nudge it to act,
            # and only give up after several consecutive no-call turns.
            if consecutive_no_calls >= MAX_NO_CALL_TURNS:
                log(f"[{step}] no tool calls {consecutive_no_calls}x; stopping")
                break
            if content:
                messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _NUDGE})
            log(f"[{step}] no tool calls; nudging ({consecutive_no_calls}/{MAX_NO_CALL_TURNS})")
            continue
        consecutive_no_calls = 0
        messages.append(assistant_message(getattr(msg, "content", ""), calls))
        total_tool_calls += len(calls)
        for c in calls:
            if c.name in _FSM_ACTION_NAMES:
                obs = await step_fsm(fsm_app, c.name, c.arguments)
                tag = obs.get("error", "ok")
            elif call_grafana is None:
                obs = {"error": "unknown_tool", "tool": c.name}
                tag = "unknown"
            elif not _case_open(fsm_app):
                obs = {
                    "error": "invalid_transition",
                    "tool": c.name,
                    "message": "Call start_investigation before running tools, and do not run tools after conclude.",
                }
                tag = "not_open"
            else:
                # Full Grafana toolset: execute the real tool, then record it as
                # evidence (which also enforces the loop guard and feeds the gate).
                result = await call_grafana(c.name, c.arguments)
                rec = await step_fsm(
                    fsm_app,
                    "record_probe",
                    {
                        "tool": c.name,
                        "backend": infer_backend(c.name),
                        "query": json.dumps(c.arguments, default=str)[:300],
                        "result_summary": _result_summary(result),
                    },
                )
                if rec.get("error"):
                    obs = rec
                    tag = rec["error"]
                else:
                    obs = {
                        "ok": True,
                        "tool": c.name,
                        "result": result,
                        "phase": rec["state"]["phase"],
                        "current_prompt": rec["state"]["current_prompt"],
                    }
                    tag = "ok"
            log(f"[{step}] {c.name}({tag})")
            messages.append(
                {"role": "tool", "tool_call_id": c.id, "content": json.dumps(obs, default=str)}
            )
        steps_log.append({"step": step, "calls": [c.name for c in calls]})
        if fsm_terminated(fsm_app):
            log(f"[{step}] FSM terminated")
            break
    return {"steps": steps_log, "total_tool_calls": total_tool_calls}


async def run_agent() -> None:
    import litellm
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    model = normalize_model_name(os.environ["MODEL"])
    stack_host = os.environ.get("STACK_HOST", "127.0.0.1")
    mcp_url = os.environ.get("MCP_URL", f"http://{stack_host}:8080/mcp")
    system_prompt = Path("/app/system_prompt.txt").read_text(encoding="utf-8")
    task_prompt_template = Path("/app/task_prompt.txt").read_text(encoding="utf-8")
    statement = Path("/app/instruction.txt").read_text(encoding="utf-8").strip()
    task_prompt = task_prompt_template.format(
        current_time=scenario_clock_iso(), statement=statement
    )
    litellm.suppress_debug_info = True
    _temp_env = os.environ.get("TEMPERATURE")
    temperature = float(_temp_env) if _temp_env else None

    agent_dir = Path("/logs/agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("O11Y_SCENARIO_TIME_ISO", scenario_clock_iso())
    fsm_app = build_application(tracking=False)
    stats = {"input": 0, "output": 0, "cost": 0.0}
    steps_log: list[dict[str, Any]] = []
    total_tool_calls = 0
    start = time.time()

    print(
        f"Connecting to Grafana MCP at {mcp_url} (bound as upstream; not exposed to the agent)..."
    )
    async with streamable_http_client(mcp_url) as (read, write, _):  # noqa: SIM117 (keep the session block at its own indent for readability)
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Bind Grafana as the FSM's upstream so record_probe / start can reach
            # it, and expose the full Grafana toolset to the agent. The FSM gates
            # phases and termination; it does not restrict which tool is called.
            bind_upstream(_SessionUpstream(session))
            grafana_tools = await discover_grafana_tools(session)
            tools = FSM_TOOLS + grafana_tools
            print(
                f"Grafana bound. Agent surface = {len(grafana_tools)} Grafana tools "
                f"+ {len(FSM_TOOLS)} FSM control actions; phases and conclude are gated."
            )

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task_prompt},
            ]

            async def complete(msgs: list[dict[str, Any]]) -> Any:
                # tool_choice="required": the only legitimate way to finish is
                # conclude(), so an empty (no-tool) turn is always a defect; force a
                # call and detect termination via fsm_terminated. Leave temperature
                # at the provider default unless TEMPERATURE is set: some variance
                # lets the model vary a refused probe instead of repeating it.
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": msgs,
                    "tools": tools,
                    "tool_choice": "required",
                }
                if temperature is not None:
                    kwargs["temperature"] = temperature
                resp = await litellm.acompletion(**kwargs)
                u = cast(Any, resp).usage
                if u:
                    stats["input"] += getattr(u, "prompt_tokens", 0) or 0
                    stats["output"] += getattr(u, "completion_tokens", 0) or 0
                with contextlib.suppress(Exception):
                    stats["cost"] += litellm.completion_cost(completion_response=resp) or 0.0
                return cast(Any, resp).choices[0].message

            result = await drive_investigation(
                complete,
                fsm_app,
                messages,
                call_grafana=lambda name, args: call_upstream("grafana", name, args),
                max_steps=MAX_STEPS,
                log=lambda s: print(s, flush=True),
            )
            steps_log = result["steps"]
            total_tool_calls = result["total_tool_calls"]
            print("done")

            final_answer = ""
            with contextlib.suppress(Exception):
                final_answer = fsm_app.state.get("final_answer") or ""
            if not final_answer:
                for m in reversed(messages):
                    if m.get("role") == "assistant" and m.get("content"):
                        final_answer = m["content"]
                        break
            (agent_dir / "final_answer.txt").write_text(final_answer or "")
            atif_steps = atif_steps_from_messages(messages, final_answer)

    traj = {
        "schema_version": "ATIF-v1.7",
        "session_id": str(uuid.uuid4()),
        "agent": {"name": "phoebe", "version": "0.1.0", "model_name": model},
        "steps": atif_steps,
        "compact_steps": steps_log,
        "final_metrics": {
            "total_prompt_tokens": stats["input"],
            "total_completion_tokens": stats["output"],
            "total_cost_usd": stats["cost"],
            "total_tool_calls": total_tool_calls,
            "elapsed_seconds": time.time() - start,
            "fsm_terminated": fsm_terminated(fsm_app),
        },
    }
    (agent_dir / "trajectory.json").write_text(json.dumps(traj, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(run_agent())
