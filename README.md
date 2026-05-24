# o11y-fsm

An observability / SRE incident-investigation finite-state machine for LLM-driven agents. Mounts as an MCP server via [`burrmcp`](https://github.com/msradam/burrmcp); ships a [Harbor](https://harborframework.com/) agent for running against [Grafana's o11y-bench](https://github.com/grafana/o11y-bench).

```text
start_investigation
  ├─ query_metrics(promql)   ┐  the query actions ARE the operations:
  ├─ query_logs(logql)       │  each runs the query through a bound
  ├─ query_traces(traceql)   ┘  telemetry client and records evidence
  ├─ advance_phase(to, rationale)   triage → diagnose → verify
  └─ conclude(primary_service, root_cause, final_answer, cascade_services)
```

Hub topology: every operational action is reachable from every other. The methodology is enforced inside action bodies, not by narrowing the graph (this is what makes a mid-size model able to *drive* the FSM instead of fighting it). `conclude` is gated: phase must be `verify`, you need probes from ≥2 distinct backends, and at least one probe must have run during the verify phase. A repeated identical probe is refused ("vary the probe").

The design follows the pattern proven in a sibling project (circe): **the operation is the FSM action.** There is no separate "do work here, record it there" surface; calling `query_metrics` runs the query and advances state in one step. State + audit trail live on the server.

## What it gives the caller

- **Phase enforcement at the protocol layer.** The agent cannot jump from `start_investigation` to `recommend_next_steps`. Cannot correlate before evidence from ≥2 backends. Cannot finalize before verifying.
- **Auditable trail.** Every step is a row in Burr's tracker (`~/.burr/o11y-fsm/<app_id>/log.jsonl`). Replayable, forkable, diffable. Tail it with `burrmcp sessions tail`.
- **Backend-agnostic.** The FSM doesn't know about Prometheus or Loki. The caller LLM runs the actual queries (against whatever MCP tools its environment exposes) and reports findings back via the next FSM step.

## Install

```bash
pip install o11y-fsm
```

For running as a Harbor agent against o11y-bench:

```bash
pip install 'o11y-fsm[harbor]'
```

## Use standalone

```python
from o11y_fsm import build_server

server = build_server()
server.run()  # serves over stdio MCP
```

Or via the burrmcp CLI:

```bash
burrmcp serve o11y_fsm.app:build_application --name o11y-fsm
```

## Use on o11y-bench

`o11y_fsm.harbor:O11yFSMAgent` is a [Harbor `BaseAgent`](https://www.harborframework.com/docs/agents) that wraps the FSM. It:

1. Walks the FSM via MCP
2. Routes the caller LLM's tool calls to Grafana's MCP server (`mcp-grafana`, exposed in Harbor's o11y-stack sidecar)
3. Returns the FSM's `final_answer` as the bench-graded response

To use it in an o11y-bench job:

```bash
mise run bench:job -- \
  --agent-import-path o11y_fsm.harbor:O11yFSMAgent \
  --model openai/meta-llama/Llama-3.3-70B-Instruct-Turbo \
  --task-name incident-triage \
  --n-attempts 3
```

## Why an FSM

The o11y-bench rubrics for the `investigation` task category grade on phase discipline:

- *"Recommendations appear only after the transcript shows queries from metrics, logs, or traces."*
- *"Response ties services to evidence from logs AND metrics."*
- *"Distinguishes primary vs cascade."*

These are exactly the criteria an FSM gate can enforce mechanically. SKILL.md prose describes the methodology; this FSM is the methodology, refusing illegal transitions. A weak model that would otherwise skip phases under pressure has no legal step to take except the next phase.

## Design note: why single-surface

The first cut (v0.1) split the work across two tool surfaces: the agent used the harness's raw Grafana MCP tools to query, then *separately* called the FSM to record what it found. A mid-size model (Llama 3.3 70B) got absorbed in the query surface and never crossed to the bookkeeping surface, looping `query_prometheus` dozens of times without ever advancing the FSM.

The fix (v0.2) collapses to one surface: the query actions ARE the operations. The only way to touch telemetry is through `query_metrics` / `query_logs` / `query_traces`, each of which runs the query and records evidence in a single step. There is no second surface to get stuck on. This mirrors the design of a sibling Burr agent (circe), where the same model drives a comparable FSM reliably.

The accompanying lesson on gate calibration: enforce the invariant that matters (don't conclude before cross-referencing ≥2 backends, don't conclude without a verifying probe) via action-body checks, and keep graph reachability broad so the agent is never told "no" by the graph for a normal operation. A repeated identical probe is refused with a specific reason ("vary the probe"), not a dead end.

## Repo layout

```
src/o11y_fsm/
  app.py             FSM actions + graph + build_application + build_server
  prompts.py         Per-phase prompt templates
  harbor/            Harbor agent wrapper (optional dep)
tests/
```

## License

Apache 2.0.

## Notice

`o11y-fsm` is independent open-source work by Adam Munawar Rahman and does not represent the views, positions, or technology roadmap of IBM Corporation or any other employer. It is built on [Apache Burr](https://github.com/apache/burr) and [burrmcp](https://github.com/msradam/burrmcp); references to Grafana's [o11y-bench](https://github.com/grafana/o11y-bench) are for integration purposes and do not imply endorsement.
