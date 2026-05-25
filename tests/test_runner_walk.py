"""Benchmark smoke test: walk a full investigation through the Harbor
runner's driving logic offline.

This exercises the exact code that runs inside the o11y-bench container
(``agent_runner.step_fsm`` + the get_next_action override + transition
gating), with a mock bound as the Grafana upstream and no LLM. It is the
fast guard that the container path still reaches a valid conclusion.
"""

from __future__ import annotations

from typing import Any

import pytest
from theodosia.upstream import bind_upstream, reset_upstream

from o11y_fsm.app import build_application
from o11y_fsm.harbor import agent_runner as runner


class _MockGrafana:
    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        assert server == "grafana"
        if tool == "list_datasources":
            return [
                {"uid": "prom-1", "type": "prometheus"},
                {"uid": "loki-1", "type": "loki"},
                {"uid": "tempo-1", "type": "tempo"},
            ]
        return {"ok": True, "tool": tool, "uid": args.get("datasourceUid")}


@pytest.fixture(autouse=True)
def _bind_mock():
    token = bind_upstream(_MockGrafana())
    try:
        yield
    finally:
        reset_upstream(token)


@pytest.mark.asyncio
async def test_runner_walks_to_conclusion():
    app = build_application(tracking=False)

    walk = [
        (
            "start_investigation",
            {
                "incident_description": "5xx spike across services",
                "scenario_time": "2026-05-24T14:00:00Z",
            },
        ),
        ("query_metrics", {"promql": "sum(rate(http_5xx[5m]))"}),
        ("query_logs", {"logql": '{job="payment"} |= "error"'}),
        ("advance_phase", {"to": "diagnose", "rationale": "payment leads the error rate"}),
        ("advance_phase", {"to": "verify", "rationale": "metrics and logs agree"}),
        ("query_metrics", {"promql": "pool_in_use{service='payment'}"}),
        (
            "conclude",
            {
                "primary_service": "payment-service",
                "cascade_services": ["order-service"],
                "root_cause": "connection pool exhaustion at 14:02",
                "final_answer": (
                    "Primary payment-service exhausted its connection pool at 14:02; "
                    "order-service cascaded downstream. Metrics and logs agree."
                ),
            },
        ),
    ]

    for action, inputs in walk:
        obs = await runner.step_fsm(app, action, inputs)
        assert obs.get("error") is None, f"{action} failed: {obs}"

    assert runner.fsm_terminated(app)
    assert app.state["investigation_summary"]["primary_service"] == "payment-service"


@pytest.mark.asyncio
async def test_runner_refuses_out_of_order():
    app = build_application(tracking=False)
    obs = await runner.step_fsm(
        app, "conclude", {"primary_service": "x", "root_cause": "y", "final_answer": "z"}
    )
    assert obs["error"] == "invalid_transition"
    assert obs["valid_next_actions"] == ["start_investigation"]
