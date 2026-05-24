# /// script
# dependencies = [
#   "apache-burr>=0.42,<0.43",
#   "pydantic>=2,<3",
#   "litellm==1.83.10",
#   "mcp>=1.9.0",
# ]
# ///
"""o11y-fsm agent runner (v0.3, external_tools federation) — runs in the Harbor container.

The agent sees BOTH surfaces:
  - the o11y-fsm actions (start_investigation, record_finding,
    advance_phase, conclude) — driven via in-process FSM steps;
  - the Grafana MCP tools (query_prometheus, query_loki_logs, ...) —
    passed through to the Grafana MCP session.

The FSM does not proxy queries. It declares which Grafana tools are
relevant per phase (mount(external_tools=...)); each FSM step response
carries next_external_tools telling the agent which Grafana tools to
call next. The agent calls them natively, then record_finding's the
result. The Burr graph conducts; the Grafana server executes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, "/app")

from o11y_fsm import build_application  # noqa: E402


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


def relax_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Collapse anyOf-of-string-or-null property types some MCP tools use,
    which not every provider round-trips cleanly."""
    import copy

    out = copy.deepcopy(schema or {})

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "anyOf" in node and "type" not in node:
                non_null = [b for b in node["anyOf"] if b.get("type") != "null"]
                if non_null:
                    node.clear()
                    node.update(non_null[0])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for it in node:
                _walk(it)

    _walk(out)
    return out


# == FSM actions as LLM tools ========================================

FSM_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "start_investigation",
            "description": "Open the investigation. Call this first.",
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
            "name": "record_finding",
            "description": (
                "Record a finding from a Grafana query you ALREADY ran. Call a "
                "Grafana tool first (see next_external_tools), then record what "
                "it showed here. The FSM gates progression on the evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "backend": {"type": "string", "description": "prometheus | loki | tempo"},
                    "query": {"type": "string"},
                    "result_summary": {"type": "string"},
                    "hypothesis": {"type": "string"},
                },
                "required": ["backend", "query", "result_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_phase",
            "description": (
                "Advance triage -> diagnose -> verify. to='diagnose' needs >=1 "
                "finding; to='verify' needs findings from >=2 distinct backends."
            ),
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
            "description": (
                "Finish with the conclusion + final answer. Requires phase=='verify', "
                "findings from >=2 backends, and a finding recorded during verify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_service": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "final_answer": {"type": "string"},
                    "cascade_services": {"type": "array", "items": {"type": "string"}},
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


# external_tools map, mirrored from o11y_fsm.app so the runner can surface
# next_external_tools in the FSM step observation (the in-process FSM
# doesn't go through burrmcp.mount, so we attach it here).
from o11y_fsm.app import EXTERNAL_TOOLS  # noqa: E402


def _next_external_tools(valid: list[str]) -> dict[str, list[str]]:
    return {a: EXTERNAL_TOOLS[a] for a in valid if EXTERNAL_TOOLS.get(a)}


async def step_fsm(app: Any, action: str, inputs: dict[str, Any]) -> dict[str, Any]:
    legal = _valid_next_actions(app)
    if action not in legal:
        return {
            "error": "invalid_transition",
            "requested": action,
            "valid_next_actions": legal,
            "next_external_tools": _next_external_tools(legal),
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
    valid = _valid_next_actions(app)
    return {
        "ok": True,
        "action_executed": action,
        "valid_next_actions": valid,
        "next_external_tools": _next_external_tools(valid),
        "state": {
            "phase": sv.get("phase"),
            "distinct_backends": sv.get("distinct_backends"),
            "n_findings": len(sv.get("findings") or []),
            "current_prompt": sv.get("current_prompt"),
            "final_answer_set": sv.get("final_answer") is not None,
        },
    }


def fsm_terminated(app: Any) -> bool:
    try:
        return app.state.get("final_answer") not in (None, "")
    except Exception:
        return False


def _extract_text(res: Any) -> str:
    if getattr(res, "structured_content", None) is not None:
        return json.dumps(res.structured_content, default=str)
    if getattr(res, "content", None):
        parts = [getattr(c, "text", "") for c in res.content if getattr(c, "text", "")]
        if parts:
            return "\n".join(parts)
    return ""


SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8")
TASK_PROMPT_TEMPLATE = Path("/app/task_prompt.txt").read_text(encoding="utf-8")
MAX_STEPS = 50


async def run_agent() -> None:
    import litellm
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    model = normalize_model_name(os.environ["MODEL"])
    stack_host = os.environ.get("STACK_HOST", "127.0.0.1")
    mcp_url = os.environ.get("MCP_URL", f"http://{stack_host}:8080/mcp")
    statement = Path("/app/instruction.txt").read_text(encoding="utf-8").strip()
    task_prompt = TASK_PROMPT_TEMPLATE.format(
        current_time=scenario_clock_iso(), statement=statement
    )
    litellm.suppress_debug_info = True

    agent_dir = Path("/logs/agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    fsm_app = build_application(tracking=False)
    stats = {"input": 0, "output": 0, "cost": 0.0}
    steps_log: list[dict[str, Any]] = []
    total_tool_calls = 0
    start = time.time()

    print(f"Connecting to Grafana MCP at {mcp_url}...")
    async with streamable_http_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            grafana_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": (t.description or "").strip()[:1024],
                        "parameters": relax_schema(t.inputSchema or {}),
                    },
                }
                for t in listed.tools
            ]
            grafana_names = {t["function"]["name"] for t in grafana_tools}
            print(
                f"Discovered {len(grafana_tools)} Grafana tools; exposing them "
                f"alongside {len(FSM_TOOLS)} FSM tools (federation)."
            )

            tools_for_llm = FSM_TOOLS + grafana_tools

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]

            step = 0
            while step < MAX_STEPS:
                step += 1
                print(f"[{step}]", end=" ", flush=True)
                resp = await litellm.acompletion(
                    model=model, messages=messages, tools=tools_for_llm, tool_choice="auto"
                )
                msg = cast(Any, resp).choices[0].message
                tool_calls = msg.tool_calls or []
                u = cast(Any, resp).usage
                if u:
                    stats["input"] += getattr(u, "prompt_tokens", 0) or 0
                    stats["output"] += getattr(u, "completion_tokens", 0) or 0
                try:
                    stats["cost"] += litellm.completion_cost(completion_response=resp) or 0.0
                except Exception:
                    pass

                if not tool_calls:
                    steps_log.append(
                        {"step": step, "type": "assistant", "content": msg.content or ""}
                    )
                    print("done (no tool calls)")
                    break

                messages.append(msg.model_dump())
                total_tool_calls += len(tool_calls)
                for tc in tool_calls:
                    fn = tc.function.name or ""
                    args = parse_tool_arguments(tc.function.arguments)
                    if fn in _FSM_ACTION_NAMES:
                        obs = await step_fsm(fsm_app, fn, args)
                        tag = obs.get("error", "ok")
                    elif fn in grafana_names:
                        try:
                            res = await session.call_tool(fn, args)
                            text = _extract_text(res)
                            obs = {"ok": True, "tool": fn, "result": text[:5000]}
                            tag = "grafana"
                        except Exception as e:  # noqa: BLE001
                            obs = {"error": "grafana_tool_error", "tool": fn, "message": str(e)}
                            tag = "grafana_err"
                    else:
                        obs = {"error": "unknown_tool", "tool": fn}
                        tag = "unknown"
                    print(f"{fn}({tag})", end=" ", flush=True)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(obs, default=str),
                        }
                    )
                steps_log.append({"step": step, "calls": [tc.function.name for tc in tool_calls]})
                if fsm_terminated(fsm_app):
                    print("FSM terminated")
                    break
            print("done")

            final_answer = ""
            try:
                final_answer = fsm_app.state.get("final_answer") or ""
            except Exception:
                pass
            if not final_answer:
                for m in reversed(messages):
                    if m.get("role") == "assistant" and m.get("content"):
                        final_answer = m["content"]
                        break
            (agent_dir / "final_answer.txt").write_text(final_answer or "")

    traj = {
        "schema_version": "ATIF-v1.7",
        "session_id": str(uuid.uuid4()),
        "agent": {"name": "o11y-fsm", "version": "0.3.0", "model_name": model},
        "steps": steps_log,
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
