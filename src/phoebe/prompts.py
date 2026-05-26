"""Prompt fragments emitted into ``state.current_prompt`` after each step.

These guide the caller LLM on what to do next. In the circe-style design
the query actions ARE the operations, so the prompts are short nudges
toward the next sensible move, not heavyweight phase scripts.
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
        "Phase: TRIAGE. Start gathering evidence with the Grafana tools "
        "(query Prometheus, then Loki, then Tempo if useful). Each call is "
        "recorded as evidence.\n\n"
        "Cross-reference at least two backends, and establish the full blast "
        "radius before you wrap up: query per service so you can say which "
        "services ARE and are NOT affected. When the affected services are "
        "covered and you have a leading hypothesis, advance_phase(to='diagnose', "
        "rationale=...), then advance_phase(to='verify', ...) and run a "
        "confirming probe before conclude(...).\n\n"
        "Investigation discipline:\n"
        "- Quantify from the query results you get back (counts, rates, shares). "
        "Read the 'result' field on each tool response; do not estimate numbers.\n"
        "- Establish the blast radius: query per service so you can say which "
        "services ARE and are NOT affected, not just the loudest one.\n"
        "- If the incident names a specific endpoint, path, service, prior "
        "incident, or rollout, query that directly before concluding.\n"
        "- If you query traces, note a representative trace ID from the result so "
        "you can cite it."
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
        f"Backends covered so far: {distinct_backends}.",
        f"Last result (read it; quantify from it, do not estimate): {summary[:600]}",
    ]
    if len(distinct_backends) < 2:
        lines.append(
            "You have only one backend. Cross-reference another "
            "(logs if you queried metrics, or vice versa) before you can verify."
        )
    else:
        lines.append(
            "You have cross-referenced >=2 backends. Keep going until you have "
            "established the full blast radius: query per service so you can state "
            "which services ARE and are NOT affected, and address any specific "
            "endpoint, prior incident, or rollout the task named. Once the affected "
            "services are covered, advance_phase(to='diagnose'), then "
            "advance_phase(to='verify'), run one confirming probe, and conclude(...)."
        )
    if over_budget:
        lines.append(
            f"You have gathered {n_probes} probes. If the blast radius is already "
            "covered, wrap up now: advance to verify and conclude. Otherwise finish "
            "the few remaining service queries first."
        )
    return "\n".join(lines)


def after_advance(to: str, distinct_backends: list[str], n_probes: int) -> str:
    if to == "verify":
        return (
            f"Phase: VERIFY. Backends covered: {distinct_backends} ({n_probes} probes).\n"
            "Run ONE focused probe that confirms (or refutes) your leading "
            "hypothesis, then call conclude(primary_service, root_cause, "
            "final_answer, cascade_services=[]). conclude is blocked until a "
            "probe runs during this verify phase.\n\n"
            "Before you conclude, make sure your final_answer:\n"
            "- quantifies the impact with a number from a query (a count, rate, or "
            "share you actually computed), not an estimate;\n"
            "- states the blast radius: which services are affected and which are "
            "not, and whether the incident is isolated or broad;\n"
            "- separates the primary/root-cause service from downstream cascade;\n"
            "- cites a representative trace ID if you queried traces;\n"
            "- addresses any specific endpoint, prior incident, or rollout the task "
            "named. If you have not queried it yet, do that now."
        )
    return (
        f"Phase: {to.upper()}. Keep gathering / cross-referencing evidence. "
        "Advance to 'verify' once you have probes from >=2 backends and a "
        "leading hypothesis."
    )
