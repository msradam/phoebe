"""o11y-fsm v0.2 (circe-style): action bodies own the queries; phase is a
state variable; gating lives in action bodies; hub topology.

A MockTelemetryClient is bound per test so the query_* actions have a
backend. The "agent" is simulated by feeding inputs into each step and
reading state / valid_next_actions afterward.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from o11y_fsm import build_server
from o11y_fsm.telemetry import MockTelemetryClient, bind_telemetry_client

_INCIDENT = "error rates jumped across services; triage primary vs cascade."


@pytest.fixture(autouse=True)
def _bind_mock_client():
    """Every test gets a mock telemetry backend bound for the duration."""
    token = bind_telemetry_client(MockTelemetryClient())
    try:
        yield
    finally:
        from o11y_fsm.telemetry import _CLIENT

        _CLIENT.reset(token)


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


async def _start(client):
    await _step(
        client,
        "start_investigation",
        incident_description=_INCIDENT,
        scenario_time="2026-05-24T14:00Z",
    )


async def _probe_two_backends(client):
    await _step(client, "query_metrics", promql="sum(rate(http_5xx[5m]))")
    await _step(client, "query_logs", logql='{job="payment"} |= "error"')


# == start_investigation ==============================================


@pytest.mark.asyncio
async def test_start_rejects_empty_incident():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_investigation", incident_description=""))
        assert out["error"] == "action_error"
        assert "incident_description must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_start_sets_triage_phase_and_opens_hub():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_investigation", incident_description=_INCIDENT))
        assert out["state"]["phase"] == "triage"
        # Hub: all operational actions reachable after start.
        for a in ("query_metrics", "query_logs", "query_traces", "advance_phase", "conclude"):
            assert a in out["valid_next_actions"]


# == query actions own the telemetry ==================================


@pytest.mark.asyncio
async def test_query_metrics_records_probe_via_client():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "query_metrics", promql="up"))
        assert "error" not in out
        probes = out["state"]["probes"]
        assert len(probes) == 1
        assert probes[0]["backend"] == "prometheus"
        assert "prometheus" in out["state"]["distinct_backends"]


@pytest.mark.asyncio
async def test_query_rejects_empty_query():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "query_logs", logql="   "))
        assert out["error"] == "action_error"
        assert "query must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_loop_guard_refuses_repeated_probe():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "query_metrics", promql="up")
        out = _payload(await _step(client, "query_metrics", promql="up"))
        assert out["error"] == "action_error"
        assert "loop guard" in out["error_message"]


# == advance_phase gating =============================================


@pytest.mark.asyncio
async def test_advance_to_diagnose_requires_a_probe():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "advance_phase", to="diagnose", rationale="go"))
        assert out["error"] == "action_error"
        assert "at least 1 probe" in out["error_message"]


@pytest.mark.asyncio
async def test_advance_to_verify_requires_two_backends():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "query_metrics", promql="up")
        out = _payload(await _step(client, "advance_phase", to="verify", rationale="x"))
        assert out["error"] == "action_error"
        assert "distinct backends" in out["error_message"]


@pytest.mark.asyncio
async def test_advance_rejects_unknown_phase():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        out = _payload(await _step(client, "advance_phase", to="party", rationale="x"))
        assert out["error"] == "action_error"
        assert "phase must be one of" in out["error_message"]


# == conclude gating ==================================================


@pytest.mark.asyncio
async def test_conclude_refused_before_verify_phase():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _probe_two_backends(client)
        out = _payload(
            await _step(
                client,
                "conclude",
                primary_service="payment",
                root_cause="pool exhaustion at 14:02 in payment-service",
                final_answer="x" * 100,
            )
        )
        assert out["error"] == "action_error"
        assert "phase=='verify'" in out["error_message"]


@pytest.mark.asyncio
async def test_conclude_requires_verify_phase_probe():
    """Reaching verify isn't enough; a probe must run DURING verify."""
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _probe_two_backends(client)
        await _step(client, "advance_phase", to="verify", rationale="cross-referenced")
        out = _payload(
            await _step(
                client,
                "conclude",
                primary_service="payment",
                root_cause="pool exhaustion at 14:02",
                final_answer="x" * 100,
            )
        )
        assert out["error"] == "action_error"
        assert "during the verify phase" in out["error_message"]


@pytest.mark.asyncio
async def test_happy_path_end_to_end():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _probe_two_backends(client)  # prometheus + loki
        await _step(client, "advance_phase", to="diagnose", rationale="payment looks primary")
        await _step(client, "advance_phase", to="verify", rationale="confirm pool exhaustion")
        # verification probe DURING verify phase
        await _step(client, "query_metrics", promql="pool_in_use{service='payment'}")
        out = _payload(
            await _step(
                client,
                "conclude",
                primary_service="payment-service",
                cascade_services=["order-service"],
                root_cause="connection-pool exhaustion at 14:02 under burst traffic",
                final_answer=(
                    "# Triage\n\nPrimary: payment-service pool exhaustion at 14:02. "
                    "order-service cascade. Metrics + logs agree."
                ),
            )
        )
        assert "error" not in out
        summary = out["state"]["investigation_summary"]
        assert summary["primary_service"] == "payment-service"
        assert sorted(summary["distinct_backends"]) == ["loki", "prometheus"]
        assert summary["n_verify_probes"] >= 1
        # Terminal: nothing reachable.
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_auto_registers_third_backend():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _step(client, "query_traces", traceql='{ span.name = "checkout" }')
        out = _payload(await _step(client, "query_metrics", promql="up"))
        assert sorted(out["state"]["distinct_backends"]) == ["prometheus", "tempo"]


# == audit trail ======================================================


@pytest.mark.asyncio
async def test_history_records_steps():
    server = build_server()
    async with Client(server) as client:
        await _start(client)
        await _probe_two_backends(client)
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == ["start_investigation", "query_metrics", "query_logs"]
