"""Drive one real investigation against the LOCAL o11y-bench stack and capture
it two ways: a tracked Theodosia session (for `theodosia watch` / the Burr UI)
and a narration sidecar JSON (per-step phase, the model's reasoning line, the
tool it called, a short real result, and the gate response) for the hero gif.

No grading, no Harbor container: this connects straight to the local stack's
Grafana MCP at :8080 and runs the same drive_investigation loop the bench uses.

    OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY=$TOGETHER_API_KEY \
    MODEL=openai/moonshotai/Kimi-K2.6 \
    uv run python scripts/capture_run.py

Writes scripts/hero_trace.json (the narration sidecar) and a tracked session
under ~/.theodosia/phoebe.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, cast

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from theodosia.upstream import bind_upstream, call_upstream

from phoebe.app import build_application
from phoebe.harbor import agent_runner as runner

_INCIDENT = (
    "Error rates and latency climbed across several services in the last hour. "
    "Find the primary service at fault, the root cause, and the blast radius."
)
_OUT = Path(os.environ.get("CAPTURE_OUT", Path(__file__).parent / "hero_trace.json"))


def _short(obj: Any, n: int = 220) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def narration_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each assistant tool call with its reasoning text and the tool result
    that followed, into a flat readable trace."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant":
            think = (m.get("content") or "").strip()
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                entry = {
                    "think": think,
                    "tool": fn.get("name", ""),
                    "args": args,
                    "result": "",
                    "status": "ok",
                }
                by_id[tc.get("id", "")] = entry
                order.append(entry)
                think = ""  # attach reasoning to the first call only
        elif m.get("role") == "tool":
            e = by_id.get(m.get("tool_call_id", ""))
            if e is None:
                continue
            content = m.get("content", "")
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                payload = {"result": content}
            if isinstance(payload, dict) and payload.get("error"):
                e["status"] = payload["error"]
                e["result"] = _short(payload.get("message") or payload.get("error_message") or "")
            else:
                res = payload.get("result") if isinstance(payload, dict) else payload
                e["result"] = _short(res if res is not None else payload)
                if isinstance(payload, dict) and payload.get("phase"):
                    e["phase"] = payload["phase"]
    return order


async def main() -> None:
    model = runner.normalize_model_name(os.environ["MODEL"])
    mcp_url = os.environ.get("MCP_URL", "http://127.0.0.1:8080/mcp")
    here = Path(__file__).resolve().parent.parent / "src" / "phoebe" / "harbor"
    system_prompt = (here / "system_prompt.txt").read_text(encoding="utf-8")
    scenario_time = os.environ.get("O11Y_SCENARIO_TIME_ISO") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    os.environ.setdefault("O11Y_SCENARIO_TIME_ISO", scenario_time)

    import litellm

    litellm.suppress_debug_info = True

    fsm_app = build_application(tracking=True)
    print(f"connecting to local Grafana MCP at {mcp_url} ...", flush=True)
    async with streamablehttp_client(mcp_url) as (read, write, _):  # noqa: SIM117
        async with ClientSession(read, write) as session:
            await session.initialize()
            bind_upstream(runner._SessionUpstream(session))
            grafana_tools = await runner.discover_grafana_tools(session)
            tools = runner.FSM_TOOLS + grafana_tools
            print(
                f"agent surface = {len(grafana_tools)} Grafana tools + "
                f"{len(runner.FSM_TOOLS)} FSM control actions",
                flush=True,
            )

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Scenario clock: {scenario_time}\n\nTask:\n{_INCIDENT}",
                },
            ]

            async def complete(msgs: list[dict[str, Any]]) -> Any:
                resp = await litellm.acompletion(
                    model=model, messages=msgs, tools=tools, tool_choice="required"
                )
                return cast(Any, resp).choices[0].message

            await runner.drive_investigation(
                complete,
                fsm_app,
                messages,
                call_grafana=lambda name, args: call_upstream("grafana", name, args),
                max_steps=runner.MAX_STEPS,
                log=lambda s: print(s, flush=True),
            )

    trace = {
        "model": model,
        "incident": _INCIDENT,
        "scenario_time": scenario_time,
        "terminated": runner.fsm_terminated(fsm_app),
        "final_answer": fsm_app.state.get("final_answer") or "",
        "primary_service": fsm_app.state.get("primary_service"),
        "root_cause": fsm_app.state.get("root_cause"),
        "steps": narration_from_messages(messages),
    }
    _OUT.write_text(json.dumps(trace, indent=2, default=str))
    print(f"\nterminated={trace['terminated']}  steps={len(trace['steps'])}")
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    asyncio.run(main())
