"""o11y-fsm v0.3 (external_tools federation): the FSM records findings the
agent brings back from the connected Grafana MCP server; it does not run
queries itself. Phase is a state variable; gating lives in action bodies;
hub topology.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from o11y_fsm import build_server

_INCIDENT = "error rates jumped across services; triage primary vs cascade."


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


async def _two_backends(client):
    await _step(
        client,
        "record_finding",
        backend="prometheus",
        query="sum(rate(http_5xx[5m]))",
        result_summary="payment-service 5xx spiking from 14:02",
    )
    await _step(
        client,
        "record_finding",
        backend="loki",
        query='{job="payment"} |= "error"',
        result_summary="connection pool exhausted errors at 14:02",
    )


# == start + external_tools surfacing =================================


@pytest.mark.asyncio
async def test_start_rejects_empty_incident():
    async with Client(build_server()) as client:
        out = _payload(await _step(client, "start_investigation", incident_description=""))
        assert out["error"] == "action_error"


@pytest.mark.asyncio
async def test_step_surfaces_next_external_tools():
    """The FSM dogfoods burrmcp external_tools: record_finding's Grafana
    tools appear in next_external_tools after start."""
    async with Client(build_server()) as client:
        out = _payload(await _step(client, "start_investigation", incident_description=_INCIDENT))
        assert out["state"]["phase"] == "triage"
        net = out.get("next_external_tools")
        assert net is not None
        assert "query_prometheus" in net.get("record_finding", [])


@pytest.mark.asyncio
async def test_graph_resource_declares_external_tools():
    async with Client(build_server()) as client:
        graph = json.loads((await client.read_resource("burr://graph"))[0].text)
        by = {a["name"]: a for a in graph["actions"]}
        assert "query_prometheus" in by["record_finding"]["external_tools"]
        assert "external_tools" not in by["advance_phase"]  # none declared


# == record_finding ===================================================


@pytest.mark.asyncio
async def test_record_finding_tracks_backends():
    async with Client(build_server()) as client:
        await _start(client)
        out = _payload(
            await _step(
                client,
                "record_finding",
                backend="metrics",
                query="up",
                result_summary="all targets healthy except payment",
            )
        )
        assert "error" not in out
        assert "prometheus" in out["state"]["distinct_backends"]  # metrics alias


@pytest.mark.asyncio
async def test_record_finding_rejects_thin_summary():
    async with Client(build_server()) as client:
        await _start(client)
        out = _payload(
            await _step(client, "record_finding", backend="loki", query="x", result_summary="hi")
        )
        assert out["error"] == "action_error"
        assert "result_summary too thin" in out["error_message"]


@pytest.mark.asyncio
async def test_loop_guard_refuses_repeated_finding():
    async with Client(build_server()) as client:
        await _start(client)
        await _step(
            client, "record_finding", backend="prometheus", query="up", result_summary="healthy ok"
        )
        out = _payload(
            await _step(
                client,
                "record_finding",
                backend="prometheus",
                query="up",
                result_summary="healthy ok",
            )
        )
        assert out["error"] == "action_error"
        assert "loop guard" in out["error_message"]


# == gating ===========================================================


@pytest.mark.asyncio
async def test_advance_verify_requires_two_backends():
    async with Client(build_server()) as client:
        await _start(client)
        await _step(
            client,
            "record_finding",
            backend="prometheus",
            query="up",
            result_summary="payment 5xx high",
        )
        out = _payload(await _step(client, "advance_phase", to="verify", rationale="x"))
        assert out["error"] == "action_error"
        assert "distinct backends" in out["error_message"]


@pytest.mark.asyncio
async def test_conclude_requires_verify_phase_finding():
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
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
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
        await _step(client, "advance_phase", to="diagnose", rationale="payment looks primary")
        await _step(client, "advance_phase", to="verify", rationale="confirm pool exhaustion")
        await _step(
            client,
            "record_finding",
            backend="prometheus",
            query="pool_in_use{service='payment'}",
            result_summary="pool maxed 14:02-14:08, confirms",
        )
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
        assert summary["n_verify_findings"] >= 1
        assert out["valid_next_actions"] == []


@pytest.mark.asyncio
async def test_history_records_steps():
    async with Client(build_server()) as client:
        await _start(client)
        await _two_backends(client)
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == ["start_investigation", "record_finding", "record_finding"]
