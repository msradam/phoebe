# o11y-fsm

An observability / SRE incident-investigation finite-state machine for LLM-driven agents. Mounts as an MCP server via [`burrmcp`](https://github.com/msradam/burrmcp); ships a [Harbor](https://harborframework.com/) agent for running against [Grafana's o11y-bench](https://github.com/grafana/o11y-bench).

```text
start_investigation
  → survey_telemetry              (which backends are reachable?)
    → gather_evidence  (loops)    (≥2 backends required before correlation)
      → correlate                 (cross-reference services + windows)
        → form_hypothesis         (primary vs cascade, confidence)
          → verify_or_revise      (focused query; disconfirms loop back)
            → recommend_next_steps  (terminal; cites evidence refs)
```

Each phase is a `step()` call against the mounted MCP server. The FSM refuses out-of-order calls with structured payloads listing what's reachable. State + audit trail live on the server.

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

## A note on gate calibration

FSM gates have to be calibrated. Too loose and they don't change behavior; too tight and they trap the agent.

The first cut of `gather_evidence` hard-refused any backend not named in `survey_telemetry`. A weak model (Llama 3.3 70B) that surveyed only Prometheus then tried to query Loki got refused, couldn't reach the `correlate` gate (which needs ≥2 backends), and looped until the step limit. The fix: a successful query against a backend is itself proof the backend is reachable, so `gather_evidence` auto-registers it. The survey is a starting inventory, not a hard allow-list. The valuable constraint, "≥2 backends before correlation," stays; the counterproductive one was relaxed.

The lesson generalizes: enforce the invariant that matters (don't conclude before cross-referencing) and let the agent discover the rest.

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
