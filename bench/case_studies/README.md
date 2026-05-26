# Case study evidence

`evidence.json` holds the distilled record behind Theodosia's
[case study](https://msradam.github.io/theodosia/case-study/): one
o11y-bench investigation task (service-degradation-rca), run by the same model
(Kimi K2.6) two ways, free-ranging with the raw Grafana toolset (`raw`) and on
rails through Phoebe (`fsm`).

For each run it carries the tool-call sequence, the final answer (or its
absence), and o11y-bench's per-check grader verdicts and explanations verbatim.
On this task all three `raw` runs ended with an empty final answer (the model
investigated and never delivered a conclusion); on rails the model committed to
a correct, evidence-cited conclusion. The grader's own words document each run.
This is a single illustrative case, not an aggregate result.

Source: o11y-bench investigation category, Kimi K2.6 via Together, May 2026.
