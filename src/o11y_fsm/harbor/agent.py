"""Harbor BaseAgent that drives o11y-fsm inside the bench container.

The agent_runner.py + the o11y_fsm package source are uploaded into
the container at /app/. The runner uses uv's PEP 723 inline-script
dep declaration to install apache-burr + fastmcp + litellm + mcp on
first run; theodosia source is vendored alongside o11y_fsm into /app.
"""

from __future__ import annotations

import contextlib
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
# (its PEP 723 deps install fastmcp, apache-burr, etc.; theodosia is vendored).
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
    bench container. Single surface: the caller LLM sees only the FSM
    actions. Grafana is bound as a Theodosia upstream and reached from
    inside the query actions; its tools are never exposed to the LLM.
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
        # Vendor both packages' source into the container. o11y_fsm is not on
        # PyPI; theodosia is, but its apache-burr[tracking] extra pulls psutil,
        # which has no prebuilt wheel for the gcc-less bench image, so we vendor
        # theodosia's source and the runner's PEP 723 deps pin apache-burr
        # without [tracking]. Create every needed subdir first (rglob hits
        # nested packages like theodosia/_experimental), then upload.
        import theodosia as _th

        await self._upload_package(environment, PACKAGE_ROOT, "/app/o11y_fsm")
        await self._upload_package(environment, Path(_th.__file__).parent, "/app/theodosia")

    @staticmethod
    async def _upload_package(environment: BaseEnvironment, root: Path, dest: str) -> None:
        files = [s for s in root.rglob("*.py") if "__pycache__" not in s.parts]
        dirs = sorted({str(Path(dest) / s.relative_to(root).parent) for s in files})
        for d in dirs:
            await environment.exec(command=f"mkdir -p {d}")
        for src in files:
            rel = src.relative_to(root)
            await environment.upload_file(source_path=src, target_path=f"{dest}/{rel}")

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
        with contextlib.suppress(Exception):
            await environment.download_file(
                source_path="/logs/agent/final_answer.txt",
                target_path=self.logs_dir / "final_answer.txt",
            )
        # The verifier reads the response from the downloaded trajectory.json
        # (ATIF format) via grading/transcript_parser.py, so the runner records
        # the conversation and the final answer there. Nothing to set on the
        # AgentContext (it has no final_answer field).
