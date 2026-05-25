"""Replay the canonical investigation walk into a tracked session at a human
pace, so `theodosia watch` renders the FSM advancing live. No LLM, no network:
a fixed action sequence stepped through the same step_fsm the Harbor runner
uses, against a mock upstream, with a pause between steps for the recording.

    python scripts/playback.py        # writes a tracked session, paced
    theodosia watch -p phoebe       # in another pane, tails it live
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from theodosia.upstream import bind_upstream, reset_upstream

from phoebe.app import build_application
from phoebe.harbor.agent_runner import step_fsm

_STEP_PAUSE_S = 1.4


class _Mock:
    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        if tool == "list_datasources":
            return [
                {"uid": "prometheus", "type": "prometheus"},
                {"uid": "loki", "type": "loki"},
                {"uid": "tempo", "type": "tempo"},
            ]
        if tool == "list_prometheus_metric_names":
            return ["http_requests_total", "http_request_duration_seconds_bucket", "up"]
        if tool == "list_prometheus_label_names":
            return ["job", "status", "instance"]
        if tool == "list_prometheus_label_values":
            return ["payment-service", "order-service", "user-service"]
        if tool == "list_loki_label_names":
            return ["job", "service", "level"]
        if tool == "query_prometheus":
            return {"series": [{"job": "payment-service", "5xx": "rising 06:48"}]}
        if tool == "query_loki_logs":
            return {"lines": ["06:48 payment-service ERROR connection pool exhausted"]}
        return {"ok": True, "tool": tool}


_WALK: list[tuple[str, dict[str, Any]]] = [
    (
        "start_investigation",
        {"incident_description": "5xx spike across services", "scenario_time": "2026-05-25T06:50:00Z"},
    ),
    ("query_metrics", {"promql": 'sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))'}),
    ("query_logs", {"logql": '{service="payment-service"} |= "error"'}),
    # a deliberately repeated probe, refused by the loop guard, to show a refusal:
    ("query_logs", {"logql": '{service="payment-service"} |= "error"'}),
    ("advance_phase", {"to": "diagnose", "rationale": "payment-service leads the 5xx rate"}),
    ("advance_phase", {"to": "verify", "rationale": "metrics and logs agree on payment-service"}),
    ("query_metrics", {"promql": 'sum by (job,status) (rate(http_requests_total[5m]))'}),
    (
        "conclude",
        {
            "primary_service": "payment-service",
            "cascade_services": ["order-service"],
            "root_cause": "connection pool exhaustion around 06:48",
            "final_answer": (
                "Primary payment-service exhausted its connection pool around 06:48; "
                "order-service cascaded downstream. Metrics and logs agree on the timing."
            ),
        },
    ),
]


async def main() -> None:
    token = bind_upstream(_Mock())
    try:
        app = build_application(tracking=True)
        for action, inputs in _WALK:
            await step_fsm(app, action, inputs)
            time.sleep(_STEP_PAUSE_S)
    finally:
        reset_upstream(token)


if __name__ == "__main__":
    asyncio.run(main())
