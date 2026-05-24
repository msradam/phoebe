"""o11y-fsm Burr Application: SRE incident-investigation as enforced phases.

This is the load-bearing module. Each action validates inputs, updates
state, and emits the prompt for the next phase. Gates between phases
enforce the SRE methodology:

* You cannot correlate until you have evidence from >=2 distinct backends.
* You cannot form a hypothesis until correlation has run.
* You cannot recommend next steps until your hypothesis has been verified
  (i.e. ``verify_or_revise`` returned with ``confirmed=True``).
* A disconfirmed hypothesis loops back to ``form_hypothesis`` rather than
  letting the agent abandon the investigation.

The Burr application is mounted as an MCP server via burrmcp.mount(...)
in ``build_server()``. The action namespace lives in the ``step`` tool's
argument schema.
"""

from __future__ import annotations

from typing import Any

from burr.core import ApplicationBuilder, Condition, State, action
from burr.tracking.client import LocalTrackingClient

from o11y_fsm.prompts import (
    PROMPT_CORRELATE,
    PROMPT_GATHER,
    PROMPT_HYPOTHESIS,
    PROMPT_RECOMMEND,
    PROMPT_SURVEY,
    PROMPT_VERIFY,
)

_TRACKER_PROJECT = "o11y-fsm"

_MIN_BACKENDS = 2
_VALID_CONFIDENCE = {"low", "medium", "high"}


# == actions ==========================================================


@action(
    reads=[],
    writes=[
        "incident_description",
        "scenario_time",
        "available_backends",
        "notable_services",
        "time_window",
        "evidence_by_backend",
        "covered_backends",
        "correlation",
        "hypothesis",
        "verification",
        "recommendations",
        "final_answer",
        "current_prompt",
        "log",
    ],
)
async def start_investigation(
    state: State,
    incident_description: str,
    scenario_time: str | None = None,
) -> State:
    """Open the investigation. Captures the prompt + scenario clock,
    emits the Phase-1 (survey) instruction.

    Args:
        incident_description: The natural-language incident statement,
            verbatim from the operator / bench task.
        scenario_time: Optional ISO-8601 anchor for the scenario clock.
            o11y-bench provides this as ``scenario_time.txt``; for
            non-bench use it can be None or "now".
    """
    if not incident_description.strip():
        raise ValueError("incident_description must not be empty")
    scenario_time = (scenario_time or "now").strip()
    prompt = PROMPT_SURVEY.format(
        incident_description=incident_description.strip(),
        scenario_time=scenario_time,
    )
    return state.update(
        incident_description=incident_description.strip(),
        scenario_time=scenario_time,
        available_backends=[],
        notable_services=[],
        time_window="",
        evidence_by_backend={},
        covered_backends=[],
        correlation=None,
        hypothesis=None,
        verification=None,
        recommendations=[],
        final_answer=None,
        current_prompt=prompt,
        log=[f"Investigation started; scenario_time={scenario_time!r}"],
    )


@action(
    reads=["log"],
    writes=["available_backends", "notable_services", "time_window", "current_prompt", "log"],
)
async def survey_telemetry(
    state: State,
    available_backends: list[str],
    notable_services: list[str] | None = None,
    time_window: str = "",
) -> State:
    """Record the reachable backends + initial scoping. The FSM remembers
    the list so later gates can refuse evidence from backends not
    surveyed and refuse correlation before >=2 backends are covered.
    """
    backends = [b.strip().lower() for b in (available_backends or []) if b and b.strip()]
    if len(backends) < 1:
        raise ValueError(
            "available_backends must list at least one backend you confirmed reachable"
        )
    # deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for b in backends:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    next_prompt = PROMPT_GATHER.format(
        pending_backends=deduped,
        covered_backends=[],
    )
    return state.update(
        available_backends=deduped,
        notable_services=list(notable_services or []),
        time_window=time_window.strip(),
        current_prompt=next_prompt,
        log=[*state["log"], f"Survey: backends={deduped}"],
    )


@action(
    reads=["available_backends", "evidence_by_backend", "covered_backends", "log"],
    writes=["evidence_by_backend", "covered_backends", "current_prompt", "log"],
)
async def gather_evidence(
    state: State,
    backend: str,
    queries: list[dict[str, Any]],
    notable_observations: str = "",
) -> State:
    """Stash one round of evidence from one backend. Loop-able: call again
    with the same or a different backend.

    Refuses ``backend`` not in the surveyed list (the SKILL says don't
    fabricate evidence from a backend you didn't probe).
    """
    backend_norm = (backend or "").strip().lower()
    if backend_norm not in state["available_backends"]:
        raise ValueError(
            f"backend={backend!r} not in surveyed available_backends="
            f"{state['available_backends']}. Survey it first or pick one you did survey."
        )
    items = list(queries or [])
    if not items:
        raise ValueError("queries must contain at least one query record")
    for i, q in enumerate(items):
        if not isinstance(q, dict) or "query" not in q or "result_summary" not in q:
            raise ValueError(
                f"queries[{i}] must be a dict with 'query' and 'result_summary'; got {q!r}"
            )
    evidence: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in state["evidence_by_backend"].items()
    }
    evidence.setdefault(backend_norm, []).append(
        {"queries": items, "notable_observations": notable_observations.strip()}
    )
    covered = sorted(evidence.keys())
    pending = [b for b in state["available_backends"] if b not in covered]
    next_prompt = PROMPT_GATHER.format(
        pending_backends=pending,
        covered_backends=covered,
    )
    return state.update(
        evidence_by_backend=evidence,
        covered_backends=covered,
        current_prompt=next_prompt,
        log=[*state["log"], f"Evidence: backend={backend_norm} ({len(items)} query/queries)"],
    )


@action(
    reads=["covered_backends", "log"],
    writes=["correlation", "current_prompt", "log"],
)
async def correlate(
    state: State,
    impacted_services: list[str],
    time_window: str,
    evidence_summary: str,
) -> State:
    """Record the cross-referenced findings. Gated server-side: not
    reachable until >=2 distinct backends are covered.
    """
    services = [s.strip() for s in (impacted_services or []) if s and s.strip()]
    if not services:
        raise ValueError("impacted_services must name at least one service")
    if not time_window.strip():
        raise ValueError("time_window must be a non-empty window string")
    if len(evidence_summary.strip()) < 40:
        raise ValueError(
            "evidence_summary must be a substantive paragraph (>=40 chars). "
            "The SKILL prescribes cross-referencing across backends; thin "
            "summaries indicate the correlation hasn't been done."
        )
    correlation = {
        "impacted_services": services,
        "time_window": time_window.strip(),
        "evidence_summary": evidence_summary.strip(),
    }
    return state.update(
        correlation=correlation,
        current_prompt=PROMPT_HYPOTHESIS,
        log=[
            *state["log"],
            f"Correlate: {len(services)} impacted service(s) over {time_window!r}",
        ],
    )


@action(
    reads=["correlation", "log"],
    writes=["hypothesis", "verification", "current_prompt", "log"],
)
async def form_hypothesis(
    state: State,
    primary_service: str,
    root_cause: str,
    cascade_services: list[str] | None = None,
    confidence: str = "medium",
) -> State:
    """Commit to a root-cause hypothesis. Called once initially; can be
    re-called after a disconfirming ``verify_or_revise``.
    """
    primary = (primary_service or "").strip()
    if not primary:
        raise ValueError("primary_service must not be empty (use 'unknown' if truly unclear)")
    if not root_cause.strip():
        raise ValueError("root_cause must not be empty")
    conf = (confidence or "").strip().lower()
    if conf not in _VALID_CONFIDENCE:
        raise ValueError(
            f"confidence must be one of {sorted(_VALID_CONFIDENCE)}; got {confidence!r}"
        )
    hypothesis = {
        "primary_service": primary,
        "cascade_services": [s.strip() for s in (cascade_services or []) if s and s.strip()],
        "root_cause": root_cause.strip(),
        "confidence": conf,
    }
    next_prompt = PROMPT_VERIFY.format(
        primary_service=hypothesis["primary_service"],
        cascade_services=hypothesis["cascade_services"],
        root_cause=hypothesis["root_cause"],
    )
    return state.update(
        hypothesis=hypothesis,
        verification=None,  # invalidate any prior verification when revising
        current_prompt=next_prompt,
        log=[
            *state["log"],
            f"Hypothesis: primary={primary!r} confidence={conf} ({len(hypothesis['cascade_services'])} cascade)",
        ],
    )


@action(
    reads=["hypothesis", "covered_backends", "log"],
    writes=["verification", "current_prompt", "log"],
)
async def verify_or_revise(
    state: State,
    verification_query: str,
    result_summary: str,
    confirmed: bool,
    revised_root_cause: str = "",
) -> State:
    """Run a focused verification. If confirmed, opens the path to
    recommendations. If not confirmed, the FSM routes back to
    ``form_hypothesis`` for revision.
    """
    if not verification_query.strip():
        raise ValueError("verification_query must not be empty")
    if not result_summary.strip():
        raise ValueError("result_summary must not be empty")
    verification = {
        "verification_query": verification_query.strip(),
        "result_summary": result_summary.strip(),
        "confirmed": bool(confirmed),
        "revised_root_cause": revised_root_cause.strip(),
    }
    if not confirmed:
        if not revised_root_cause.strip():
            raise ValueError(
                "confirmed=False requires revised_root_cause (so the next "
                "form_hypothesis call has something to revise toward)"
            )
        next_prompt = (
            f"Verification disconfirmed your hypothesis. Revise: the data "
            f"suggests {revised_root_cause.strip()!r}. Call form_hypothesis "
            f"again with an updated primary_service / root_cause."
        )
        log_note = "Verify: DISCONFIRMED, looping to form_hypothesis"
    else:
        hyp = state["hypothesis"] or {}
        next_prompt = PROMPT_RECOMMEND.format(
            primary_service=hyp.get("primary_service", "?"),
            root_cause=hyp.get("root_cause", "?"),
            covered_backends=state["covered_backends"],
        )
        log_note = "Verify: CONFIRMED, opening recommendations"
    return state.update(
        verification=verification,
        current_prompt=next_prompt,
        log=[*state["log"], log_note],
    )


@action(
    reads=[
        "incident_description",
        "evidence_by_backend",
        "covered_backends",
        "correlation",
        "hypothesis",
        "verification",
        "log",
    ],
    writes=["recommendations", "final_answer", "investigation_summary", "current_prompt", "log"],
)
async def recommend_next_steps(
    state: State,
    recommendations: list[dict[str, Any]],
    final_answer: str,
) -> State:
    """Terminal. Records actionable next steps + the final answer the
    operator (or bench grader) reads.
    """
    items = list(recommendations or [])
    if not items:
        raise ValueError("recommendations must include at least one item")
    for i, r in enumerate(items):
        if not isinstance(r, dict) or "action" not in r:
            raise ValueError(f"recommendations[{i}] must be a dict with 'action'; got {r!r}")
    if len(final_answer.strip()) < 80:
        raise ValueError(
            "final_answer must be a substantive markdown response "
            "(>=80 chars). The bench grader reads this verbatim."
        )
    hyp = state["hypothesis"] or {}
    summary = {
        "incident_description": state["incident_description"],
        "primary_service": hyp.get("primary_service"),
        "root_cause": hyp.get("root_cause"),
        "covered_backends": state["covered_backends"],
        "n_recommendations": len(items),
        "final_answer_chars": len(final_answer),
    }
    return state.update(
        recommendations=items,
        final_answer=final_answer.strip(),
        investigation_summary=summary,
        current_prompt="Investigation complete. Final answer in state.final_answer.",
        log=[
            *state["log"],
            f"Recommendations: {len(items)} item(s). Investigation complete.",
        ],
    )


# == graph ============================================================


# Gate: at least 2 distinct backends covered → correlate opens.
_HAVE_TWO_BACKENDS = Condition.expr(f"len(covered_backends) >= {_MIN_BACKENDS}")
_NEED_MORE_BACKENDS = Condition.expr(f"len(covered_backends) < {_MIN_BACKENDS}")

# Gate: verify_or_revise outcome drives the routing.
_HYPOTHESIS_CONFIRMED = Condition.expr(
    "verification is not None and bool(verification.get('confirmed'))"
)
_HYPOTHESIS_DISCONFIRMED = Condition.expr(
    "verification is not None and not bool(verification.get('confirmed'))"
)


def build_application():
    """Build the o11y-fsm Burr Application.

    Returns a fresh ``burr.core.Application`` per call; pass this function
    (not its result) as the factory to ``mount(...)`` for per-session
    state isolation.
    """
    return (
        ApplicationBuilder()
        .with_actions(
            start_investigation=start_investigation,
            survey_telemetry=survey_telemetry,
            gather_evidence=gather_evidence,
            correlate=correlate,
            form_hypothesis=form_hypothesis,
            verify_or_revise=verify_or_revise,
            recommend_next_steps=recommend_next_steps,
        )
        .with_transitions(
            ("start_investigation", "survey_telemetry"),
            ("survey_telemetry", "gather_evidence"),
            # gather_evidence loops until >=2 backends are covered, then opens correlate.
            ("gather_evidence", "gather_evidence", _NEED_MORE_BACKENDS),
            ("gather_evidence", "correlate", _HAVE_TWO_BACKENDS),
            ("correlate", "form_hypothesis"),
            ("form_hypothesis", "verify_or_revise"),
            # verify outcome: confirmed -> recommend; disconfirmed -> back to form_hypothesis.
            ("verify_or_revise", "recommend_next_steps", _HYPOTHESIS_CONFIRMED),
            ("verify_or_revise", "form_hypothesis", _HYPOTHESIS_DISCONFIRMED),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(
            incident_description="",
            scenario_time="",
            available_backends=[],
            notable_services=[],
            time_window="",
            evidence_by_backend={},
            covered_backends=[],
            correlation=None,
            hypothesis=None,
            verification=None,
            recommendations=[],
            final_answer=None,
            investigation_summary=None,
            current_prompt="",
            log=[],
        )
        .with_entrypoint("start_investigation")
        .build()
    )


def build_server():
    """Mount the o11y-fsm application as an MCP server.

    Imports burrmcp lazily so that ``build_application`` (pure Burr) can be
    used in environments where burrmcp isn't installed (e.g. the Harbor
    agent runner drives the Application directly via ``astep`` and never
    needs the MCP layer).
    """
    from burrmcp import ServingMode, mount

    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="o11y-fsm",
        instructions=(
            "Observability / SRE incident-investigation FSM. The CALLER LLM "
            "(whoever is driving you through MCP) does the actual querying; "
            "this FSM emits one prompt per phase and stores your findings. "
            "Walk: start_investigation(incident_description, scenario_time=None) "
            "-> survey_telemetry(available_backends, ...) "
            "-> gather_evidence(backend, queries, ...) [loops; >=2 backends required] "
            "-> correlate(impacted_services, time_window, evidence_summary) "
            "-> form_hypothesis(primary_service, root_cause, ...) "
            "-> verify_or_revise(verification_query, result_summary, confirmed, revised_root_cause='') "
            "[disconfirmed -> loops back to form_hypothesis] "
            "-> recommend_next_steps(recommendations, final_answer) [terminal]. "
            "Read state.current_prompt after every step for the next phase's "
            "instructions; burr://history for the full audit trail."
        ),
    )


if __name__ == "__main__":
    build_server().run()
