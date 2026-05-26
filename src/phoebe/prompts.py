"""Prompt fragments emitted into ``state.current_prompt`` after each step.

Experiment A: enforce completion and verification structure, not investigative
content. The prompts tell the agent it must gather evidence, verify a leading
hypothesis, and conclude (never trail off). They do NOT prescribe how to
investigate (no per-service blast-radius script, no cross-reference mandate, no
deployment hint); that channels the agent below a free agent's exploration. The
graph enforces order, a verify-phase probe, and termination; the agent
investigates freely within that.
"""

from __future__ import annotations

from typing import Any


def _trunc(items: list[Any], n: int) -> str:
    shown = ", ".join(str(x) for x in items[:n])
    return shown + (" ..." if len(items) > n else "")


def schema_block(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    lines: list[str] = []
    if schema.get("metrics"):
        lines.append(f"- Prometheus metrics: {_trunc(schema['metrics'], 20)}")
    if schema.get("prometheus_labels"):
        lines.append(f"- Prometheus labels: {_trunc(schema['prometheus_labels'], 15)}")
    if schema.get("jobs"):
        lines.append(f"- job label values: {_trunc(schema['jobs'], 15)}")
    if schema.get("loki_labels"):
        lines.append(f"- Loki labels: {_trunc(schema['loki_labels'], 15)}")
    if not lines:
        return ""
    return (
        "Discovered telemetry schema (query these exact names; do not invent metric "
        "or label names):\n" + "\n".join(lines) + "\n\n"
    )


def after_start(
    incident_description: str, scenario_time: str, schema: dict[str, Any] | None = None
) -> str:
    return (
        f"Investigating: {incident_description}\n"
        f"Scenario clock: {scenario_time}\n\n"
        f"{schema_block(schema)}"
        "Phase: TRIAGE. Investigate freely with the Grafana tools (Prometheus, "
        "Loki, Tempo). Each call is recorded as evidence; read the 'result' field "
        "on each response and quantify from it.\n\n"
        "When you have a leading hypothesis, advance_phase(to='diagnose', "
        "rationale=...), then advance_phase(to='verify', ...), run one probe that "
        "confirms or refutes it, and conclude(primary_service, root_cause, "
        "final_answer, cascade_services=[]). You must reach conclude; do not end "
        "the session by trailing off. conclude is blocked until a probe is recorded "
        "during the verify phase."
    )


def after_probe(
    *,
    backend: str,
    summary: str,
    phase: str,
    distinct_backends: list[str],
    n_probes: int,
    over_budget: bool = False,
) -> str:
    lines = [
        f"Recorded {backend} probe ({n_probes} total). Phase: {phase.upper()}.",
        f"Last result (read it; quantify from it): {summary[:600]}",
        "When you have a leading hypothesis, advance to diagnose, then verify, run "
        "one confirming probe, and conclude with a committed, evidence-cited answer.",
    ]
    if over_budget:
        lines.append(
            f"You are at the probe budget ({n_probes} probes). Wrap up: advance to "
            "verify, run one confirming probe, and conclude with the evidence you "
            "have. A committed answer beats trailing off with no answer."
        )
    return "\n".join(lines)


def after_advance(to: str, distinct_backends: list[str], n_probes: int) -> str:
    if to == "verify":
        return (
            f"Phase: VERIFY. {n_probes} probes recorded. Run one probe that confirms "
            "or refutes your leading hypothesis, then conclude(primary_service, "
            "root_cause, final_answer, cascade_services=[]). conclude is blocked "
            "until a probe runs during this verify phase. Your final_answer should "
            "state the conclusion and cite the evidence you gathered. You must "
            "conclude; do not trail off.\n\n"
            "If your conclusion characterizes scope (isolated to one service versus "
            "a broad or fleet-wide issue), verify it before stating it: query the "
            "other services for the same symptom. Call it isolated only if they are "
            "clean; call it broad only if they are also affected."
        )
    return (
        f"Phase: {to.upper()}. Keep investigating. Advance to 'verify' once you have "
        "a leading hypothesis, then run a confirming probe and conclude."
    )
