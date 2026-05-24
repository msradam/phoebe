"""Prompt fragments emitted into ``state.current_prompt`` after each step.

These guide the caller LLM on what to do next. In the circe-style design
the query actions ARE the operations, so the prompts are short nudges
toward the next sensible move, not heavyweight phase scripts.
"""

from __future__ import annotations


def after_start(incident_description: str, scenario_time: str) -> str:
    return (
        f"Investigating: {incident_description}\n"
        f"Scenario clock: {scenario_time}\n\n"
        "Phase: TRIAGE. Start gathering evidence. Use:\n"
        "- query_metrics(promql, hypothesis=...) for Prometheus\n"
        "- query_logs(logql, hypothesis=...) for Loki\n"
        "- query_traces(traceql, hypothesis=...) for Tempo\n\n"
        "Cross-reference at least two backends. When you have a leading "
        "hypothesis, advance_phase(to='diagnose', rationale=...), then "
        "(after >=2 backends) advance_phase(to='verify', ...) and run a "
        "confirming probe before conclude(...)."
    )


def after_probe(
    *,
    backend: str,
    summary: str,
    phase: str,
    distinct_backends: list[str],
    n_probes: int,
) -> str:
    lines = [
        f"Recorded {backend} probe ({n_probes} total). Phase: {phase.upper()}.",
        f"Backends covered so far: {distinct_backends}.",
        f"Last result: {summary[:160]}",
    ]
    if len(distinct_backends) < 2:
        lines.append(
            "You have only one backend. Cross-reference another "
            "(logs if you queried metrics, or vice versa) before you can verify."
        )
    else:
        lines.append(
            "You have >=2 backends. When ready: advance_phase(to='diagnose'/'verify', "
            "rationale=...). conclude needs phase=='verify' + a probe taken during verify."
        )
    return "\n".join(lines)


def after_advance(to: str, distinct_backends: list[str], n_probes: int) -> str:
    if to == "verify":
        return (
            f"Phase: VERIFY. Backends covered: {distinct_backends} ({n_probes} probes).\n"
            "Run ONE focused probe that confirms (or refutes) your leading "
            "hypothesis, then call conclude(primary_service, root_cause, "
            "final_answer, cascade_services=[]). conclude is blocked until a "
            "probe runs during this verify phase."
        )
    return (
        f"Phase: {to.upper()}. Keep gathering / cross-referencing evidence. "
        "Advance to 'verify' once you have probes from >=2 backends and a "
        "leading hypothesis."
    )
