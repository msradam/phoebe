"""Infrastructure smoke test for the agent loop.

Drives the runner's full turn loop (drive_investigation) with a scripted
fake LLM and a mock Grafana upstream: no Docker, no real model, no grading.
It deliberately delivers some tool calls (including the terminal conclude)
in the leaked native-token format to prove the loop reaches a conclusion
even when a model emits that shape. This is the cheap guard that must pass
before spending tokens on a graded run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from theodosia.upstream import bind_upstream, reset_upstream

from o11y_fsm.app import build_application
from o11y_fsm.harbor import agent_runner as runner


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
        return {"ok": True, "tool": tool}


@pytest.fixture(autouse=True)
def _bind_mock():
    token = bind_upstream(_MockGrafana())
    try:
        yield
    finally:
        reset_upstream(token)


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
        return self._queue.pop(0)


@pytest.mark.asyncio
async def test_loop_reaches_conclusion_even_with_leaked_calls():
    final = (
        "Primary payment-service exhausted its connection pool at 14:02; order-service "
        "cascaded downstream. Metrics and logs agree."
    )
    script = _ScriptedLLM(
        [
            _structured(
                "start_investigation",
                {"incident_description": "5xx spike", "scenario_time": "2026-05-24T14:00:00Z"},
            ),
            _structured("query_metrics", {"promql": "sum(rate(http_5xx[5m]))"}),
            _structured("query_logs", {"logql": '{job="payment"} |= "error"'}),
            _structured("advance_phase", {"to": "diagnose", "rationale": "payment leads"}),
            # the model switches to its native token format from here:
            _leaked("advance_phase", {"to": "verify", "rationale": "metrics and logs agree"}),
            _leaked("query_metrics", {"promql": "pool_in_use{service='payment'}"}),
            _leaked(
                "conclude",
                {
                    "primary_service": "payment-service",
                    "cascade_services": ["order-service"],
                    "root_cause": "connection pool exhaustion at 14:02",
                    "final_answer": final,
                },
            ),
        ]
    )

    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "y"},
    ]
    result = await runner.drive_investigation(script, app, messages, max_steps=20)

    assert runner.fsm_terminated(app)
    assert app.state["final_answer"] == final
    assert app.state["investigation_summary"]["primary_service"] == "payment-service"
    assert result["total_tool_calls"] == 7


@pytest.mark.asyncio
async def test_loop_nudges_past_an_empty_turn():
    final = (
        "Primary payment-service exhausted its connection pool; order-service cascaded. "
        "Metrics and logs agree on the timing."
    )
    script = _ScriptedLLM(
        [
            _structured(
                "start_investigation",
                {"incident_description": "5xx spike", "scenario_time": "2026-05-24T14:00:00Z"},
            ),
            SimpleNamespace(content="", tool_calls=None),  # intermittent empty turn
            _structured("query_metrics", {"promql": "sum(rate(http_requests_total[5m]))"}),
            _structured("query_logs", {"logql": '{service_name="payment-service"} |= "error"'}),
            _structured("advance_phase", {"to": "diagnose", "rationale": "payment leads"}),
            _structured("advance_phase", {"to": "verify", "rationale": "metrics and logs agree"}),
            _structured("query_metrics", {"promql": "pool_in_use{service='payment'}"}),
            _structured(
                "conclude",
                {
                    "primary_service": "payment-service",
                    "cascade_services": ["order-service"],
                    "root_cause": "connection pool exhaustion",
                    "final_answer": final,
                },
            ),
        ]
    )
    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "y"}]
    result = await runner.drive_investigation(script, app, messages, max_steps=20)
    assert runner.fsm_terminated(app), result
    assert app.state["final_answer"] == final


@pytest.mark.asyncio
async def test_loop_stops_after_repeated_no_calls():
    script = _ScriptedLLM([SimpleNamespace(content="", tool_calls=None) for _ in range(5)])
    app = build_application(tracking=False)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "y"}]
    result = await runner.drive_investigation(script, app, messages, max_steps=10)
    assert not runner.fsm_terminated(app)
    assert len(result["steps"]) == runner.MAX_NO_CALL_TURNS
