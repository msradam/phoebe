"""o11y-fsm Burr Application: SRE incident investigation via external_tools federation.

Design (v0.3): the FSM does NOT proxy telemetry queries. It declares,
per phase, which tools on the connected Grafana MCP server are relevant
(via ``mount(external_tools=...)``), and records the findings the agent
brings back. The agent calls Grafana's real tools natively (no bespoke
proxy / arg-mapping in the FSM), reads ``next_external_tools`` off each
step response, and ``step()``s here to record + advance.

This dogfoods burrmcp's external_tools feature in the flagship demo: the
Burr graph is the conductor; Grafana's MCP server is the orchestra.

Phases (state variable): triage -> diagnose -> verify.

Actions:
  start_investigation(incident_description, scenario_time)
  record_finding(backend, query, result_summary, hypothesis)   [loops]
  advance_phase(to, rationale)
  conclude(primary_service, root_cause, final_answer, cascade_services)

Gating (in action bodies, circe-style; see [[fsm-single-surface-lesson]]):
  - advance_phase to diagnose: >=1 finding.
  - advance_phase to verify: findings from >=2 distinct backends.
  - conclude: phase==verify, >=2 backends, >=1 finding recorded during verify.
  - loop guard: same (backend, query) within a short window is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from o11y_fsm import prompts

_TRACKER_PROJECT = "o11y-fsm"

_PHASES = ("triage", "diagnose", "verify")
_DEFAULT_PHASE = "triage"
_BACKEND_ALIASES = {
    "metrics": "prometheus",
    "prom": "prometheus",
    "prometheus": "prometheus",
    "logs": "loki",
    "loki": "loki",
    "traces": "tempo",
    "tempo": "tempo",
}
_LOOP_GUARD_WINDOW = 4
_MIN_BACKENDS_TO_CONCLUDE = 2

# Which Grafana MCP tools are relevant per phase. Surfaced via
# mount(external_tools=...) as next_external_tools on each step response.
# Soft guidance: the agent has the full Grafana tool surface available;
# these scope its choice to what matters for the reachable actions.
EXTERNAL_TOOLS = {
    "start_investigation": ["list_datasources", "list_prometheus_metric_names"],
    "record_finding": [
        "query_prometheus",
        "query_loki_logs",
        "query_loki_stats",
        "list_loki_label_names",
        "query_tempo_traces",
        "list_prometheus_metric_names",
        "list_prometheus_label_names",
    ],
    "conclude": ["query_prometheus", "query_loki_logs"],
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _probe_hash(backend: str, query: str) -> str:
    return f"{backend}::{query.strip()}"


def _distinct_backends(findings: list[dict[str, Any]]) -> set[str]:
    return {f["backend"] for f in findings if f.get("backend")}


# == actions ==========================================================


@action(
    reads=[],
    writes=[
        "incident_description",
        "scenario_time",
        "phase",
        "phase_history",
        "findings",
        "distinct_backends",
        "recent_probe_hashes",
        "hypothesis",
        "final_answer",
        "investigation_summary",
        "current_prompt",
        "log",
    ],
)
async def start_investigation(
    state: State[Any],
    incident_description: str,
    scenario_time: str | None = None,
) -> State[Any]:
    """Open the investigation. Phase starts at ``triage``.

    Args:
        incident_description: the incident statement, verbatim.
        scenario_time: optional ISO anchor for the scenario clock.
    """
    if not incident_description.strip():
        raise ValueError("incident_description must not be empty")
    scenario_time = (scenario_time or "now").strip()
    return state.update(
        incident_description=incident_description.strip(),
        scenario_time=scenario_time,
        phase=_DEFAULT_PHASE,
        phase_history=[],
        findings=[],
        distinct_backends=[],
        recent_probe_hashes=[],
        hypothesis=None,
        final_answer=None,
        investigation_summary=None,
        current_prompt=prompts.after_start(incident_description.strip(), scenario_time),
        log=[f"investigation started; scenario_time={scenario_time!r}"],
    )


@action(
    reads=["phase", "findings", "distinct_backends", "recent_probe_hashes", "log"],
    writes=["findings", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def record_finding(
    state: State[Any],
    backend: str,
    query: str,
    result_summary: str,
    hypothesis: str | None = None,
) -> State[Any]:
    """Record one finding from a Grafana query the agent already ran.

    Call this AFTER using a Grafana MCP tool (query_prometheus,
    query_loki_logs, query_tempo_traces, ...) named in next_external_tools.
    The FSM does not run the query; it records what you found and gates
    progression on the evidence.

    Args:
        backend: which backend the query hit ("prometheus" / "loki" /
            "tempo"; metrics/logs/traces aliases accepted).
        query: the query string you ran.
        result_summary: 1-3 sentences on what the result showed.
        hypothesis: optional short reason this probe mattered.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    if len(result_summary.strip()) < 10:
        raise ValueError(
            "result_summary too thin; summarize what the Grafana query actually returned."
        )
    backend = _BACKEND_ALIASES.get((backend or "").strip().lower(), (backend or "").strip().lower())
    if not backend:
        raise ValueError("backend must be a non-empty backend name")

    recent = state.get("recent_probe_hashes") or []
    h = _probe_hash(backend, query)
    if h in recent[-_LOOP_GUARD_WINDOW:]:
        raise ValueError(
            f"loop guard: this exact {backend} query was already recorded within "
            f"the last {_LOOP_GUARD_WINDOW} findings. Vary the query / backend, "
            f"or advance_phase / conclude if you have enough evidence."
        )

    phase = state.get("phase") or _DEFAULT_PHASE
    finding = {
        "backend": backend,
        "query": query,
        "result_summary": result_summary.strip(),
        "hypothesis": (hypothesis or "").strip(),
        "phase": phase,
        "ts": _now(),
    }
    findings = [*(state.get("findings") or []), finding]
    distinct = sorted(_distinct_backends(findings))
    next_recent = [*recent, h][-(_LOOP_GUARD_WINDOW * 2) :]
    return state.update(
        findings=findings,
        distinct_backends=distinct,
        recent_probe_hashes=next_recent,
        current_prompt=prompts.after_probe(
            backend=backend,
            summary=result_summary.strip(),
            phase=phase,
            distinct_backends=distinct,
            n_probes=len(findings),
        ),
        log=[*state["log"], f"finding {backend}: {result_summary.strip()[:60]}"],
    )


@action(
    reads=["phase", "phase_history", "findings", "distinct_backends", "log"],
    writes=["phase", "phase_history", "current_prompt", "log"],
)
async def advance_phase(state: State[Any], to: str, rationale: str) -> State[Any]:
    """Advance the investigation phase: triage -> diagnose -> verify.

    Gated: to='diagnose' needs >=1 finding; to='verify' needs findings
    from >=2 distinct backends.
    """
    to = (to or "").strip().lower()
    if to not in _PHASES:
        raise ValueError(f"phase must be one of {list(_PHASES)}; got {to!r}")
    if not rationale.strip():
        raise ValueError("advance_phase requires a non-empty rationale")

    findings = state.get("findings") or []
    distinct = _distinct_backends(findings)
    if to == "diagnose" and len(findings) < 1:
        raise ValueError(
            "advance_phase(to='diagnose') requires at least 1 finding first. "
            "Use a Grafana tool then record_finding."
        )
    if to == "verify" and len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"advance_phase(to='verify') requires findings from at least "
            f"{_MIN_BACKENDS_TO_CONCLUDE} distinct backends; you have "
            f"{sorted(distinct)}. Cross-reference another backend first."
        )

    prev = state.get("phase") or _DEFAULT_PHASE
    history = [
        *(state.get("phase_history") or []),
        {"from": prev, "to": to, "rationale": rationale.strip(), "ts": _now()},
    ]
    return state.update(
        phase=to,
        phase_history=history,
        current_prompt=prompts.after_advance(to, sorted(distinct), len(findings)),
        log=[*state["log"], f"phase {prev} -> {to}: {rationale.strip()[:60]}"],
    )


@action(
    reads=["incident_description", "phase", "findings", "distinct_backends", "log"],
    writes=["hypothesis", "final_answer", "investigation_summary", "current_prompt", "log"],
)
async def conclude(
    state: State[Any],
    primary_service: str,
    root_cause: str,
    final_answer: str,
    cascade_services: list[str] | None = None,
) -> State[Any]:
    """Terminal. Commit the conclusion + final answer. Gated.

    Requires phase=='verify', findings from >=2 distinct backends, and at
    least one finding recorded during the verify phase.

    Args:
        primary_service: the single service judged the root cause
            (or "unknown" with justification in root_cause).
        root_cause: 1-2 sentences naming the most likely root cause.
        final_answer: full markdown response the operator/grader reads.
        cascade_services: services impacted as downstream consequences.
    """
    phase = state.get("phase") or _DEFAULT_PHASE
    if phase != "verify":
        raise ValueError(
            f"conclude requires phase=='verify'; current phase is {phase!r}. "
            "advance_phase(to='verify', rationale=...) once you have "
            "cross-referenced evidence and a leading hypothesis."
        )
    findings = state.get("findings") or []
    distinct = _distinct_backends(findings)
    if len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"conclude requires findings from >= {_MIN_BACKENDS_TO_CONCLUDE} distinct "
            f"backends; you have {sorted(distinct)}."
        )
    verify_findings = [f for f in findings if f.get("phase") == "verify"]
    if not verify_findings:
        raise ValueError(
            "conclude requires at least one finding recorded during the verify "
            "phase. Run a focused Grafana query that confirms (or refutes) your "
            "leading hypothesis, record_finding it, then conclude."
        )
    primary = (primary_service or "").strip()
    if not primary:
        raise ValueError("primary_service must not be empty (use 'unknown' if truly unclear)")
    if not root_cause.strip():
        raise ValueError("root_cause must not be empty")
    if len(final_answer.strip()) < 80:
        raise ValueError(
            "final_answer must be a substantive markdown response (>=80 chars); "
            "the grader reads it verbatim."
        )

    hypothesis = {
        "primary_service": primary,
        "cascade_services": [s.strip() for s in (cascade_services or []) if s and s.strip()],
        "root_cause": root_cause.strip(),
    }
    summary = {
        "incident_description": state["incident_description"],
        "primary_service": primary,
        "cascade_services": hypothesis["cascade_services"],
        "root_cause": hypothesis["root_cause"],
        "distinct_backends": sorted(distinct),
        "n_findings": len(findings),
        "n_verify_findings": len(verify_findings),
        "final_answer_chars": len(final_answer),
    }
    return state.update(
        hypothesis=hypothesis,
        final_answer=final_answer.strip(),
        investigation_summary=summary,
        current_prompt="Investigation complete. Final answer in state.final_answer.",
        log=[*state["log"], f"concluded: primary={primary!r}; {len(findings)} finding(s)"],
    )


# == graph (hub topology) =============================================

_HUB = ("record_finding", "advance_phase", "conclude")
_OPEN = Condition.expr("final_answer is None")


def _hub_transitions() -> list[tuple[str, str, Condition]]:
    transitions: list[tuple[str, str, Condition]] = [
        ("start_investigation", a, _OPEN) for a in _HUB
    ]
    for src in _HUB:
        if src == "conclude":
            continue
        for dst in _HUB:
            transitions.append((src, dst, _OPEN))
    return transitions


def build_application(tracking: bool = True):
    """Build the o11y-fsm Burr Application.

    Args:
        tracking: wire a LocalTrackingClient for Burr UI + burrmcp sessions
            visibility. Default True; set False in minimal environments
            (the Harbor container lacks a compiler for the tracking extra's
            transitive psutil).
    """
    builder = (
        ApplicationBuilder()
        .with_actions(
            start_investigation=start_investigation,
            record_finding=record_finding,
            advance_phase=advance_phase,
            conclude=conclude,
        )
        .with_transitions(*_hub_transitions())
    )
    if tracking:
        from burr.tracking.client import LocalTrackingClient

        builder = builder.with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
    return (
        builder.with_state(
            incident_description="",
            scenario_time="",
            phase=_DEFAULT_PHASE,
            phase_history=[],
            findings=[],
            distinct_backends=[],
            recent_probe_hashes=[],
            hypothesis=None,
            final_answer=None,
            investigation_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_investigation")
        .build()
    )


def build_server():
    """Mount o11y-fsm as an MCP server, declaring Grafana tools per phase."""
    from burrmcp import ServingMode, mount

    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="o11y-fsm",
        external_tools=EXTERNAL_TOOLS,
        instructions=(
            "Observability / SRE incident-investigation FSM. This server holds "
            "no telemetry tools; it orchestrates the connected Grafana MCP "
            "server. Each step response carries next_external_tools naming the "
            "Grafana tools relevant for the reachable actions. Walk: "
            "start_investigation(incident_description, scenario_time=None) -> "
            "[call a Grafana tool, then record_finding(backend, query, "
            "result_summary)] looped across >=2 backends -> "
            "advance_phase(to, rationale) triage->diagnose->verify -> "
            "conclude(primary_service, root_cause, final_answer, cascade_services). "
            "conclude is gated: phase=='verify', >=2 backends, and a finding "
            "recorded during verify. Read state.current_prompt after each step."
        ),
    )


if __name__ == "__main__":
    build_server().run()
