"""o11y-fsm Burr Application: SRE incident investigation over Grafana, via Theodosia upstream.

Single surface: the agent sees ONLY the FSM's actions. The query actions
drive Grafana's MCP server through Theodosia (``call_upstream("grafana",
...)``): the FSM owns the Grafana plumbing (datasource discovery, time
windows, query types) and exposes a clean ``query_metrics(promql)`` /
``query_logs(logql)`` / ``query_traces(traceql)`` to the agent. Every query
happens inside an action, so it advances state (ledger). Driving the
telemetry through the FSM action, rather than a separate query surface, is
what lets a mid-size model walk the investigation to a conclusion.

Phases (state variable): triage -> diagnose -> verify.
Actions: start_investigation, query_metrics, query_logs, query_traces,
advance_phase, conclude.

Requires an upstream "grafana" server bound (mount(upstream={"grafana":...})
standalone, or the Harbor runner binding its Grafana session). Tests bind
a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from theodosia import call_upstream

from o11y_fsm import prompts

_TRACKER_PROJECT = "o11y-fsm"
_PHASES = ("triage", "diagnose", "verify")
_DEFAULT_PHASE = "triage"
_LOOP_GUARD_WINDOW = 4
_MIN_BACKENDS_TO_CONCLUDE = 2
_DEFAULT_LOOKBACK_HOURS = 6


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(scenario_time: str) -> tuple[str, str]:
    """(start, end) RFC3339 around the scenario clock; default last 6h."""
    try:
        end = datetime.fromisoformat(scenario_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        end = datetime.now(UTC)
    start = end - timedelta(hours=_DEFAULT_LOOKBACK_HOURS)
    return _rfc3339(start), _rfc3339(end)


def _pick_uid(datasources: Any, *types: str) -> str | None:
    """Find a datasource uid whose type matches one of ``types``."""
    items = datasources
    if isinstance(datasources, dict):
        items = datasources.get("datasources") or datasources.get("result") or []
    if not isinstance(items, list):
        return None
    for ds in items:
        if isinstance(ds, dict) and str(ds.get("type", "")).lower() in types:
            return ds.get("uid") or ds.get("id")
    return None


def _distinct_backends(findings: list[dict[str, Any]]) -> set[str]:
    return {f["backend"] for f in findings if f.get("backend")}


def _summarize(result: Any, limit: int = 300) -> str:
    s = result if isinstance(result, str) else __import__("json").dumps(result, default=str)
    return s[:limit]


async def _record(
    state: State[Any], *, backend: str, query: str, result: Any, hypothesis: str | None
) -> State[Any]:
    phase = state.get("phase") or _DEFAULT_PHASE
    summary = _summarize(result)
    finding = {
        "backend": backend,
        "query": query,
        "result_summary": summary,
        "hypothesis": (hypothesis or "").strip(),
        "phase": phase,
        "ts": _now(),
    }
    findings = [*(state.get("findings") or []), finding]
    distinct = sorted(_distinct_backends(findings))
    recent = state.get("recent_probe_hashes") or []
    return state.update(
        findings=findings,
        distinct_backends=distinct,
        recent_probe_hashes=[*recent, f"{backend}::{query.strip()}"][-(_LOOP_GUARD_WINDOW * 2) :],
        current_prompt=prompts.after_probe(
            backend=backend,
            summary=summary,
            phase=phase,
            distinct_backends=distinct,
            n_probes=len(findings),
        ),
        log=[*state["log"], f"{backend} probe: {summary[:60]}"],
    )


def _loop_guard(state: State[Any], backend: str, query: str) -> None:
    recent = state.get("recent_probe_hashes") or []
    if f"{backend}::{query.strip()}" in recent[-_LOOP_GUARD_WINDOW:]:
        raise ValueError(
            f"loop guard: this exact {backend} query ran within the last "
            f"{_LOOP_GUARD_WINDOW} probes. Vary it, or advance_phase / conclude."
        )


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
        "ds_uids",
        "window",
        "hypothesis",
        "final_answer",
        "investigation_summary",
        "current_prompt",
        "log",
    ],
)
async def start_investigation(
    state: State[Any], incident_description: str, scenario_time: str | None = None
) -> State[Any]:
    """Open the investigation. Discovers Grafana datasources (so later
    queries hit the right uid) and sets the time window from the scenario
    clock. Phase starts at triage.
    """
    if not incident_description.strip():
        raise ValueError("incident_description must not be empty")
    scenario_time = (scenario_time or _now()).strip()
    start, end = _window(scenario_time)
    try:
        ds = await call_upstream("grafana", "list_datasources", {})
        uids = {
            "prometheus": _pick_uid(ds, "prometheus"),
            "loki": _pick_uid(ds, "loki"),
            "tempo": _pick_uid(ds, "tempo"),
        }
    except Exception as e:  # noqa: BLE001 (surface as recoverable state, not a crash)
        uids = {"prometheus": None, "loki": None, "tempo": None, "_error": str(e)}
    return state.update(
        incident_description=incident_description.strip(),
        scenario_time=scenario_time,
        phase=_DEFAULT_PHASE,
        phase_history=[],
        findings=[],
        distinct_backends=[],
        recent_probe_hashes=[],
        ds_uids=uids,
        window={"start": start, "end": end},
        hypothesis=None,
        final_answer=None,
        investigation_summary=None,
        current_prompt=prompts.after_start(incident_description.strip(), scenario_time),
        log=[f"investigation started; datasources={ {k: bool(v) for k, v in uids.items()} }"],
    )


@action(
    reads=[
        "phase",
        "findings",
        "distinct_backends",
        "recent_probe_hashes",
        "ds_uids",
        "window",
        "log",
    ],
    writes=["findings", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_metrics(
    state: State[Any], promql: str, hypothesis: str | None = None
) -> State[Any]:
    """Run a PromQL query against Grafana (through Theodosia) and record it.

    Args:
        promql: the PromQL expression.
        hypothesis: optional short reason for this probe.
    """
    promql = (promql or "").strip()
    if not promql:
        raise ValueError("promql must not be empty")
    _loop_guard(state, "prometheus", promql)
    uid = (state.get("ds_uids") or {}).get("prometheus")
    win = state.get("window") or {}
    result = await call_upstream(
        "grafana",
        "query_prometheus",
        {
            "datasourceUid": uid,
            "expr": promql,
            "queryType": "range",
            "startTime": win.get("start"),
            "endTime": win.get("end"),
            "stepSeconds": 300,
        },
    )
    return await _record(
        state, backend="prometheus", query=promql, result=result, hypothesis=hypothesis
    )


@action(
    reads=[
        "phase",
        "findings",
        "distinct_backends",
        "recent_probe_hashes",
        "ds_uids",
        "window",
        "log",
    ],
    writes=["findings", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_logs(state: State[Any], logql: str, hypothesis: str | None = None) -> State[Any]:
    """Run a LogQL query against Grafana (through Theodosia) and record it.

    Args:
        logql: the LogQL query.
        hypothesis: optional short reason for this probe.
    """
    logql = (logql or "").strip()
    if not logql:
        raise ValueError("logql must not be empty")
    _loop_guard(state, "loki", logql)
    uid = (state.get("ds_uids") or {}).get("loki")
    win = state.get("window") or {}
    result = await call_upstream(
        "grafana",
        "query_loki_logs",
        {
            "datasourceUid": uid,
            "logql": logql,
            "startRfc3339": win.get("start"),
            "endRfc3339": win.get("end"),
            "limit": 50,
        },
    )
    return await _record(state, backend="loki", query=logql, result=result, hypothesis=hypothesis)


@action(
    reads=[
        "phase",
        "findings",
        "distinct_backends",
        "recent_probe_hashes",
        "ds_uids",
        "window",
        "log",
    ],
    writes=["findings", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def query_traces(
    state: State[Any], traceql: str, hypothesis: str | None = None
) -> State[Any]:
    """Run a TraceQL search against Grafana (through Theodosia) and record it.

    Args:
        traceql: the TraceQL query.
        hypothesis: optional short reason for this probe.
    """
    traceql = (traceql or "").strip()
    if not traceql:
        raise ValueError("traceql must not be empty")
    _loop_guard(state, "tempo", traceql)
    uid = (state.get("ds_uids") or {}).get("tempo")
    win = state.get("window") or {}
    result = await call_upstream(
        "grafana",
        "tempo_traceql-search",
        {
            "datasourceUid": uid,
            "query": traceql,
            "start": win.get("start"),
            "end": win.get("end"),
        },
    )
    return await _record(
        state, backend="tempo", query=traceql, result=result, hypothesis=hypothesis
    )


@action(
    reads=["phase", "phase_history", "findings", "distinct_backends", "log"],
    writes=["phase", "phase_history", "current_prompt", "log"],
)
async def advance_phase(state: State[Any], to: str, rationale: str) -> State[Any]:
    """Advance triage -> diagnose -> verify. to='diagnose' needs >=1 finding;
    to='verify' needs findings from >=2 distinct backends."""
    to = (to or "").strip().lower()
    if to not in _PHASES:
        raise ValueError(f"phase must be one of {list(_PHASES)}; got {to!r}")
    if not rationale.strip():
        raise ValueError("advance_phase requires a non-empty rationale")
    findings = state.get("findings") or []
    distinct = _distinct_backends(findings)
    if to == "diagnose" and len(findings) < 1:
        raise ValueError("advance_phase(to='diagnose') requires at least 1 finding first.")
    if to == "verify" and len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"advance_phase(to='verify') requires findings from >= "
            f"{_MIN_BACKENDS_TO_CONCLUDE} distinct backends; you have {sorted(distinct)}."
        )
    prev = state.get("phase") or _DEFAULT_PHASE
    return state.update(
        phase=to,
        phase_history=[
            *(state.get("phase_history") or []),
            {"from": prev, "to": to, "rationale": rationale.strip(), "ts": _now()},
        ],
        current_prompt=prompts.after_advance(to, sorted(distinct), len(findings)),
        log=[*state["log"], f"phase {prev} -> {to}"],
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
    """Terminal. Requires phase=='verify', findings from >=2 backends, and a
    finding recorded during verify."""
    phase = state.get("phase") or _DEFAULT_PHASE
    if phase != "verify":
        raise ValueError(f"conclude requires phase=='verify'; current phase is {phase!r}.")
    findings = state.get("findings") or []
    distinct = _distinct_backends(findings)
    if len(distinct) < _MIN_BACKENDS_TO_CONCLUDE:
        raise ValueError(
            f"conclude requires >= {_MIN_BACKENDS_TO_CONCLUDE} distinct backends; have {sorted(distinct)}."
        )
    if not any(f.get("phase") == "verify" for f in findings):
        raise ValueError("conclude requires a finding recorded during the verify phase.")
    primary = (primary_service or "").strip()
    if not primary:
        raise ValueError("primary_service must not be empty (use 'unknown' if unclear)")
    if not root_cause.strip():
        raise ValueError("root_cause must not be empty")
    if len(final_answer.strip()) < 80:
        raise ValueError("final_answer must be a substantive markdown response (>=80 chars).")
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
        "final_answer_chars": len(final_answer),
    }
    return state.update(
        hypothesis=hypothesis,
        final_answer=final_answer.strip(),
        investigation_summary=summary,
        current_prompt="Investigation complete. Final answer in state.final_answer.",
        log=[*state["log"], f"concluded: primary={primary!r}"],
    )


# == graph (hub) ======================================================

_HUB = ("query_metrics", "query_logs", "query_traces", "advance_phase", "conclude")
_OPEN = Condition.expr("final_answer is None")


def _hub_transitions() -> list[tuple[str, str, Condition]]:
    ts: list[tuple[str, str, Condition]] = [("start_investigation", a, _OPEN) for a in _HUB]
    for src in _HUB:
        if src == "conclude":
            continue
        ts.extend((src, dst, _OPEN) for dst in _HUB)
    return ts


def build_application(tracking: bool = True):
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
        from theodosia import tracker

        builder = builder.with_tracker(tracker(project=_TRACKER_PROJECT))
    return (
        builder.with_state(
            incident_description="",
            scenario_time="",
            phase=_DEFAULT_PHASE,
            phase_history=[],
            findings=[],
            distinct_backends=[],
            recent_probe_hashes=[],
            ds_uids={},
            window={},
            hypothesis=None,
            final_answer=None,
            investigation_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_investigation")
        .build()
    )


def build_server(grafana_mcp_url: str | None = None):
    """Mount o11y-fsm, driving Grafana through Theodosia upstream (single surface).

    Args:
        grafana_mcp_url: URL of the Grafana MCP server to drive. Defaults to
            $GRAFANA_MCP_URL. The agent connects only to this server; Grafana
            is reached through it.
    """
    import os

    from theodosia import ServingMode, mount

    url = grafana_mcp_url or os.environ.get("GRAFANA_MCP_URL", "http://127.0.0.1:8080/mcp")
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="o11y-fsm",
        upstream={"grafana": url},
        instructions=(
            "SRE incident-investigation FSM that drives a Grafana MCP server "
            "THROUGH this server (you are not given Grafana tools directly). "
            "Walk: start_investigation(incident_description, scenario_time) -> "
            "query_metrics(promql) / query_logs(logql) / query_traces(traceql) "
            "[the FSM runs these against Grafana and records them] -> "
            "advance_phase(to, rationale) triage->diagnose->verify -> "
            "conclude(primary_service, root_cause, final_answer, cascade_services). "
            "conclude needs phase=='verify', >=2 backends, and a verify-phase "
            "finding. Read state.current_prompt after each step."
        ),
    )


if __name__ == "__main__":
    build_server().run()
