"""Prompt templates emitted by each FSM action.

Each prompt instructs the caller LLM (whichever model is driving the FSM via
MCP) on what to do for the current phase, and how to report its findings via
the next FSM call. The FSM never calls an LLM itself; it just stores
structured artifacts and gates which phase is reachable.
"""

from __future__ import annotations

PROMPT_SURVEY = """\
PHASE 1 of 6: SURVEY TELEMETRY.

You are investigating: {incident_description}
Scenario time anchor: {scenario_time}

Before querying anything, take a quick inventory of what's available:
- Which observability backends are reachable (Prometheus? Loki? Tempo?
  Grafana dashboards?). Use whatever tool the MCP server exposes for
  listing data sources or running a no-op probe per backend.
- Which services / jobs / namespaces appear in the metrics catalog?
- Roughly what time window is interesting (last hour? last 6h?
  the scenario_time anchor plus or minus)?

Call `survey_telemetry(available_backends=[...], notable_services=[...], time_window="...")`:
- available_backends: list of backend names you confirmed reachable
  (e.g. ["prometheus", "loki"]).
- notable_services: services / jobs you noticed are worth examining.
- time_window: a human-readable window like "last 6 hours" or
  "2026-05-24T08:00Z to 2026-05-24T14:00Z".

The FSM enforces that you gather evidence from at least two of the
backends you list here before you can correlate. Do not list a backend
you didn't actually probe.
"""


PROMPT_GATHER = """\
PHASE 2 of 6: GATHER EVIDENCE.

Run one or more concrete queries against ONE backend. You'll repeat this
call for each backend you investigate; the FSM will not let you move on
to correlation until you have findings from at least two different backends.

Backends still pending: {pending_backends}
Backends already covered: {covered_backends}

Pick a backend, run real queries, capture what you observed. For each
query, record:
- the query string
- a one-line summary of the result
- the backend name

If a query errors, capture that too (the failure mode is also evidence).

Call `gather_evidence(backend="...", queries=[{{...}}], notable_observations="...")`:
- backend: which backend you queried (one of your surveyed backends).
- queries: list of dicts:
    [{{"query": "...", "result_summary": "...", "ok": true|false}}, ...]
- notable_observations: a paragraph synthesizing what stood out.

When at least 2 backends have evidence, `correlate` opens up. You can
also call `gather_evidence` again on the same backend with deeper queries
if the first pass left gaps.
"""


PROMPT_CORRELATE = """\
PHASE 3 of 6: CORRELATE.

You have evidence from: {covered_backends}.

Now cross-reference. Identify:
- Which services are impacted, and in what time window?
- Do the metrics tell the same story as the logs / traces?
- What's the timeline (e.g., "5xx rate spiked at HH:MM in service A,
  followed by elevated errors in service B at HH:MM+2min")?

Call `correlate(impacted_services=[...], time_window="...", evidence_summary="...")`:
- impacted_services: list of service names that showed anomalies.
- time_window: tight time window over which the incident is visible.
- evidence_summary: 2-4 sentences cross-referencing what you saw across
  backends. Cite specific query results where possible.
"""


PROMPT_HYPOTHESIS = """\
PHASE 4 of 6: FORM HYPOTHESIS.

Based on the correlated evidence, commit to a root-cause hypothesis. The
SKILL of incident response is distinguishing primary cause from cascading
effects. Don't list every affected service as equally responsible.

Call `form_hypothesis(primary_service="...", cascade_services=[...], root_cause="...", confidence="low|medium|high")`:
- primary_service: the single service you believe is the root cause.
  If you genuinely cannot pick one, set this to "unknown" and explain
  in root_cause why the data is insufficient.
- cascade_services: services impacted as a downstream consequence.
  Empty list is fine if the primary stands alone.
- root_cause: 1-2 sentences naming the most likely root cause
  (e.g., "payment-service hit a connection-pool exhaustion at 14:02,
  causing upstream timeouts in order-service and checkout-service").
- confidence: your own calibration. "low" if the data is thin.

The next phase will ask you to verify this hypothesis with a fresh query.
"""


PROMPT_VERIFY = """\
PHASE 5 of 6: VERIFY OR REVISE.

Your hypothesis:
- primary: {primary_service}
- cascade: {cascade_services}
- root cause: {root_cause}

Run ONE focused verification query against any backend that would
distinguish your hypothesis from a plausible alternative. Examples:

- If primary == "payment-service", query specifically for that service's
  saturation / errors / restarts in the hypothesis window.
- If you claimed a connection-pool issue, look for connection-pool
  metrics or the corresponding log lines.

Call `verify_or_revise(verification_query="...", result_summary="...", confirmed=true|false, revised_root_cause="...")`:
- verification_query: the query string you ran.
- result_summary: what it returned.
- confirmed: True if the result supports your hypothesis. False if it
  contradicts and you need to revise.
- revised_root_cause: only if confirmed=False; the FSM will route you
  back to form_hypothesis to commit to the revision.

If confirmed=True, the next phase opens: write recommendations.
"""


PROMPT_RECOMMEND = """\
PHASE 6 of 6: RECOMMEND NEXT STEPS.

Hypothesis verified:
- primary: {primary_service}
- root cause: {root_cause}
- evidence backends: {covered_backends}

Now produce a tight, actionable recommendation list. Each recommendation
should cite specific evidence from earlier phases (don't fabricate; the
audit trail will show what you queried).

Call `recommend_next_steps(recommendations=[...], final_answer="...")`:
- recommendations: list of dicts:
    [{{"action": "...", "owner": "team|individual|null",
       "evidence_ref": "phase + brief citation"}}, ...]
- final_answer: a complete markdown response to the original incident
  question. Lead with the conclusion (primary, root cause, timestamp);
  follow with cited evidence; end with the recommendations. This is the
  artifact the operator (or grader) will read.

The FSM terminates here.
"""
