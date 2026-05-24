"""Harbor BaseAgent that drives o11y-fsm inside the bench container.

The agent_runner.py + the o11y_fsm package source are uploaded into
the container at /app/. The runner uses uv's PEP 723 inline-script
dep declaration to install burrmcp + litellm + mcp on first run.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

HARBOR_DIR = Path(__file__).parent
RUNNER_SCRIPT = HARBOR_DIR / "agent_runner.py"
SYSTEM_PROMPT = HARBOR_DIR / "system_prompt.txt"
TASK_PROMPT = HARBOR_DIR / "task_prompt.txt"
# We upload the entire o11y_fsm package source so the runner can `import o11y_fsm`
# (its PEP 723 deps already install burrmcp, fastmcp, apache-burr, etc.).
PACKAGE_ROOT = HARBOR_DIR.parent  # .../src/o11y_fsm
VIEWER_COMMAND_STDOUT_PATH = "/logs/agent/command-0/stdout.txt"


def _normalize_litellm_model_name(model_name: str) -> str:
    if model_name.startswith("google/"):
        return f"gemini/{model_name.split('/', 1)[1]}"
    return model_name


def _select_remote_mcp_url(mcp_servers: list[Any]) -> str | None:
    for server in mcp_servers:
        url = getattr(server, "url", None)
        if not isinstance(url, str) or not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1", "o11y-stack"}:
            continue
        return url
    return None


def _build_runner_command() -> str:
    cmd = (
        "set -o pipefail; "
        "mkdir -p /logs/agent/command-0; "
        f'uv run /app/agent_runner.py 2>&1 | tee "{VIEWER_COMMAND_STDOUT_PATH}"'
    )
    return f"bash -lc {shlex.quote(cmd)}"


class O11yFSMAgent(BaseAgent):
    """Harbor agent that walks the o11y-fsm Burr application inside the
    bench container, exposing both Grafana MCP tools and an in-process
    ``advance_workflow`` tool to the caller LLM.
    """

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        reasoning_effort: str = "off",
        temperature: float | None = None,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self._extra_env = extra_env or {}

    @staticmethod
    def name() -> str:
        return "o11y-fsm"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        await environment.exec(command="mkdir -p /app/o11y_fsm/harbor")
        # Runner script + prompts.
        await environment.upload_file(source_path=RUNNER_SCRIPT, target_path="/app/agent_runner.py")
        await environment.upload_file(
            source_path=SYSTEM_PROMPT, target_path="/app/system_prompt.txt"
        )
        await environment.upload_file(source_path=TASK_PROMPT, target_path="/app/task_prompt.txt")
        # The o11y_fsm package source so the runner can `import o11y_fsm`.
        for src in PACKAGE_ROOT.rglob("*.py"):
            if "__pycache__" in src.parts:
                continue
            rel = src.relative_to(PACKAGE_ROOT)
            await environment.upload_file(source_path=src, target_path=f"/app/o11y_fsm/{rel}")
        # Vendor burrmcp's source too: it's not on PyPI, so the runner's
        # `import burrmcp` (and o11y_fsm's) can't resolve it from a registry.
        # Upload the installed package source under /app/burrmcp.
        import burrmcp as _bm

        burrmcp_root = Path(_bm.__file__).parent
        await environment.exec(command="mkdir -p /app/burrmcp")
        for src in burrmcp_root.rglob("*.py"):
            if "__pycache__" in src.parts:
                continue
            rel = src.relative_to(burrmcp_root)
            await environment.upload_file(source_path=src, target_path=f"/app/burrmcp/{rel}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        requested_model = self.model_name or "anthropic/claude-sonnet-4-6"
        model = _normalize_litellm_model_name(requested_model)
        mcp_url = _select_remote_mcp_url(self.mcp_servers)

        instruction_path = self.logs_dir / "instruction.txt"
        instruction_path.write_text(instruction)
        await environment.upload_file(
            source_path=instruction_path, target_path="/app/instruction.txt"
        )

        env: dict[str, str] = {
            "MODEL": model,
            "REASONING_EFFORT": self.reasoning_effort,
            "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
            "PYTHONPATH": "/app",
            "O11Y_SCENARIO_TIME_ISO": os.environ.get("O11Y_SCENARIO_TIME_ISO", ""),
        }
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENROUTER_API_KEY"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        gemini = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if gemini:
            env["GEMINI_API_KEY"] = gemini
            env["GOOGLE_API_KEY"] = gemini
        if mcp_url:
            env["MCP_URL"] = mcp_url
        if self.temperature is not None:
            env["TEMPERATURE"] = str(self.temperature)
        env.update(self._extra_env)

        self.logger.info(f"Running o11y-fsm agent runner with model={model}")
        await environment.exec(command=_build_runner_command(), env=env)

        # Pull artifacts back.
        try:
            await environment.download_file(
                source_path="/logs/agent/trajectory.json",
                target_path=self.logs_dir / "trajectory.json",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"trajectory download failed: {exc}")
        try:
            await environment.download_file(
                source_path="/logs/agent/final_answer.txt",
                target_path=self.logs_dir / "final_answer.txt",
            )
            final_answer = (self.logs_dir / "final_answer.txt").read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"final_answer download failed: {exc}")
            final_answer = ""

        # Surface the FSM's final_answer to Harbor's grading context.
        # The verifier reads either context.final_answer or the agent's
        # accumulated transcript; setting both gives us belt + suspenders.
        if final_answer.strip():
            try:
                context.final_answer = final_answer
            except AttributeError:
                pass
            try:
                context.add_assistant_message(final_answer)
            except AttributeError:
                pass
