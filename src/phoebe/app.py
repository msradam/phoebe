"""phoebe Burr Application: SRE incident investigation over Grafana.

The FSM is an invariant, not a toolbox. It does not narrow the agent's tools;
the agent has the full Grafana toolset (the runner passes the real Grafana MCP
tools through). What the FSM enforces is the procedure: phases
(triage -> diagnose -> verify), a cross-reference gate, and a terminal that
cannot fire early. Every tool call is recorded as evidence via record_probe so
the audit trail and the gate stay honest, but which tool the agent calls is the
agent's choice.

Actions: start_investigation (open the case, discover datasources/schema),
record_probe (one recorded Grafana call; the runner executes the real tool),
advance_phase (triage -> diagnose -> verify), conclude (gated terminal).

This is the open, general investigation invariant, the stepping stone, not a
fully integrated product. Requires an upstream "grafana" server bound
(mount(upstream={"grafana":...}) standalone, or the Harbor runner binding its
Grafana session). Tests bind a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from theodosia import call_upstream

from phoebe import prompts

_TRACKER_PROJECT = "phoebe"
_PHASES = ("triage", "diagnose", "verify")
_DEFAULT_PHASE = "triage"
_LOOP_GUARD_WINDOW = 4
_MIN_BACKENDS_TO_CONCLUDE = 2
_DEFAULT_LOOKBACK_HOURS = 6
# Probe budget, advisory only. The toolset is open and probing is never
# refused; this is the telemetry count at which after_probe begins nudging the
# model to wrap up. Enforcement lives in the procedure (phase order, mandatory
# verify probe, the conclude gate), not in the probe count. A binding cap here
# removed exploration the model needs on broad blast-radius incidents and drove
# the FSM below raw prompting. See record_probe.
_PROBE_BUDGET_TOTAL = 12


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


def _as_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        for key in ("data", "result", "values", "labels"):
            if isinstance(val.get(key), list):
                return val[key]
    return []


async def _discover_schema(uids: dict[str, Any]) -> dict[str, Any]:
    """Pull the real metric names, label names, job values, and Loki labels
    from Grafana so the agent queries against actual names instead of guessing.
    Tolerant: any failed lookup is simply omitted."""

    async def _safe(tool: str, args: dict[str, Any]) -> Any:
        try:
            return await call_upstream("grafana", tool, args)
        except Exception:  # noqa: BLE001 (a missing lookup is not fatal)
            return None

    schema: dict[str, Any] = {}
    prom = uids.get("prometheus")
    loki = uids.get("loki")
    if prom:
        schema["metrics"] = _as_list(
            await _safe("list_prometheus_metric_names", {"datasourceUid": prom})
        )
        schema["prometheus_labels"] = _as_list(
            await _safe("list_prometheus_label_names", {"datasourceUid": prom})
        )
        schema["jobs"] = _as_list(
            await _safe("list_prometheus_label_values", {"datasourceUid": prom, "labelName": "job"})
        )
    if loki:
        schema["loki_labels"] = _as_list(
            await _safe("list_loki_label_names", {"datasourceUid": loki})
        )
    return schema


_TELEMETRY_BACKENDS = ("prometheus", "loki", "tempo")


def _distinct_backends(findings: list[dict[str, Any]]) -> set[str]:
    # Only the three telemetry backends count toward the cross-reference gate.
    # Dashboard/annotation/alerting calls are recorded but do not satisfy
    # "evidence from >= 2 distinct backends".
    return {f["backend"] for f in findings if f.get("backend") in _TELEMETRY_BACKENDS}


def _summarize(result: Any, limit: int = 1200) -> str:
    s = result if isinstance(result, str) else __import__("json").dumps(result, default=str)
    return s[:limit]


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
        "schema",
        "window",
        "hypothesis",
        "primary_service",
        "root_cause",
        "cascade_services",
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
    schema = await _discover_schema(uids)
    return state.update(
        incident_description=incident_description.strip(),
        scenario_time=scenario_time,
        phase=_DEFAULT_PHASE,
        phase_history=[],
        findings=[],
        distinct_backends=[],
        recent_probe_hashes=[],
        ds_uids=uids,
        schema=schema,
        window={"start": start, "end": end},
        hypothesis=None,
        primary_service=None,
        root_cause=None,
        cascade_services=[],
        final_answer=None,
        investigation_summary=None,
        current_prompt=prompts.after_start(incident_description.strip(), scenario_time, schema),
        log=[f"investigation started; datasources={ {k: bool(v) for k, v in uids.items()} }"],
    )


@action(
    reads=["phase", "findings", "distinct_backends", "recent_probe_hashes", "log"],
    writes=["findings", "distinct_backends", "recent_probe_hashes", "current_prompt", "log"],
)
async def record_probe(
    state: State[Any],
    tool: str,
    backend: str | None = None,
    query: str = "",
    result_summary: str = "",
    hypothesis: str | None = None,
) -> State[Any]:
    """Record one Grafana tool call as evidence.

    The agent has the full Grafana toolset; the runner executes the actual tool
    and records the call here so the audit trail and the cross-reference gate
    stay honest. The FSM does not restrict which tool is called, only that
    conclude cannot fire until the phase gates are met.

    Args:
        tool: the Grafana tool that was called (e.g. ``query_prometheus``).
        backend: ``prometheus`` / ``loki`` / ``tempo`` when the call hit a
            telemetry datasource, else ``None`` (counts toward the >=2 backend
            gate only for the telemetry backends).
        query: a short representation of the call arguments.
        result_summary: the (already summarized) result text.
        hypothesis: optional short reason for this probe.
    """
    tool = (tool or "").strip()
    if not tool:
        raise ValueError("record_probe requires a tool name")
    be = (backend or "").strip().lower() or None
    if be not in (None, *_TELEMETRY_BACKENDS):
        be = None
    phase = state.get("phase") or _DEFAULT_PHASE
    existing = state.get("findings") or []
    # The probe budget is advisory, not enforcement. We count telemetry probes
    # (Prometheus / Loki / Tempo; discovery calls carry backend=None and don't
    # count) and surface the count in current_prompt to nudge the model toward
    # wrapping up, but we never refuse a probe. A binding cap removes the
    # exploration the model needs on broad blast-radius incidents (covering five
    # services costs more than a flat cap allows) and drove the FSM below raw
    # prompting. The enforcement lives in the procedure (phase order, mandatory
    # verify probe, the conclude gate), not in the probe count.
    telemetry = [f for f in existing if f.get("backend") in _TELEMETRY_BACKENDS]
    over_budget = (be in _TELEMETRY_BACKENDS) and len(telemetry) >= _PROBE_BUDGET_TOTAL
    key = f"{be or tool}::{(query or '').strip()}"
    recent = state.get("recent_probe_hashes") or []
    if key in recent[-_LOOP_GUARD_WINDOW:]:
        raise ValueError(
            f"loop guard: this exact {tool} call ran within the last "
            f"{_LOOP_GUARD_WINDOW} probes. Vary it, or advance_phase / conclude."
        )
    summary = _summarize(result_summary)
    finding = {
        "backend": be,
        "tool": tool,
        "query": query,
        "result_summary": summary,
        "hypothesis": (hypothesis or "").strip(),
        "phase": phase,
        "ts": _now(),
    }
    findings = [*(state.get("findings") or []), finding]
    distinct = sorted(_distinct_backends(findings))
    return state.update(
        findings=findings,
        distinct_backends=distinct,
        recent_probe_hashes=[*recent, key][-(_LOOP_GUARD_WINDOW * 2) :],
        current_prompt=prompts.after_probe(
            backend=be or tool,
            summary=summary,
            phase=phase,
            distinct_backends=distinct,
            n_probes=len(findings),
            over_budget=over_budget,
        ),
        log=[*state["log"], f"{tool} probe: {summary[:60]}"],
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
    writes=[
        "hypothesis",
        "primary_service",
        "root_cause",
        "cascade_services",
        "final_answer",
        "investigation_summary",
        "current_prompt",
        "log",
    ],
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
        primary_service=primary,
        root_cause=hypothesis["root_cause"],
        cascade_services=hypothesis["cascade_services"],
        final_answer=final_answer.strip(),
        investigation_summary=summary,
        current_prompt="Investigation complete. Final answer in state.final_answer.",
        log=[*state["log"], f"concluded: primary={primary!r}"],
    )


# == graph (hub) ======================================================

_HUB = ("record_probe", "advance_phase", "conclude")
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
            record_probe=record_probe,
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
            schema={},
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
    """Mount phoebe, driving Grafana through Theodosia upstream (single surface).

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
        name="phoebe",
        upstream={"grafana": url},
        instructions=(
            "SRE incident-investigation invariant. Walk: "
            "start_investigation(incident_description, scenario_time), then gather "
            "evidence, then advance_phase(to, rationale) triage->diagnose->verify, "
            "then conclude(primary_service, root_cause, final_answer, cascade_services). "
            "conclude needs phase=='verify', findings from >=2 telemetry backends, "
            "and a probe recorded during verify. Read state.current_prompt after "
            "each step. The full Grafana toolset is exposed by the Harbor runner; "
            "record_probe logs each tool call as evidence."
        ),
    )


if __name__ == "__main__":
    build_server().run()
