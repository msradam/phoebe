# Research notes: why agents are unreliable for SRE, and what fixes it

Captured source material backing Phoebe's thesis. Each file is a readout of one
source: what it is, the numbers, the most quotable lines, and how it bears on
the pitch. Pulled for reference so the marketing and blog can cite without
re-fetching.

## The argument these sources support

1. Current LLM agents are not reliable at SRE / observability / incident work.
   Frontier models top out well short of dependable; open models are far worse.
   (IBM IT-Bench, Grafana o11y-bench, AIOpsLab, CUJBench.)
2. The failures are structural and nameable, not random. A small set of failure
   modes dominates: incorrect verification, premature termination, not knowing
   when the task is done, reasoning-action mismatch, lost context.
   (MAST taxonomy; IBM IT-Bench failure analysis.)
3. Prompt engineering barely moves these. Architectural control does. IBM
   Research measured prompt-level fixes at about 15.6% improvement versus up to
   53% from structural mechanisms, and names finite state machines specifically.
4. Therefore: put the procedure outside the model. Enforce ordering, gates, and
   termination in a state machine; record everything. That is exactly what
   Phoebe (an FSM mounted over MCP via Theodosia) does, and the structural
   failure modes it removes are the ones the literature flags as fatal.

The honest boundary: a state machine removes the structural failures (ordering,
skipped gates, premature/late termination, unbounded action space). It does not
fix incorrect verification or reasoning-action mismatch inside a valid step.
That boundary is consistent across every source here, and it matches Phoebe's
own benchmark, where the residual misses are verification errors.

## Index

- [ibm-itbench-mast.md](ibm-itbench-mast.md) — IBM Research IT-Bench + MAST analysis. The centerpiece: pass rates, the dominant failure modes, and the explicit prescription to use finite state machines and put termination control outside the model.
- [mast-taxonomy.md](mast-taxonomy.md) — "Why Do Multi-Agent LLM Systems Fail?" (UC Berkeley Sky Lab). The 14-failure-mode taxonomy and the finding that multi-agent systems often fail to beat simple baselines.
- [aiopslab.md](aiopslab.md) — Microsoft Research AIOpsLab. Off-the-shelf models are insufficient for cloud ops; the workflow-structured agent (FLASH) leads.
- [benchmark-difficulty.md](benchmark-difficulty.md) — Grafana o11y-bench and CUJBench: how unsaturated these benchmarks are even for frontier models.
