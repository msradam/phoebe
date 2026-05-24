"""o11y-fsm: SRE incident-investigation FSM, mounted as MCP via BurrMCP.

Walks the standard observability methodology as enforced phases:

    start_investigation
      -> survey_telemetry
        -> gather_evidence  (loops; requires >=2 distinct backends)
          -> correlate
            -> form_hypothesis
              -> verify_or_revise  (may loop back to form_hypothesis)
                -> recommend_next_steps   [terminal]

Designed for the o11y-bench task shape (Grafana + Prometheus + Loki + Tempo)
but the FSM is backend-agnostic; the agent talks to whatever MCP tools are
available in its environment.
"""

from o11y_fsm.app import build_application, build_server

__all__ = ["build_application", "build_server"]

__version__ = "0.1.0"
