"""Infrastructure smoke test for the agent loop with the open toolset.

Drives the runner's loop (drive_investigation) with a scripted fake LLM that
calls the real Grafana tool names (routed through call_grafana + record_probe)
plus the FSM control actions. A mock upstream stands in for Grafana, so no
Docker, no real model, no grading. Some calls arrive in the leaked native-token
format to prove the loop still drives to a conclusion.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from theodosia.upstream import bind_upstream, call_upstream, reset_upstream

from phoebe.app import build_application
from phoebe.harbor import agent_runner as runner


class _MockGrafana:
    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        if tool == "list_datasources":
            return [
                {"uid": "prom-1", "type": "prometheus"},
                {"uid": "loki-1", "type": "loki"},
                {"uid": "tempo-1", "type": "tempo"},
            ]
        if tool == "list_prometheus_metric_names":
            return ["http_requests_total", "up"]
        if tool == "list_prometheus_label_names":
            return ["job", "status"]
        if tool == "list_prometheus_label_values":
            return ["payment-service", "order-service"]
        if tool == "list_loki_label_names":
            return ["job", "service", "level"]
        return {"ok": True, "tool": tool, "rows": 3}


@pytest.fixture(autouse=True)
def _bind_mock():
    token = bind_upstream(_MockGrafana())
    try:
        yield
    finally:
        reset_upstream(token)


def _grafana(name, args):
    return call_upstream("grafana", name, args)


def _structured(name: str, args: dict[str, Any]):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=f"c_{name}", function=fn)])


def _leaked(name: str, args: dict[str, Any]):
    body = (
        f"<|tool_calls_section_begin|><|tool_call_begin|>functions.{name}:0"
        f"<|tool_call_argument_begin|>{json.dumps(args)}<|tool_call_end|><|tool_calls_section_end|>"
    )
    return SimpleNamespace(content=body, tool_calls=None)


class _ScriptedLLM:
    def __init__(self, messages: list[Any]):
        self._queue = list(messages)

    async def __call__(self, _messages: list[dict[str, Any]]) -> Any:
        if not self._queue:
            return SimpleNamespace(content="", tool_calls=None)
        return self._queue.pop(0)


_FINAL = (
    "Primary payment-service exhausted its connection pool at 14:02; order-service cascaded "
    "downstream. Metrics and logs agree on the timing."
)


@pytest.mark.asyncio
async def test_open_toolset_walk_reaches_conclusion():
    script = _ScriptedLLM(
        [
            _structured(
                "start_investigation",
                {"incident_description": "5xx spike", "scenario_time": "2026-05-24T14:00:00Z"},
            ),
            _structured(
                "query_prometheus", {"expr": 'rate(http_requests_total{status=~"5.."}[5m])'}
            ),
            _structured(
                "query_loki_logs", {"logql": '{service_name="payment-service"} |= "error"'}
            ),
            # a real Grafana tool the old narrow design could not reach:
            _structured("tempo_get-trace", {"traceID": "abc123"}),
            _structured("advance_phase", {"to": "diagnose", "rationale": "payment leads"}),
            _leaked("advance_phase", {"to": "verify", "rationale": "metrics and logs agree"}),
            _leaked("query_prometheus", {"expr": "pool_in_use{service='payment'}"}),
            _leaked(
                "conclude",
                {
                    "primary_service": "payment-service",
                    "cascade_services": ["order-service"],
                    "root_cause": "connection pool exhaustion at 14:02",
                    "final_answer": _FINAL,
                },
            ),
        ]
    )
    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "y"}]
    await runner.drive_investigation(script, app, messages, call_grafana=_grafana, max_steps=20)
    assert runner.fsm_terminated(app)
    assert app.state["final_answer"] == _FINAL
    # tempo_get-trace was reachable and recorded as tempo evidence
    tools_used = {f["tool"] for f in app.state["findings"]}
    assert "tempo_get-trace" in tools_used


@pytest.mark.asyncio
async def test_grafana_tool_before_start_is_refused():
    script = _ScriptedLLM([_structured("query_prometheus", {"expr": "up"})])
    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "y"}]
    await runner.drive_investigation(script, app, messages, call_grafana=_grafana, max_steps=2)
    assert not app.state["findings"]  # nothing recorded; case was not open


@pytest.mark.asyncio
async def test_loop_stops_after_repeated_no_calls():
    script = _ScriptedLLM([SimpleNamespace(content="", tool_calls=None) for _ in range(5)])
    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "y"}]
    result = await runner.drive_investigation(
        script, app, messages, call_grafana=_grafana, max_steps=10
    )
    assert not runner.fsm_terminated(app)
    assert len(result["steps"]) == runner.MAX_NO_CALL_TURNS
