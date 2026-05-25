"""Benchmark smoke test at the step level: walk the FSM through the runner's
step_fsm + record_probe path offline (mock upstream, no LLM, no Docker),
confirming the phase gates hold and the case reaches a terminal conclusion.
"""

from __future__ import annotations

from typing import Any

import pytest
from theodosia.upstream import bind_upstream, reset_upstream

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
        return {"ok": True, "tool": tool}


@pytest.fixture(autouse=True)
def _bind_mock():
    token = bind_upstream(_MockGrafana())
    try:
        yield
    finally:
        reset_upstream(token)


async def _probe(app, tool, backend, query):
    return await runner.step_fsm(
        app,
        "record_probe",
        {"tool": tool, "backend": backend, "query": query, "result_summary": "r"},
    )


@pytest.mark.asyncio
async def test_step_walk_to_conclusion():
    app = build_application(tracking=False)
    assert (
        await runner.step_fsm(
            app,
            "start_investigation",
            {"incident_description": "5xx spike", "scenario_time": "2026-05-24T14:00:00Z"},
        )
    ).get("error") is None
    assert (
        await _probe(app, "query_prometheus", "prometheus", "rate(http_requests_total[5m])")
    ).get("error") is None
    assert (await _probe(app, "query_loki_logs", "loki", '{service_name="payment-service"}')).get(
        "error"
    ) is None
    assert (
        await runner.step_fsm(app, "advance_phase", {"to": "diagnose", "rationale": "payment"})
    ).get("error") is None
    assert (
        await runner.step_fsm(app, "advance_phase", {"to": "verify", "rationale": "agree"})
    ).get("error") is None
    assert (await _probe(app, "query_prometheus", "prometheus", "pool_in_use")).get("error") is None
    out = await runner.step_fsm(
        app,
        "conclude",
        {
            "primary_service": "payment-service",
            "cascade_services": ["order-service"],
            "root_cause": "connection pool exhaustion at 14:02",
            "final_answer": (
                "Primary payment-service exhausted its connection pool at 14:02; order-service "
                "cascaded downstream. Metrics and logs agree."
            ),
        },
    )
    assert out.get("error") is None
    assert runner.fsm_terminated(app)
    assert app.state["investigation_summary"]["primary_service"] == "payment-service"


@pytest.mark.asyncio
async def test_step_refuses_conclude_before_start():
    app = build_application(tracking=False)
    out = await runner.step_fsm(
        app, "conclude", {"primary_service": "x", "root_cause": "y", "final_answer": "z"}
    )
    assert out["error"] == "invalid_transition"
    assert out["valid_next_actions"] == ["start_investigation"]
