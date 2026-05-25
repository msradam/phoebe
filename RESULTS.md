# Results: o11y-bench investigation set (first run)

Date: 2026-05-25. A first, unoptimized run of the `o11y-fsm` investigation
harness on Grafana's o11y-bench, with a real grading pass.

## Setup

- Harness: `o11y-fsm`, an SRE incident-investigation finite state machine,
  mounted as an MCP server by [Theodosia](https://github.com/msradam/theodosia) (v0.1).
- Driver model: Kimi K2.6 (1T MoE, open weights) via Together, function
  calling. The model never sees Grafana directly. It drives the FSM; the FSM
  reaches Prometheus, Loki, and Tempo through Theodosia's upstream.
- Environment: o11y-bench's Grafana o11y-stack with seeded telemetry, one
  attempt per task.
- Grading: o11y-bench's rubric judge (Anthropic).

## The invariant is the agent logic

The investigation procedure is not a prompt the model may ignore. It is a
graph, designed once and served over MCP: one entry, a hub of operations,
phase gates, and a terminal. The model fills slots; the graph enforces the
order, like a circuit.

![circuit](demos/circuit.gif)

- `start_investigation` discovers the live datasources and the telemetry
  schema (metric names, labels, services), then sets phase to triage.
- `query_metrics` / `query_logs` / `query_traces` run real PromQL / LogQL /
  TraceQL and record the evidence. The operation is the FSM action.
- `advance_phase` moves triage to diagnose to verify. diagnose needs at least
  one finding; verify needs evidence from at least two distinct backends.
- `conclude` is gated: phase must be verify, with at least two backends and a
  probe taken during the verify phase.
- A repeated identical probe is refused by a loop guard; the agent varies it
  and continues.

Out-of-order and premature steps are refused server-side, with the legal next
actions carried on the response so the agent self-corrects. The whole session
is a replayable, inspectable trace.

![watch](demos/watch.gif)

![logs](demos/logs.gif)

## Results

| Task | Reward | Reached a conclusion |
|---|---|---|
| incident-triage | 0.78 | yes |
| retry-backlog-incident | 0.30 | yes |
| promql-retry-backlog-triage | 0.30 | yes |
| payments-path-root-cause | 0.00 | yes |
| cache-incident-blast-radius | 0.00 | yes |
| **Mean** | **0.28** | **5 / 5** |

## Reading the results

Two axes, and they separate cleanly.

**Structural: 5 of 5 runs completed the procedure.** Every run discovered the
schema, queried at least two backends, advanced through the phases, and
produced an evidence-cited conclusion. No run skipped a phase, terminated
early, or crashed. That is what the FSM provides by construction, on a 1T open
model, at one attempt each.

**Semantic: mean 0.28, range 0.0 to 0.78. The FSM does not make the model
correct.** On `incident-triage` it scored 0.78 (8 of 9 rubric checks); the one
miss was stating the combined payment plus order 5xx share accurately, a
numeric error inside a valid step. The two zeros are wrong diagnoses, not
broken runs: on `payments-path-root-cause` the model blamed order-service when
the answer is the payments path through payment-service; on
`cache-incident-blast-radius` it called the incident broad when it is isolated
to user-service. In both, the agent followed the procedure and cited real
telemetry, then reasoned to the wrong conclusion.

This is the boundary stated plainly in the design: it enforces the shape of
the work, not the reasoning inside a step. Mapped onto the IBM Research and
UC Berkeley [MAST](https://huggingface.co/blog/ibm-research/itbenchandmast)
failure taxonomy, the FSM structurally prevents ordering violations, skipped
gates, and premature termination (FM-1.1, FM-1.5, FM-3.1). It does nothing for
incorrect verification (FM-3.3) or reasoning-action mismatch (FM-2.6), which
is exactly where the zeros land.

## Caveats

First run. One attempt per task, one model, one author, and the graph was not
tuned to these tasks. This is not a leaderboard submission. It is a baseline:
evidence that the harness drives real investigations to gradeable, auditable
conclusions, and an honest read on where an open model does and does not get
the answer right once the procedure is enforced.

## Reproduce

```bash
# stack on localhost (Grafana MCP on :8080)
docker run -d -p 3000:3000 -p 9090:9090 -p 3100:3100 -p 3200:3200 -p 8080:8080 \
  o11y-bench-o11y-stack:latest

# one task through the FSM, graded
mise run bench:job -- \
  --agent-import-path o11y_fsm.harbor:O11yFSMAgent \
  --model openai/moonshotai/Kimi-K2.6 \
  --task-name incident-triage --n-attempts 1
```
