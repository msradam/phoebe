"""Harbor agent that wraps o11y-fsm for o11y-bench.

Import path for the bench: ``o11y_fsm.harbor:O11yFSMAgent``.

    mise run bench:job -- \\
      --agent-import-path o11y_fsm.harbor:O11yFSMAgent \\
      --model openai/meta-llama/Llama-3.3-70B-Instruct-Turbo \\
      --task-name incident-triage
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from o11y_fsm.harbor.agent import O11yFSMAgent

__all__ = ["O11yFSMAgent"]


def __getattr__(name: str) -> Any:
    # Lazy so importing o11y_fsm.harbor.agent_runner (which needs only burr +
    # theodosia) does not pull in the optional `harbor` framework that
    # O11yFSMAgent subclasses.
    if name == "O11yFSMAgent":
        from o11y_fsm.harbor.agent import O11yFSMAgent

        return O11yFSMAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
