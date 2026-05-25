# How hard these benchmarks are, even for frontier models

Two more sources showing the observability/incident category is genuinely
unsaturated, so "the model just needs to be better" is not a near-term answer.

## Grafana o11y-bench

- Source: https://o11ybench.ai/ (leaderboard) ; https://github.com/grafana/o11y-bench
- What it is: "the first observability benchmark for AI agents", 63 real tasks across logs, metrics, traces, dashboards, and incident workflows. Scores are Pass^3 (all three trials must clear) per category.
- Leaderboard reality (as observed, April 2026 entries):
  - Best overall: claude-opus-4-7 (thinking off) at 79.4%.
  - Investigation category: opus-4-7 tops at 73%; most other models 27%-64% (sonnet-4-6 45%, gemini-3.1-pro 27%, gpt-5.4 64%).
  - Dashboards is the hardest category (top 57%); Grafana API is saturated (100%).
- Read: even the strongest frontier model clears under 80% overall and 73% on Investigation at Pass^3. The category we target is real and hard.
- The top agents are all "Base Model" entries (raw tools, no harness), which is the baseline Phoebe is measured against.

## CUJBench

- Source: "CUJBench: Benchmarking LLM-Agent on Cross-Modal Failure Diagnosis from Browser to Backend" (arXiv 2604.23455)
- Finding: overall agent accuracy 19.7%, with a ceiling of 52%, "well below saturation."
- Read: cross-modal failure diagnosis (the realistic shape of an incident) is far from solved for agents.

## Industry corroboration (Traversal)

- Source: https://traversal.com/blog/llm-benchmarking-in-context-retrieval-reasoning-incident-root-cause-analysis
- Notes: production RCA grading uses senior-SRE judgment, not string match ("whether the model's reasoning and conclusion matched what senior SREs ultimately determined"). Their stated production constraints: "accuracy ... is non-negotiable", latency compounds in high-stakes incidents, token/context cost is a real tradeoff.
- Read: in production, getting the answer right is the bar, and current agents do not clear it reliably, which is the gap a gated, auditable harness is meant to manage (bound the failure, make it inspectable) rather than pretend away.

## Bearing on Phoebe

- These establish the "agents are not reliable for SRE yet" premise with numbers, so the pitch is not asserting it, it is citing it.
- They also set up the honest framing: Phoebe does not claim to beat the ceiling; it makes the agent's behavior bounded and auditable, and (per IBM) removes the structural failure modes that prompt engineering cannot.
