# Microsoft Research: AIOpsLab

- Source: "AIOpsLab: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds" (Microsoft Research)
- URLs: blog https://www.microsoft.com/en-us/research/blog/aiopslab-building-ai-agents-for-autonomous-clouds/ ; paper https://www.microsoft.com/en-us/research/wp-content/uploads/2024/10/arxiv_AIOpsLab.pdf ; design-principles paper https://arxiv.org/abs/2407.12165
- What it is: a framework that spins up microservice environments, injects faults, generates load, and exports telemetry to evaluate agents across the incident lifecycle (detection, localization, root cause, mitigation). Microsoft frames the goal as "AgentOps", self-healing clouds.
- Why it matters to us: independent, big-lab corroboration that generic LLM agents are insufficient for cloud ops and that workflow structure helps. Note: the public blog is mostly framework design; the empirical numbers below are from the paper, cite the paper for them.

## Findings (from the paper)

- No single agent excelled across tasks. The best overall was FLASH, an agent built around a workflow-automation system, not a bare model.
- A generic GPT-3.5 agent struggled significantly, "highlighting that off-the-shelf models are insufficient for complex operational tasks."
- Existing question-answer evals (e.g. OpsEval) "disconnect from real-world operational challenges that require complex debugging, code understanding, and multi-step fault resolution." Real ops needs multi-step execution, not Q&A.

## Design insights (from the blog)

> "Observability is crucial for clear root-cause diagnosis ... the ability to execute arbitrary shell commands allowed for effective troubleshooting."

## Bearing on Phoebe

- "Off-the-shelf models are insufficient; the workflow-structured agent leads" is the same finding as Phoebe's: structure (the FSM) is what makes a model usable for ops.
- Caveat for honesty: AIOpsLab is a different environment and task set than o11y-bench; cite it as corroboration of the direction, not as a comparable score.
