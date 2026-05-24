"""Harbor agent that wraps o11y-fsm for o11y-bench.

Import path for the bench: ``o11y_fsm.harbor:O11yFSMAgent``.

    mise run bench:job -- \\
      --agent-import-path o11y_fsm.harbor:O11yFSMAgent \\
      --model openai/meta-llama/Llama-3.3-70B-Instruct-Turbo \\
      --task-name incident-triage
"""

from o11y_fsm.harbor.agent import O11yFSMAgent

__all__ = ["O11yFSMAgent"]
