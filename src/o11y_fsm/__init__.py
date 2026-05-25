"""o11y-fsm: SRE incident-investigation FSM, mounted as MCP via Theodosia.

Single surface: the agent sees only the FSM's actions. The query actions
run telemetry queries against Grafana (through Theodosia's upstream
mechanism) and record evidence in the same step, so the operation is the
FSM action. Phase is a state variable; methodology gating lives in the
action bodies.

    start_investigation
      query_metrics(promql) / query_logs(logql) / query_traces(traceql)
      advance_phase(to, rationale)   triage -> diagnose -> verify
      conclude(primary_service, root_cause, final_answer, cascade_services)

Built for the o11y-bench task shape (Grafana + Prometheus + Loki + Tempo).
"""

from o11y_fsm.app import build_application, build_server

__all__ = ["build_application", "build_server"]

__version__ = "0.1.0"
