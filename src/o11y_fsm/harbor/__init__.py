"""Harbor agent that wraps o11y-fsm for o11y-bench.

Import path for the bench: ``o11y_fsm.harbor:O11yFsmAgent``.

    mise run bench:job -- \\
      --agent-import-path o11y_fsm.harbor:O11yFsmAgent \\
      --model openai/meta-llama/Llama-3.3-70B-Instruct-Turbo \\
      --task-name incident-triage
"""

from o11y_fsm.harbor.agent import O11yFsmAgent

__all__ = ["O11yFsmAgent"]
