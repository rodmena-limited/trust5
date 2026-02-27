import os
import subprocess
import time
from pathlib import Path
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.agent import Agent
from ..core.config import ConfigManager
from ..core.lang import detect_language, get_profile
from ..core.llm import LLM
from ..core.lsp import LSPClient
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit


class RalphLoop:
    """Continuous LSP-driven diagnostics loop that fixes issues iteratively via LLM agents."""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.config_manager = ConfigManager(project_root)
        self.config = self.config_manager.load_config()

        language = detect_language(project_root)
        self.profile = get_profile(language)

        lsp_cmd = self.config.language.lsp_command
        self.lsp = LSPClient(lsp_cmd, root_uri=f"file://{os.path.abspath(project_root)}")

        self.max_iterations = 100
        self.iteration_count = 0

    def start_loop(self) -> None:
        emit(M.LSTR, "Starting Ralph Loop...")
        try:
            self.lsp.start()
            time.sleep(2)

            while self.iteration_count < self.max_iterations:
                self.iteration_count += 1
                emit(M.LITR, f"Iteration {self.iteration_count}/{self.max_iterations}")

                issues = self.diagnose()
                if not issues:
                    emit(M.LEND, "No issues found. Loop complete.")
                    break

                emit(M.LDIG, f"Found {len(issues)} issues.")

                for issue in issues:
                    self.fix_issue(issue)

                time.sleep(1)

        except (OSError, RuntimeError) as e:  # loop: LSP/subprocess/IO errors
            emit(M.LERR, f"Loop Error: {e}")
        finally:
            self.lsp.stop()

    def diagnose(self) -> list[dict[str, Any]]:
        issues = []

        source_files = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in self.profile.skip_dirs]
            for file in files:
                if any(file.endswith(ext) for ext in self.profile.extensions):
                    source_files.append(os.path.join(root, file))

        for file_path in source_files:
            uri = f"file://{os.path.abspath(file_path)}"
            self.lsp.rpc.send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.profile.lsp_language_id,
                        "version": 1,
                        "text": Path(file_path).read_text(),
                    }
                },
            )
            time.sleep(0.5)

            diags = self.lsp.get_diagnostics(uri)
            for d in diags:
                issues.append(
                    {
                        "type": "lsp_error",
                        "file": file_path,
                        "message": d.get("message"),
                        "severity": d.get("severity"),
                        "range": d.get("range"),
                    }
                )

        test_cmd = self.config.language.test_framework
        if test_cmd:
            try:
                result = subprocess.run(
                    test_cmd,
                    shell=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    issues.append(
                        {
                            "type": "test_failure",
                            "message": result.stderr or result.stdout,
                            "file": "tests",
                        }
                    )
            except (subprocess.SubprocessError, OSError) as e:  # test runner errors
                issues.append({"type": "test_error", "message": str(e)})

        return issues

    def fix_issue(self, issue: dict[str, Any]) -> None:
        emit(M.LFIX, f"Fixing issue: {issue['message']}")

        agent_name = "expert-debug"
        prompt_file = "expert-debug.md"

        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompt_path = os.path.join(base_path, "assets", "prompts", prompt_file)
        with open(prompt_path) as f:
            system_prompt = f.read()

        issue_context = (
            f"\n\nActive Issue:\nType: {issue['type']}\nFile: {issue.get('file')}\nMessage: {issue['message']}\n"
        )
        system_prompt += issue_context

        llm = LLM.for_tier("fast", stage_name=agent_name)
        with mcp_clients() as mcp:
            agent = Agent(name=agent_name, prompt=system_prompt, llm=llm, mcp_clients=mcp)
            agent.run(f"Fix this issue: {issue['message']}")


class LoopTask(Task):
    """Stabilize task wrapper that runs a single RalphLoop to completion."""

    def execute(self, stage: StageExecution) -> TaskResult:
        loop = RalphLoop(os.getcwd())
        loop.start_loop()
        return TaskResult.success(outputs={"status": "loop_complete"})
