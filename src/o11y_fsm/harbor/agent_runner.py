# /// script
# dependencies = [
#   "apache-burr[tracking]>=0.42,<0.43",
#   "pydantic>=2,<3",
#   "litellm==1.83.10",
#   "mcp>=1.9.0",
# ]
# ///
"""o11y-fsm agent runner — executes inside the Harbor container.

Mirrors o11y-bench's default agent_runner but adds a single in-process
"advance_workflow" tool that drives the o11y-fsm Burr Application. The
LLM sees Grafana MCP tools (for actual telemetry queries) PLUS the
advance_workflow tool (for phase commitments). The FSM enforces the
investigation methodology; the LLM does the work between phases.

Termination: when advance_workflow returns a state where the FSM has
terminated (final_answer populated), the runner writes the trajectory
and exits. The final_answer is written to /logs/agent/final_answer.txt
so the verifier can read it.

The o11y-fsm package itself is bundled into the container under
/app/o11y_fsm (uploaded by the Harbor agent in setup()).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

# o11y_fsm is uploaded under /app/o11y_fsm by the Harbor agent's setup().
sys.path.insert(0, "/app")

from o11y_fsm import build_application  # noqa: E402


# == helpers (mirrored from o11y-bench's agent_runner) ===============


def scenario_clock_iso() -> str:
    iso = os.environ.get("O11Y_SCENARIO_TIME_ISO")
    if iso:
        return iso
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_model_name(model_name: str) -> str:
    if model_name.startswith("google/"):
        return f"gemini/{model_name.split('/', 1)[1]}"
    return model_name


def relax_mcp_tool_input_schema_for_llm(schema: dict[str, Any]) -> dict[str, Any]:
    """Some MCP tools advertise schemas using JSON Schema features (e.g. anyOf
    in property types) that not every LLM SDK round-trips cleanly. Strip the
    weirdest cases so litellm + the provider accept the tool defs."""
    out = copy.deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "anyOf" in node and "type" not in node:
                # collapse anyOf-of-string-or-null to just string
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


async def discover_mcp_tools(session: Any) -> list[dict[str, Any]]:
    tools = await session.list_tools()
    out = []
    for t in tools.tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "parameters": relax_mcp_tool_input_schema_for_llm(t.inputSchema or {}),
                },
            }
        )
    return out


async def call_mcp_tool(session: Any, name: str, arguments: dict[str, Any]) -> str:
    res = await session.call_tool(name, arguments)
    if hasattr(res, "structured_content") and res.structured_content is not None:
        return json.dumps(res.structured_content, default=str)
    if hasattr(res, "content") and res.content:
        parts = []
        for c in res.content:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return json.dumps({"_note": "no content"}, default=str)


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw": arguments}
    return {"_raw": str(arguments)}


# == the advance_workflow tool definition ============================


ADVANCE_WORKFLOW_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "advance_workflow",
        "description": (
            "Drive the o11y-fsm investigation workflow. Call this to commit "
            "to a phase. The workflow enforces SRE methodology: start_investigation "
            "→ survey_telemetry → gather_evidence (loops; ≥2 backends required) → "
            "correlate → form_hypothesis → verify_or_revise → recommend_next_steps. "
            "Returns the new state and the next phase's prompt in `current_prompt`. "
            "Returns an error payload with `valid_next_actions` if you call an "
            "out-of-order action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The FSM action to invoke.",
                    "enum": [
                        "start_investigation",
                        "survey_telemetry",
                        "gather_evidence",
                        "correlate",
                        "form_hypothesis",
                        "verify_or_revise",
                        "recommend_next_steps",
                    ],
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Action-specific keyword arguments. Read the prior step's "
                        "`current_prompt` for the exact shape this action expects."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["action", "inputs"],
        },
    },
}


def call_advance_workflow(app: Any, action: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute one FSM step in-process.

    Returns a dict with either the new state slice (success path) or a
    structured error with `valid_next_actions` (refusal). The LLM gets
    this back as its tool observation.
    """
    # Burr's "next action" mechanism: monkey-patch get_next_action like burrmcp does.
    valid_now = [
        t.to_action.name
        for t in app.application_state.next_action_choices
    ] if hasattr(app, "application_state") else []
    # Compute valid next actions defensively from the graph.
    try:
        valid_now = sorted(
            {t.to.name for t in app.graph.get_next_node_choices(app.state, app.action_choice_history)}
        ) if hasattr(app.graph, "get_next_node_choices") else valid_now
    except Exception:
        pass
    # Re-compute via internal API: get all transitions from current action whose conditions evaluate.
    try:
        current_action_name = (
            app.current_action.name if app.current_action else "start_investigation"
        )
    except Exception:
        current_action_name = "start_investigation"

    legal = _legal_next_actions(app)
    if action not in legal:
        return {
            "error": "invalid_transition",
            "requested": action,
            "valid_next_actions": legal,
            "message": (
                f"action {action!r} is not reachable from the current state. "
                f"Reachable now: {legal}."
            ),
        }
    # Use burrmcp's trick: monkey-patch get_next_action to return our chosen action.
    return _step_named(app, action, inputs)


def _legal_next_actions(app: Any) -> list[str]:
    """Compute the next legal actions from the current state by walking the
    graph and evaluating each transition's condition."""
    from burr.core.action import DEFAULT
    state = app.state
    # Figure out current action (or entrypoint if not yet started).
    try:
        current = app.current_action.name if app.current_action else app.graph.entrypoint
    except Exception:
        current = app.graph.entrypoint
    legal: list[str] = []
    for transition in app.graph.transitions:
        if transition.from_.name != current:
            continue
        cond = transition.condition
        if cond is None or cond.name == DEFAULT.name or _eval_condition(cond, state):
            if transition.to.name not in legal:
                legal.append(transition.to.name)
    return legal


def _eval_condition(condition: Any, state: Any) -> bool:
    try:
        result = condition.run(state)
        if isinstance(result, dict):
            return bool(result.get("PROCEED", False))
        return bool(result)
    except Exception:
        return False


def _step_named(app: Any, action_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Drive one Burr step with the agent-chosen action."""
    original_get_next_action = app.get_next_action
    try:
        app.get_next_action = lambda: next(
            a for a in app.graph.actions if a.name == action_name
        )
        # astep returns (action_run, result, state)
        loop = asyncio.get_event_loop()
        action_run, result, new_state = loop.run_until_complete(app.astep(inputs=inputs))
        # Get refreshed legal next from the new state.
        legal_next = _legal_next_actions(app)
        # Trim state for the tool response: omit big internal fields.
        state_view = {k: v for k, v in new_state.get_all().items() if not k.startswith("__")}
        return {
            "ok": True,
            "action_executed": action_name,
            "valid_next_actions": legal_next,
            "state": state_view,
        }
    except Exception as e:
        return {
            "error": "action_error",
            "requested": action_name,
            "error_message": str(e),
        }
    finally:
        app.get_next_action = original_get_next_action


def fsm_terminated(app: Any) -> bool:
    """The FSM is done when recommend_next_steps has been called (final_answer set)."""
    try:
        return app.state.get("final_answer") not in (None, "")
    except Exception:
        return False


# == main agent loop =================================================


SYSTEM_PROMPT = Path("/app/system_prompt.txt").read_text(encoding="utf-8")
TASK_PROMPT_TEMPLATE = Path("/app/task_prompt.txt").read_text(encoding="utf-8")
MAX_STEPS = 60


async def run_agent() -> None:
    import litellm
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    model = normalize_model_name(os.environ["MODEL"])
    stack_host = os.environ.get("STACK_HOST", "127.0.0.1")
    mcp_url = os.environ.get("MCP_URL", f"http://{stack_host}:8080/mcp")

    statement = Path("/app/instruction.txt").read_text(encoding="utf-8").strip()
    env_ts = scenario_clock_iso()
    task_prompt = TASK_PROMPT_TEMPLATE.format(current_time=env_ts, statement=statement)

    litellm.suppress_debug_info = True

    agent_dir = Path("/logs/agent")
    agent_dir.mkdir(parents=True, exist_ok=True)

    session_id = str(uuid.uuid4())
    trajectory_id = str(uuid.uuid4())
    steps_log: list[dict[str, Any]] = []
    stats = {"input": 0, "output": 0, "cost": 0.0}
    step_id = 0
    total_tool_calls = 0
    fsm_app = build_application()
    start = time.time()

    def flush() -> None:
        traj = {
            "schema_version": "ATIF-v1.7",
            "session_id": session_id,
            "trajectory_id": trajectory_id,
            "agent": {
                "name": "o11y-fsm",
                "version": "0.1.0",
                "model_name": model,
                "tool_definitions": tool_defs,
            },
            "steps": steps_log,
            "final_metrics": {
                "total_prompt_tokens": stats["input"],
                "total_completion_tokens": stats["output"],
                "total_cost_usd": stats["cost"],
                "total_steps": step_id,
                "extra": {
                    "total_tool_calls": total_tool_calls,
                    "elapsed_seconds": time.time() - start,
                    "fsm_terminated": fsm_terminated(fsm_app),
                },
            },
        }
        (agent_dir / "trajectory.json").write_text(json.dumps(traj, indent=2, default=str))

    print(f"Connecting to MCP at {mcp_url}...")
    async with streamable_http_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = await discover_mcp_tools(session)
            tool_defs: list[dict[str, Any]] = [
                t["function"] for t in mcp_tools
            ] + [ADVANCE_WORKFLOW_TOOL_DEF["function"]]
            print(f"Discovered {len(mcp_tools)} MCP tools + advance_workflow (FSM)")

            tools_for_llm = mcp_tools + [ADVANCE_WORKFLOW_TOOL_DEF]

            steps_log.append({"step_id": 0, "type": "system", "content": SYSTEM_PROMPT})
            steps_log.append({"step_id": 1, "type": "user", "content": task_prompt})
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]

            step = 0
            while step < MAX_STEPS:
                step += 1
                print(f"[{step}]", end=" ", flush=True)

                resp = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=tools_for_llm,
                    tool_choice="auto",
                )
                msg = cast(Any, resp).choices[0].message
                content = msg.content or ""
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
                    step_id += 1
                    steps_log.append({"step_id": step_id, "type": "assistant", "content": content})
                    print("done (no tool calls)")
                    break

                messages.append(msg.model_dump())
                total_tool_calls += len(tool_calls)

                for tc in tool_calls:
                    fn = tc.function.name or ""
                    args = parse_tool_arguments(tc.function.arguments)
                    if fn == "advance_workflow":
                        action = args.get("action", "")
                        inputs = args.get("inputs") or {}
                        if isinstance(inputs, str):
                            try:
                                inputs = json.loads(inputs)
                            except json.JSONDecodeError:
                                inputs = {}
                        obs = call_advance_workflow(fsm_app, action, inputs)
                        print(f"FSM:{action}({obs.get('error','ok')})", end=" ", flush=True)
                    else:
                        try:
                            obs_raw = await call_mcp_tool(session, fn, args)
                            # cap obs size to avoid token blow-up
                            if len(obs_raw) > 6000:
                                obs_raw = obs_raw[:6000] + "...[truncated]"
                            obs = obs_raw
                        except Exception as e:
                            obs = json.dumps({"error": "mcp_tool_error", "message": str(e)})
                        print(f"{fn}", end=" ", flush=True)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": obs if isinstance(obs, str) else json.dumps(obs, default=str),
                        }
                    )

                if fsm_terminated(fsm_app):
                    print("FSM terminated")
                    break

            print("done")

            # Surface the FSM's final_answer for the verifier.
            final_answer = ""
            try:
                final_answer = fsm_app.state.get("final_answer") or ""
            except Exception:
                pass
            if not final_answer and messages:
                # Fallback: last assistant message.
                for m in reversed(messages):
                    if m.get("role") == "assistant" and m.get("content"):
                        final_answer = m["content"]
                        break
            (agent_dir / "final_answer.txt").write_text(final_answer or "")

    flush()


if __name__ == "__main__":
    asyncio.run(run_agent())
