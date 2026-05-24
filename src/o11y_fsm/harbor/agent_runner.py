# /// script
# dependencies = [
#   "apache-burr>=0.42,<0.43",
#   "pydantic>=2,<3",
#   "litellm==1.83.10",
#   "mcp>=1.9.0",
# ]
# ///
"""o11y-fsm agent runner (v0.2, circe-style) — runs inside the Harbor container.

Single tool surface: the LLM sees ONLY the o11y-fsm actions
(start_investigation, query_metrics, query_logs, query_traces,
advance_phase, conclude). It does NOT see the raw Grafana MCP tools.
The FSM's query actions own the telemetry: they call a
GrafanaMCPTelemetryClient (bound on o11y_fsm.telemetry's ContextVar)
that proxies to the Grafana MCP session under the hood.

This is the fix for the two-surface problem: a weak model can't get
absorbed in raw queries and forget to drive the FSM, because the only
way to query telemetry is *through* an FSM action.
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
from o11y_fsm.telemetry import bind_telemetry_client  # noqa: E402

# == helpers =========================================================


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


# == Grafana MCP-backed telemetry client =============================


class GrafanaMCPTelemetryClient:
    """Implements o11y_fsm.telemetry.TelemetryClient by proxying to the
    Grafana MCP session. Resolves the right query tool per backend by
    name-matching the discovered tools, and fills the query into the
    tool's most likely query argument.
    """

    _BACKEND_KEYWORDS = {
        "prometheus": ("prometheus", "promql", "metric"),
        "loki": ("loki", "logql", "log"),
        "tempo": ("tempo", "trace"),
    }
    _QUERY_ARG_CANDIDATES = ("expr", "query", "promql", "logql", "traceql", "q", "logQL", "promQL")

    def __init__(self, session: Any, tools: list[dict[str, Any]]):
        self._session = session
        self._tools = tools  # list of {"name", "description", "parameters"}
        self._by_backend: dict[str, dict[str, Any]] = {}
        for backend, keywords in self._BACKEND_KEYWORDS.items():
            tool = self._find_tool(keywords)
            if tool:
                self._by_backend[backend] = tool

    def _find_tool(self, keywords: tuple[str, ...]) -> dict[str, Any] | None:
        # Prefer a tool whose name contains a keyword AND looks like a query.
        named = [t for t in self._tools if any(k in t["name"].lower() for k in keywords)]
        for t in named:
            if "quer" in t["name"].lower():
                return t
        return named[0] if named else None

    def _query_arg(self, tool: dict[str, Any]) -> str:
        props = (tool.get("parameters") or {}).get("properties") or {}
        for cand in self._QUERY_ARG_CANDIDATES:
            if cand in props:
                return cand
        # last resort: first string property
        for name, spec in props.items():
            if spec.get("type") == "string":
                return name
        return "query"

    async def query(self, backend: str, query: str, **kwargs: Any) -> dict[str, Any]:
        tool = self._by_backend.get(backend)
        if tool is None:
            return {
                "ok": False,
                "backend": backend,
                "summary": f"no Grafana MCP tool resolved for backend {backend!r}; "
                f"available backends: {sorted(self._by_backend)}",
            }
        arg = self._query_arg(tool)
        try:
            res = await self._session.call_tool(tool["name"], {arg: query})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": backend, "summary": f"query error: {e}"}
        text = self._extract_text(res)
        return {
            "ok": True,
            "backend": backend,
            "tool": tool["name"],
            "summary": text[:600] if text else "(empty result)",
            "raw": text[:4000],
        }

    @staticmethod
    def _extract_text(res: Any) -> str:
        if getattr(res, "structured_content", None) is not None:
            return json.dumps(res.structured_content, default=str)
        if getattr(res, "content", None):
            parts = [getattr(c, "text", "") for c in res.content if getattr(c, "text", "")]
            if parts:
                return "\n".join(parts)
        return ""

    async def list_datasources(self) -> list[dict[str, Any]]:
        return [{"name": b, "type": b} for b in self._by_backend]


# == FSM actions exposed as LLM tools ================================

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
            "name": "query_metrics",
            "description": "Run a Prometheus/PromQL query and record the evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {"type": "string"},
                    "hypothesis": {
                        "type": "string",
                        "description": "Optional reason for this probe.",
                    },
                },
                "required": ["promql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": "Run a Loki/LogQL query and record the evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "logql": {"type": "string"},
                    "hypothesis": {"type": "string"},
                },
                "required": ["logql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_traces",
            "description": "Run a Tempo/TraceQL query and record the evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "traceql": {"type": "string"},
                    "hypothesis": {"type": "string"},
                },
                "required": ["traceql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_phase",
            "description": (
                "Advance the investigation phase: triage -> diagnose -> verify. "
                "to='diagnose' needs >=1 probe; to='verify' needs probes from "
                ">=2 distinct backends."
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
                "Finish the investigation with the conclusion + final answer. "
                "Requires phase=='verify', probes from >=2 backends, and a probe "
                "taken during the verify phase."
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
    # Keep the observation compact: drop bulky probe payloads, keep counts + prompt.
    compact = {
        "phase": sv.get("phase"),
        "distinct_backends": sv.get("distinct_backends"),
        "n_probes": len(sv.get("probes") or []),
        "current_prompt": sv.get("current_prompt"),
        "final_answer_set": sv.get("final_answer") is not None,
    }
    return {
        "ok": True,
        "action_executed": action,
        "valid_next_actions": _valid_next_actions(app),
        "state": compact,
    }


def fsm_terminated(app: Any) -> bool:
    try:
        return app.state.get("final_answer") not in (None, "")
    except Exception:
        return False


# == main loop =======================================================

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
                    "name": t.name,
                    "description": (t.description or ""),
                    "parameters": t.inputSchema or {},
                }
                for t in listed.tools
            ]
            print(
                f"Discovered {len(grafana_tools)} Grafana MCP tools (proxied; not exposed to LLM)"
            )

            # Bind the telemetry client so the FSM query actions reach Grafana.
            client = GrafanaMCPTelemetryClient(session, grafana_tools)
            print(f"Telemetry backends resolved: {sorted(client._by_backend)}")
            bind_telemetry_client(client)

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]

            step = 0
            while step < MAX_STEPS:
                step += 1
                print(f"[{step}]", end=" ", flush=True)
                resp = await litellm.acompletion(
                    model=model, messages=messages, tools=FSM_TOOLS, tool_choice="auto"
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
                        print(f"{fn}({obs.get('error', 'ok')})", end=" ", flush=True)
                    else:
                        obs = {
                            "error": "unknown_tool",
                            "message": f"{fn} is not an o11y-fsm action",
                        }
                        print(f"?{fn}", end=" ", flush=True)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(obs, default=str),
                        }
                    )
                steps_log.append(
                    {
                        "step": step,
                        "type": "tools",
                        "calls": [tc.function.name for tc in tool_calls],
                    }
                )

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
        "agent": {"name": "o11y-fsm", "version": "0.2.0", "model_name": model},
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
