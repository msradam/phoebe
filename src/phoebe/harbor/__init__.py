"""Harbor agent that wraps phoebe for o11y-bench.

Import path for the bench: ``phoebe.harbor:PhoebeAgent``.

    mise run bench:job -- \\
      --agent-import-path phoebe.harbor:PhoebeAgent \\
      --model openai/meta-llama/Llama-3.3-70B-Instruct-Turbo \\
      --task-name incident-triage
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from phoebe.harbor.agent import PhoebeAgent

__all__ = ["PhoebeAgent"]


def __getattr__(name: str) -> Any:
    # Lazy so importing phoebe.harbor.agent_runner (which needs only burr +
    # theodosia) does not pull in the optional `harbor` framework that
    # PhoebeAgent subclasses.
    if name == "PhoebeAgent":
        from phoebe.harbor.agent import PhoebeAgent

        return PhoebeAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
