"""o11y-fsm Burr Application: SRE incident investigation, circe-style.

Design (learned from circe, github project "madeline/circe"):

* **The operation IS the FSM action.** ``query_metrics`` / ``query_logs``
  / ``query_traces`` actually run the telemetry query (through a bound
  ``TelemetryClient``) and record the evidence in one step. There is no
  separate "do work elsewhere, then report it" surface; that split is
  what made the earlier v0.1 design loop weak models.

* **Phase is a state variable, not a graph node.** ``phase`` moves
  ``triage -> diagnose -> verify`` via ``advance_phase(to, rationale)``.
  Operations and ``conclude`` consult it.

* **Hub topology + body-level gating.** Every operational action is
  reachable from every other (broad reachability). The methodology is
  enforced inside action bodies (``advance_phase`` requires probes;
  ``conclude`` requires phase==verify + >=2 backends + a verifying probe),
  not by narrowing graph reachability. The agent is never told "no" by
  the graph for a normal operation; only by a body raising ValueError
  with a specific, recoverable reason.

* **Loop guard.** The same ``(backend, query)`` within a short window is
  refused ("vary the probe"), so a weak model can't burn its budget
  re-running one query.

The query actions reach the backend through ``telemetry.get_telemetry_client``;
bind one with ``telemetry.bind_telemetry_client(...)`` before stepping
(the Harbor runner binds a Grafana-MCP-backed client; tests bind a
``MockTelemetryClient``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition

from o11y_fsm import prompts
from o11y_fsm.telemetry import require_telemetry_client

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _probe_hash(backend: str, query: str) -> str:
    return f"{backend}::{query.strip()}"


def _distinct_backends(probes: list[dict[str, Any]]) -> set[str]:
    return {p["backend"] for p in probes if p.get("backend")}


async def _run_probe(
    state: State[Any],
    *,
    backend: str,
    query: str,
    hypothesis: str | None,
) -> State[Any]:
    """Shared body for the three query_* actions.

    Executes the query through the bound telemetry client, records the
    probe (with the phase it ran in, so post-verify verification probes
    can be counted), and enforces the loop guard.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    backend = _BACKEND_ALIASES.get((backend or "").strip().lower(), (backend or "").strip().lower())

    recent = state.get("recent_probe_hashes") or []
    h = _probe_hash(backend, query)
    if h in recent[-_LOOP_GUARD_WINDOW:]:
        raise ValueError(
            f"loop guard: this exact {backend} query ran within the last "
            f"{_LOOP_GUARD_WINDOW} probes. Vary the probe (different query, "
            f"different backend) or, if you have enough evidence, "
            f"advance_phase / conclude."
        )

    client = require_telemetry_client()
    result = await client.query(backend, query)
    ok = bool(result.get("ok", True))
    summary = str(result.get("summary", "")).strip() or "(no summary)"

    phase = state.get("phase") or _DEFAULT_PHASE
    probe = {
        "backend": backend,
        "query": query,
        "ok": ok,
        "summary": summary,
        "hypothesis": (hypothesis or "").strip(),
        "phase": phase,
        "ts": _now(),
    }
    probes = [*(state.get("probes") or []), probe]
    distinct = sorted(_distinct_backends(probes))
    next_recent = [*recent, h][-(_LOOP_GUARD_WINDOW * 2) :]

    prompt = prompts.after_probe(
        backend=backend,
        summary=summary,
        phase=phase,
        distinct_backends=distinct,
        n_probes=len(probes),
    )
    return state.update(
        probes=probes,
        distinct_backends=distinct,
        recent_probe_hashes=next_recent,
        current_prompt=prompt,
        log=[*state["log"], f"probe {backend}: {summary[:60]}"],
    )


# == actions ==========================================================


@action(
    reads=[],
    writes=[
        "incident_description",
        "scenario_time",
        "phase",
        "phase_history",
        "probes",
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
        probes=[],
        distinct_backends=[],
        recent_probe_hashes=[],
        hypothesis=None,
        final_answer=None,
        investigation_summary=None,
        current_prompt=prompts.after_start(incident_description.strip(), scenario_time),
        log=[f"investigation started; scenario_time={scenario_time!r}"],
    )


@action(
    reads=["phase", "probes", "distinct_backends", "recent_probe_hashes", "log"],
    writes=["probes", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_metrics(
    state: State[Any], promql: str, hypothesis: str | None = None
) -> State[Any]:
    """Run a Prometheus (metrics) query and record the evidence.

    Args:
        promql: the PromQL query string.
        hypothesis: optional short reason for this probe.
    """
    return await _run_probe(state, backend="prometheus", query=promql, hypothesis=hypothesis)


@action(
    reads=["phase", "probes", "distinct_backends", "recent_probe_hashes", "log"],
    writes=["probes", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_logs(state: State[Any], logql: str, hypothesis: str | None = None) -> State[Any]:
    """Run a Loki (logs) query and record the evidence.

    Args:
        logql: the LogQL query string.
        hypothesis: optional short reason for this probe.
    """
    return await _run_probe(state, backend="loki", query=logql, hypothesis=hypothesis)


@action(
    reads=["phase", "probes", "distinct_backends", "recent_probe_hashes", "log"],
    writes=["probes", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_traces(
    state: State[Any], traceql: str, hypothesis: str | None = None
) -> State[Any]:
    """Run a Tempo (traces) query and record the evidence.

    Args:
        traceql: the TraceQL query string.
        hypothesis: optional short reason for this probe.
    """
    return await _run_probe(state, backend="tempo", query=traceql, hypothesis=hypothesis)


@action(
    reads=["phase", "phase_history", "probes", "distinct_backends", "log"],
    writes=["phase", "phase_history", "current_prompt", "log"],
)
async def advance_phase(state: State[Any], to: str, rationale: str) -> State[Any]:
    """Advance the investigation phase. Gated on evidence.

    Phases:
      - ``triage``   initial context gathering (default).
      - ``diagnose`` working a hypothesis; needs >=1 probe.
      - ``verify``   confirming the leading hypothesis; needs probes from
        >=2 distinct backends (you can't conclude a cross-cutting incident
        from a single signal).

    The hard gate for the final answer lives on ``conclude``; this action
    just enforces that you don't skip the investigation the phase implies.
    """
    to = (to or "").strip().lower()
    if to not in _PHASES:
        raise ValueError(f"phase must be one of {list(_PHASES)}; got {to!r}")
    if not rationale.strip():
        raise ValueError("advance_phase requires a non-empty rationale")

    probes = state.get("probes") or []
    distinct = _distinct_backends(probes)
    if to == "diagnose" and len(probes) < 1:
        raise ValueError(
            "advance_phase(to='diagnose') requires at least 1 probe first. "
            "Run a query_metrics / query_logs / query_traces to gather evidence."
        )
    if to == "verify" and len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"advance_phase(to='verify') requires probes from at least "
            f"{_MIN_BACKENDS_TO_CONCLUDE} distinct backends; you have "
            f"{sorted(distinct)}. Cross-reference at least one more backend "
            f"(e.g. logs if you've only queried metrics) before verifying."
        )

    prev = state.get("phase") or _DEFAULT_PHASE
    history = [
        *(state.get("phase_history") or []),
        {"from": prev, "to": to, "rationale": rationale.strip(), "ts": _now()},
    ]
    return state.update(
        phase=to,
        phase_history=history,
        current_prompt=prompts.after_advance(to, sorted(distinct), len(probes)),
        log=[*state["log"], f"phase {prev} -> {to}: {rationale.strip()[:60]}"],
    )


@action(
    reads=[
        "incident_description",
        "phase",
        "probes",
        "distinct_backends",
        "log",
    ],
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

    Requires:
      - phase == ``verify`` (you advanced through the methodology),
      - probes from >= 2 distinct backends,
      - at least one probe taken DURING the verify phase (the verification
        step: you confirmed the leading hypothesis, not just asserted it).

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
            "Call advance_phase(to='verify', rationale=...) once you have "
            "cross-referenced evidence and a leading hypothesis."
        )
    probes = state.get("probes") or []
    distinct = _distinct_backends(probes)
    if len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"conclude requires probes from >= {_MIN_BACKENDS_TO_CONCLUDE} distinct "
            f"backends; you have {sorted(distinct)}."
        )
    verify_probes = [p for p in probes if p.get("phase") == "verify"]
    if not verify_probes:
        raise ValueError(
            "conclude requires at least one probe taken during the verify phase. "
            "Run a focused query that confirms (or refutes) your leading "
            "hypothesis, then conclude."
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
        "n_probes": len(probes),
        "n_verify_probes": len(verify_probes),
        "final_answer_chars": len(final_answer),
    }
    return state.update(
        hypothesis=hypothesis,
        final_answer=final_answer.strip(),
        investigation_summary=summary,
        current_prompt="Investigation complete. Final answer in state.final_answer.",
        log=[*state["log"], f"concluded: primary={primary!r}; {len(probes)} probe(s)"],
    )


# == graph (hub topology) =============================================

_HUB = ("query_metrics", "query_logs", "query_traces", "advance_phase", "conclude")

# Every transition carries an explicit condition. Burr only rejects
# multiple *unconditional* (default) transitions from one source; giving
# each an explicit condition makes the hub legal. ``_OPEN`` is true while
# the investigation hasn't concluded, so all hub actions are reachable
# until conclude sets final_answer (after which nothing is, => terminal).
# Methodology gating lives in the action bodies, not here (circe-style).
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
        tracking: wire a ``LocalTrackingClient`` for Burr UI + ``burrmcp
            sessions`` visibility. Default True. Set False in minimal
            environments (the Harbor container has no compiler for
            psutil, pulled transitively by the tracking extra).
    """
    builder = (
        ApplicationBuilder()
        .with_actions(
            start_investigation=start_investigation,
            query_metrics=query_metrics,
            query_logs=query_logs,
            query_traces=query_traces,
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
            probes=[],
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
    """Mount the o11y-fsm application as an MCP server (burrmcp lazy-imported)."""
    from burrmcp import ServingMode, mount

    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="o11y-fsm",
        instructions=(
            "Observability / SRE incident-investigation FSM, circe-style: "
            "the query actions ARE the operations. Walk: "
            "start_investigation(incident_description, scenario_time=None), "
            "then query_metrics(promql) / query_logs(logql) / query_traces(traceql) "
            "to gather evidence (each runs the query and records it), "
            "advance_phase(to, rationale) to move triage->diagnose->verify, "
            "and conclude(primary_service, root_cause, final_answer, cascade_services=[]) "
            "to finish. conclude is gated: phase must be 'verify', you need probes "
            "from >=2 distinct backends, and at least one probe during verify. "
            "Repeated identical probes are refused (vary the probe). Read "
            "state.current_prompt after each step; burr://history for the trail. "
            "Requires a telemetry client bound (Grafana MCP in the Harbor runner)."
        ),
    )


if __name__ == "__main__":
    build_server().run()
