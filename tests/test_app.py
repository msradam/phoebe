"""o11y-fsm: action-level validation + transition-gate enforcement.

Pure orchestration tests — no LLM, no actual MCP server. The agent is
simulated by feeding canned arguments into each step's inputs and
reading state.current_prompt + valid_next_actions afterward.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from o11y_fsm import build_server


async def _step(client, action, **inputs):
    return await client.call_tool("step", {"action": action, "inputs": inputs})


def _payload(result):
    return result.structured_content


_INCIDENT = (
    "We had a noisy incident window a few hours ago - error rates jumped "
    "across services. Triage which is primary vs cascade."
)


# == start_investigation ==============================================


@pytest.mark.asyncio
async def test_start_investigation_rejects_empty_description():
    server = build_server()
    async with Client(server) as client:
        out = _payload(await _step(client, "start_investigation", incident_description=""))
        assert out["error"] == "action_error"
        assert "incident_description must not be empty" in out["error_message"]


@pytest.mark.asyncio
async def test_start_investigation_emits_survey_prompt():
    server = build_server()
    async with Client(server) as client:
        out = _payload(
            await _step(
                client,
                "start_investigation",
                incident_description=_INCIDENT,
                scenario_time="2026-05-24T14:00:00Z",
            )
        )
        assert "error" not in out
        assert out["valid_next_actions"] == ["survey_telemetry"]
        assert "SURVEY TELEMETRY" in out["state"]["current_prompt"]
        assert "2026-05-24T14:00:00Z" in out["state"]["current_prompt"]


# == survey_telemetry ================================================


@pytest.mark.asyncio
async def test_survey_telemetry_refuses_empty_backends():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_investigation", incident_description=_INCIDENT)
        out = _payload(await _step(client, "survey_telemetry", available_backends=[]))
        assert out["error"] == "action_error"


@pytest.mark.asyncio
async def test_survey_telemetry_normalizes_and_deduplicates():
    server = build_server()
    async with Client(server) as client:
        await _step(client, "start_investigation", incident_description=_INCIDENT)
        out = _payload(
            await _step(
                client,
                "survey_telemetry",
                available_backends=["Prometheus", "loki", "PROMETHEUS"],
                notable_services=["payment-service"],
                time_window="last 6 hours",
            )
        )
        assert out["state"]["available_backends"] == ["prometheus", "loki"]
        assert out["state"]["notable_services"] == ["payment-service"]
        assert "GATHER EVIDENCE" in out["state"]["current_prompt"]


# == gather_evidence + the >=2-backend gate ==========================


async def _walk_to_gather(client):
    await _step(client, "start_investigation", incident_description=_INCIDENT)
    await _step(
        client, "survey_telemetry", available_backends=["prometheus", "loki"]
    )


@pytest.mark.asyncio
async def test_gather_evidence_rejects_unsurveyed_backend():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_gather(client)
        out = _payload(
            await _step(
                client,
                "gather_evidence",
                backend="tempo",
                queries=[{"query": "x", "result_summary": "y"}],
            )
        )
        assert out["error"] == "action_error"
        assert "not in surveyed available_backends" in out["error_message"]


@pytest.mark.asyncio
async def test_gather_evidence_requires_query_records():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_gather(client)
        out = _payload(
            await _step(client, "gather_evidence", backend="prometheus", queries=[])
        )
        assert out["error"] == "action_error"


@pytest.mark.asyncio
async def test_gather_evidence_validates_query_shape():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_gather(client)
        out = _payload(
            await _step(
                client,
                "gather_evidence",
                backend="prometheus",
                queries=[{"query": "x"}],  # missing result_summary
            )
        )
        assert out["error"] == "action_error"
        assert "result_summary" in out["error_message"]


@pytest.mark.asyncio
async def test_correlate_unreachable_with_one_backend():
    """The load-bearing gate: <2 backends covered → correlate refused."""
    server = build_server()
    async with Client(server) as client:
        await _walk_to_gather(client)
        out = _payload(
            await _step(
                client,
                "gather_evidence",
                backend="prometheus",
                queries=[{"query": "up", "result_summary": "all green"}],
            )
        )
        assert "correlate" not in out["valid_next_actions"]
        assert "gather_evidence" in out["valid_next_actions"]
        # And explicitly: trying to correlate is invalid_transition.
        out = _payload(
            await _step(
                client,
                "correlate",
                impacted_services=["x"],
                time_window="now",
                evidence_summary="x" * 50,
            )
        )
        assert out["error"] == "invalid_transition"


@pytest.mark.asyncio
async def test_correlate_opens_when_two_backends_covered():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_gather(client)
        await _step(
            client,
            "gather_evidence",
            backend="prometheus",
            queries=[{"query": "up", "result_summary": "all green"}],
        )
        out = _payload(
            await _step(
                client,
                "gather_evidence",
                backend="loki",
                queries=[{"query": '{job="x"}', "result_summary": "many errors"}],
            )
        )
        assert "correlate" in out["valid_next_actions"]
        assert sorted(out["state"]["covered_backends"]) == ["loki", "prometheus"]


# == correlate validation ============================================


async def _walk_to_correlate(client):
    await _walk_to_gather(client)
    await _step(
        client,
        "gather_evidence",
        backend="prometheus",
        queries=[{"query": "up", "result_summary": "all green"}],
    )
    await _step(
        client,
        "gather_evidence",
        backend="loki",
        queries=[{"query": '{job="x"}', "result_summary": "many errors"}],
    )


@pytest.mark.asyncio
async def test_correlate_refuses_thin_summary():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_correlate(client)
        out = _payload(
            await _step(
                client,
                "correlate",
                impacted_services=["payment-service"],
                time_window="last hour",
                evidence_summary="thin",
            )
        )
        assert out["error"] == "action_error"
        assert "substantive paragraph" in out["error_message"]


# == hypothesis + verify-or-revise routing ===========================


async def _walk_to_hypothesis(client):
    await _walk_to_correlate(client)
    await _step(
        client,
        "correlate",
        impacted_services=["payment-service", "order-service"],
        time_window="14:00 to 14:30",
        evidence_summary=(
            "Prom shows payment-service 5xx spiking at 14:02; Loki shows "
            "order-service timeouts cascading from 14:04. Time-aligned across both."
        ),
    )


@pytest.mark.asyncio
async def test_form_hypothesis_validates_confidence():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        out = _payload(
            await _step(
                client,
                "form_hypothesis",
                primary_service="payment-service",
                root_cause="connection-pool exhaustion at 14:02",
                confidence="extremely-confident",  # not a valid value
            )
        )
        assert out["error"] == "action_error"
        assert "confidence must be one of" in out["error_message"]


@pytest.mark.asyncio
async def test_verify_disconfirmed_loops_back_to_hypothesis():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        await _step(
            client,
            "form_hypothesis",
            primary_service="payment-service",
            root_cause="connection-pool exhaustion at 14:02",
            cascade_services=["order-service"],
            confidence="medium",
        )
        out = _payload(
            await _step(
                client,
                "verify_or_revise",
                verification_query='rate(connection_pool_exhausted{service="payment-service"}[5m])',
                result_summary="no exhaustion events found in the window; metric is flat zero",
                confirmed=False,
                revised_root_cause="upstream auth-service intermittent failures",
            )
        )
        assert "error" not in out
        assert out["valid_next_actions"] == ["form_hypothesis"]


@pytest.mark.asyncio
async def test_verify_disconfirmed_requires_revised_root_cause():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        await _step(
            client,
            "form_hypothesis",
            primary_service="payment-service",
            root_cause="pool exhaustion",
            confidence="medium",
        )
        out = _payload(
            await _step(
                client,
                "verify_or_revise",
                verification_query="x",
                result_summary="y",
                confirmed=False,
                # no revised_root_cause
            )
        )
        assert out["error"] == "action_error"
        assert "revised_root_cause" in out["error_message"]


@pytest.mark.asyncio
async def test_verify_confirmed_opens_recommendations():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        await _step(
            client,
            "form_hypothesis",
            primary_service="payment-service",
            root_cause="pool exhaustion at 14:02",
            confidence="high",
        )
        out = _payload(
            await _step(
                client,
                "verify_or_revise",
                verification_query="pool_in_use{service=payment-service}",
                result_summary="maxed out at 14:02-14:08 window, confirms",
                confirmed=True,
            )
        )
        assert out["valid_next_actions"] == ["recommend_next_steps"]
        assert "RECOMMEND" in out["state"]["current_prompt"]


# == terminal ========================================================


@pytest.mark.asyncio
async def test_recommend_refuses_thin_final_answer():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        await _step(
            client,
            "form_hypothesis",
            primary_service="payment-service",
            root_cause="pool exhaustion",
            confidence="high",
        )
        await _step(
            client,
            "verify_or_revise",
            verification_query="x",
            result_summary="confirms",
            confirmed=True,
        )
        out = _payload(
            await _step(
                client,
                "recommend_next_steps",
                recommendations=[{"action": "scale connection pool"}],
                final_answer="too short",
            )
        )
        assert out["error"] == "action_error"
        assert "substantive markdown response" in out["error_message"]


@pytest.mark.asyncio
async def test_happy_path_walks_end_to_end():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_hypothesis(client)
        await _step(
            client,
            "form_hypothesis",
            primary_service="payment-service",
            cascade_services=["order-service"],
            root_cause="connection-pool exhaustion at 14:02 triggered by burst traffic",
            confidence="high",
        )
        await _step(
            client,
            "verify_or_revise",
            verification_query="pool_in_use",
            result_summary="confirms exhaustion in the window",
            confirmed=True,
        )
        out = _payload(
            await _step(
                client,
                "recommend_next_steps",
                recommendations=[
                    {
                        "action": "scale payment-service connection pool from 50 to 100",
                        "owner": "payments-team",
                        "evidence_ref": "verify: pool_in_use maxed 14:02-14:08",
                    },
                    {
                        "action": "add p99 latency alert on order-service checkout path",
                        "owner": "order-team",
                        "evidence_ref": "correlate: cascade visible in Loki at 14:04",
                    },
                ],
                final_answer=(
                    "# Incident triage\n\n"
                    "Primary: **payment-service** connection-pool exhaustion at 14:02. "
                    "order-service is a downstream cascade (timeouts visible at 14:04). "
                    "Recommended actions follow."
                ),
            )
        )
        assert "error" not in out
        summary = out["state"]["investigation_summary"]
        assert summary["primary_service"] == "payment-service"
        assert summary["n_recommendations"] == 2
        assert sorted(summary["covered_backends"]) == ["loki", "prometheus"]


# == audit trail ====================================================


@pytest.mark.asyncio
async def test_history_records_each_phase():
    server = build_server()
    async with Client(server) as client:
        await _walk_to_correlate(client)
        history = json.loads((await client.read_resource("burr://history"))[0].text)
        actions = [h["action"] for h in history]
        assert actions == [
            "start_investigation",
            "survey_telemetry",
            "gather_evidence",
            "gather_evidence",
        ]
