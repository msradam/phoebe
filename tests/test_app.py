"""o11y-fsm: single surface, drives Grafana through Theodosia upstream.

The query actions call call_upstream("grafana", ...); tests bind a mock
upstream so no real Grafana is needed. Phase is a state variable; gating
lives in action bodies; hub topology.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from theodosia import ServingMode, mount
from theodosia.upstream import bind_upstream, reset_upstream

from o11y_fsm.app import build_application


def build_server():
    # Mount WITHOUT upstream so the fixture's mock (bound on the ContextVar)
    # is the upstream. The real upstream wiring is covered by the live smoke.
    return mount(build_application, mode=ServingMode.STEP, name="o11y-fsm")


_INCIDENT = "error rates jumped across services; triage primary vs cascade."


class _MockGrafana:
    """Stands in for the Grafana MCP server bound as upstream."""

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        assert server == "grafana"
        if tool == "list_datasources":
            return [
                {"uid": "prom-1", "type": "prometheus", "name": "Prometheus"},
                {"uid": "loki-1", "type": "loki", "name": "Loki"},
                {"uid": "tempo-1", "type": "tempo", "name": "Tempo"},
            ]
        if tool == "list_prometheus_metric_names":
            return ["http_requests_total", "http_request_duration_seconds_bucket", "up"]
        if tool == "list_prometheus_label_names":
            return ["__name__", "job", "status", "instance"]
        if tool == "list_prometheus_label_values":
            return ["payment-service", "order-service"]
        if tool == "list_loki_label_names":
            return ["job", "service", "level", "service_name"]
        if tool == "query_prometheus":
            return {"data": f"series for {args.get('expr')}", "uid": args.get("datasourceUid")}
        if tool == "query_loki_logs":
            return {"streams": f"logs for {args.get('logql')}", "uid": args.get("datasourceUid")}
        if tool == "tempo_traceql-search":
            return {"traces": f"traces for {args.get('query')}"}
        return {"_unhandled": tool}


@pytest.fixture(autouse=True)
def _bind_mock():
    token = bind_upstream(_MockGrafana())
    try:
        yield
    finally:
        reset_upstream(token)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _p(result):
    return result.structured_content


async def _start(client):
    await _step(
        client,
        "start_investigation",
        incident_description=_INCIDENT,
        scenario_time="2026-05-24T14:00:00Z",
    )


async def _two_backends(client):
    await _step(client, "query_metrics", promql="sum(rate(http_5xx[5m]))")
    await _step(client, "query_logs", logql='{job="payment"} |= "error"')


@pytest.mark.asyncio
async def test_start_discovers_datasources():
    async with Client(build_server()) as client:
        out = _p(
            await _step(
                client,
                "start_investigation",
                incident_description=_INCIDENT,
                scenario_time="2026-05-24T14:00:00Z",
            )
        )
        assert "error" not in out
        assert out["state"]["phase"] == "triage"
        assert out["state"]["ds_uids"]["prometheus"] == "prom-1"
        assert out["state"]["window"]["end"] == "2026-05-24T14:00:00Z"


@pytest.mark.asyncio
async def test_start_rejects_empty_incident():
    async with Client(build_server()) as client:
        out = _p(await _step(client, "start_investigation", incident_description=""))
        assert out["error"] == "action_error"


@pytest.mark.asyncio
async def test_query_metrics_drives_grafana_with_uid():
    async with Client(build_server()) as client:
        await _start(client)
        out = _p(await _step(client, "query_metrics", promql="up"))
        assert "error" not in out
        f = out["state"]["findings"][-1]
        assert f["backend"] == "prometheus"
        assert "prom-1" in f["result_summary"]  # uid threaded into the query


@pytest.mark.asyncio
async def test_loop_guard():
    async with Client(build_server()) as client:
        await _start(client)
        await _step(client, "query_metrics", promql="up")
        out = _p(await _step(client, "query_metrics", promql="up"))
        assert out["error"] == "action_error"
        assert "loop guard" in out["error_message"]


@pytest.mark.asyncio
async def test_advance_verify_requires_two_backends():
    async with Client(build_server()) as client:
        await _start(client)
        await _step(client, "query_metrics", promql="up")
        out = _p(await _step(client, "advance_phase", to="verify", rationale="x"))
        assert out["error"] == "action_error"
        assert "distinct backends" in out["error_message"]


@pytest.mark.asyncio
async def test_conclude_requires_verify_finding():
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
        await _step(client, "advance_phase", to="verify", rationale="cross-referenced")
        out = _p(
            await _step(
                client,
                "conclude",
                primary_service="payment",
                root_cause="pool exhaustion",
                final_answer="x" * 100,
            )
        )
        assert out["error"] == "action_error"
        assert "verify phase" in out["error_message"]


@pytest.mark.asyncio
async def test_happy_path():
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
        await _step(client, "advance_phase", to="diagnose", rationale="payment primary")
        await _step(client, "advance_phase", to="verify", rationale="confirm")
        await _step(client, "query_metrics", promql="pool_in_use{service='payment'}")
        out = _p(
            await _step(
                client,
                "conclude",
                primary_service="payment-service",
                cascade_services=["order-service"],
                root_cause="pool exhaustion at 14:02",
                final_answer=(
                    "# Incident triage\n\nPrimary: payment-service connection-pool exhaustion "
                    "at 14:02. order-service is a downstream cascade. Metrics and logs agree."
                ),
            )
        )
        assert "error" not in out
        s = out["state"]["investigation_summary"]
        assert s["primary_service"] == "payment-service"
        assert sorted(s["distinct_backends"]) == ["loki", "prometheus"]
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_history_records_steps():
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
        history = json.loads((await client.read_resource("theodosia://history"))[0].text)
        assert [h["action"] for h in history] == [
            "start_investigation",
            "query_metrics",
            "query_logs",
        ]
