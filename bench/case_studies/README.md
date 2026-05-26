# Case study evidence

`evidence.json` holds the distilled record behind Theodosia's
[case study](https://msradam.github.io/theodosia/case-study/): two o11y-bench
investigation tasks, each run by the same model (Kimi K2.6) two ways,
free-ranging with the raw Grafana toolset (`raw`) and on rails through Phoebe
(`fsm`).

Each record carries the final answer (or its absence) and o11y-bench's per-check
grader verdicts and explanations verbatim. The recurring `raw` failure is the
same: the agent trails off and never delivers a final response. On
`service-degradation-rca` it fails that way on all three runs; on
`cache-refresh-lag-handoff` it fails on one of three (so it misses Pass^3). On
rails the model commits to a correct, evidence-cited conclusion every time. The
grader's own words document each run. These are illustrative cases, not an
aggregate result.

Source: o11y-bench investigation category, Kimi K2.6 via Together, May 2026.
