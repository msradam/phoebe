# Case study evidence

`evidence.json` holds the distilled record behind Theodosia's
[case study](https://msradam.github.io/theodosia/case-study/): three
o11y-bench investigation tasks, each run by the same model (Kimi K2.6) two ways,
free-ranging with the raw Grafana toolset (`raw`) and on rails through Phoebe
(`fsm`).

For each run it carries the tool-call sequence, the final answer (or its
absence), and o11y-bench's per-check grader verdicts and explanations verbatim.
The recurring failure in the `raw` runs is an empty final answer: the model
investigated and never delivered a conclusion. The grader's own words document
each one.

Source: o11y-bench investigation category, Kimi K2.6 via Together, May 2026.
