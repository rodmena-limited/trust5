"""LLM-based code review task — semantic analysis between repair and quality gate."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from stabilize import StageExecution, Task, TaskResult

from ..core.agent import Agent
from ..core.config import ConfigManager, QualityConfig
from ..core.context_builder import (
    MAX_TOTAL_CONTEXT,
    _find_source_files,
    _read_file_safe,
    build_spec_context,
)
from ..core.context_keys import increment_jump_count, propagate_context
from ..core.lang import LanguageProfile, build_language_context, detect_language, get_profile
from ..core.llm import LLM
from ..core.mcp_manager import mcp_clients
from ..core.message import M, emit, emit_block

logger = logging.getLogger(__name__)

REVIEW_CATEGORIES = (
    "code-duplication",
    "deprecated-api",
    "design-smell",
    "error-handling",
    "performance",
    "security",
    "test-quality",
)

_FINDINGS_RE = re.compile(
    r"<!--\s*REVIEW_FINDINGS\s+JSON\s*\n(.*?)\n\s*-->",
    re.DOTALL,
)

REVIEWER_TOOLS = ["Read", "ReadFiles", "Glob", "Grep"]


@dataclass
class ReviewFinding:
    severity: str  # "error" | "warning" | "info"
    category: str  # one of REVIEW_CATEGORIES
    file: str
    line: int
    description: str


@dataclass
class ReviewReport:
    findings: list[ReviewFinding] = field(default_factory=list)
    summary_score: float = 1.0
    total_errors: int = 0
    total_warnings: int = 0
    total_info: int = 0


def parse_review_findings(raw_output: str) -> ReviewReport:
    """Parse structured findings from the LLM's review output."""
    match = _FINDINGS_RE.search(raw_output)
    if not match:
        # Fallback: no valid JSON block — return advisory info finding
        return ReviewReport(
            findings=[
                ReviewFinding(
                    severity="info",
                    category="design-smell",
                    file="",
                    line=0,
                    description="Review completed but produced no structured findings.",
                )
            ],
            summary_score=0.7,
            total_info=1,
        )

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return ReviewReport(
            findings=[
                ReviewFinding(
                    severity="info",
                    category="design-smell",
                    file="",
                    line=0,
                    description="Review produced malformed JSON — treating as advisory.",
                )
            ],
            summary_score=0.7,
            total_info=1,
        )

    findings: list[ReviewFinding] = []
    for item in data.get("findings", []):
        if not isinstance(item, dict):
            continue
        findings.append(
            ReviewFinding(
                severity=str(item.get("severity", "info")),
                category=str(item.get("category", "design-smell")),
                file=str(item.get("file", "")),
                line=int(item.get("line", 0)),
                description=str(item.get("description", "")),
            )
        )

    return ReviewReport(
        findings=findings,
        summary_score=float(data.get("summary_score", 0.7)),
        total_errors=int(data.get("total_errors", 0)),
        total_warnings=int(data.get("total_warnings", 0)),
        total_info=int(data.get("total_info", 0)),
    )


class ReviewTask(Task):
    """LLM-based code review: reads source files and produces structured findings."""

    def execute(self, stage: StageExecution) -> TaskResult:
        project_root = stage.context.get("project_root", os.getcwd())
        profile_data = stage.context.get("language_profile", {})
        config = self._load_config(project_root)

        if not config.code_review_enabled:
            emit(M.RVST, "Code review disabled — skipping")
            return TaskResult.success(outputs={"review_passed": True, "review_skipped": True})

        profile = self._build_profile(profile_data, project_root)
        emit(M.RVST, f"Code review started [{profile.language}]")

        # Build the review prompt
        prompt = self._build_review_prompt(stage, project_root, profile)

        # Load the system prompt from the asset file
        system_prompt = self._load_system_prompt()

        # Run the review agent
        try:
            llm = LLM.for_tier(
                tier=config.review_model_tier,
                stage_name="review",
            )
            with mcp_clients() as clients:
                agent = Agent(
                    name="reviewer",
                    prompt=system_prompt,
                    llm=llm,
                    mcp_clients=clients,
                    non_interactive=True,
                    allowed_tools=REVIEWER_TOOLS,
                )
                raw_output = agent.run(
                    prompt,
                    max_turns=config.review_max_turns,
                )
        except Exception as e:
            emit(M.RVFL, f"Review agent error: {e}")
            return TaskResult.failed_continue(
                error=f"Review agent failed: {e}",
                outputs={
                    "review_passed": False,
                    "review_score": 0.0,
                    "review_error": str(e),
                },
            )

        # Parse findings
        report = parse_review_findings(raw_output)
        self._emit_report(report)

        # Determine pass/fail
        passed = report.total_errors == 0 and report.summary_score >= 0.8

        if passed:
            emit(
                M.RVPS,
                f"Code review PASSED — score {report.summary_score:.2f} "
                f"({report.total_warnings} warnings, {report.total_info} info)",
            )
            return TaskResult.success(
                outputs=self._build_outputs(report, passed=True),
            )

        # Review failed — decide whether to jump to repair or accept advisory
        if config.code_review_jump_to_repair and report.total_errors > 0:
            emit(
                M.RVFL,
                f"Code review FAILED — score {report.summary_score:.2f} "
                f"({report.total_errors} errors). Jumping to repair.",
            )
            jump_repair_ref = stage.context.get("jump_repair_ref", "repair")
            feedback = self._format_repair_feedback(report)
            jump_context: dict[str, Any] = {
                "_repair_requested": True,
                "test_output": feedback[:6000],
                "failure_type": "review",
                "project_root": project_root,
                "language_profile": profile_data,
            }
            propagate_context(stage.context, jump_context)
            increment_jump_count(jump_context)
            return TaskResult.jump_to(jump_repair_ref, context=jump_context)

        # Advisory mode: report findings but don't block pipeline
        emit(
            M.RVFL,
            f"Code review FAILED (advisory) — score {report.summary_score:.2f} "
            f"({report.total_errors} errors, {report.total_warnings} warnings)",
        )
        return TaskResult.failed_continue(
            error=f"Code review failed (score={report.summary_score:.2f})",
            outputs=self._build_outputs(report, passed=False),
        )

    def _build_review_prompt(
        self,
        stage: StageExecution,
        project_root: str,
        profile: LanguageProfile,
    ) -> str:
        """Build the user prompt for the review agent with full context."""
        parts: list[str] = []

        # Plan context (no amnesia — carry forward from upstream)
        ancestor_outputs = stage.context.get("ancestor_outputs", {})
        plan_output = ancestor_outputs.get("plan", "")
        if plan_output:
            parts.append(f"## Plan Output\n\n{plan_output[:4000]}")

        # SPEC context
        plan_config = stage.context.get("plan_config", {})
        spec_id = plan_config.get("spec_id", "")
        if spec_id:
            spec_ctx = build_spec_context(spec_id, project_root)
            parts.append(f"## SPEC Context\n\n{spec_ctx}")

        # Source files
        extensions = profile.extensions or (".py",)
        skip_dirs = profile.skip_dirs or ()
        source_files = _find_source_files(project_root, extensions)

        source_parts: list[str] = []
        test_parts: list[str] = []
        total_len = 0

        for fpath in source_files:
            if total_len >= MAX_TOTAL_CONTEXT:
                break
            rel = os.path.relpath(fpath, project_root)
            if any(sd in rel for sd in skip_dirs):
                continue
            content = _read_file_safe(fpath)
            if "test" in rel.lower():
                test_parts.append(f"--- {rel} ---\n{content}")
            else:
                source_parts.append(f"--- {rel} ---\n{content}")
            total_len += len(content)

        if source_parts:
            parts.append("## Source Files\n\n" + "\n\n".join(source_parts))
        if test_parts:
            parts.append("## Test Files\n\n" + "\n\n".join(test_parts))

        # Language-specific context (injected dynamically — requirement A)
        lang_ctx = build_language_context(profile)
        parts.append(lang_ctx)

        parts.append(
            f"\nWORKING DIRECTORY: {project_root}\n"
            f"Review the code above and produce your findings in the required format."
        )

        return "\n\n".join(parts)

    @staticmethod
    def _load_system_prompt() -> str:
        """Load the reviewer system prompt from the assets directory."""
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "assets",
            "prompts",
            "reviewer.md",
        )
        try:
            with open(prompt_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return (
                "You are a code reviewer. Review the provided source code "
                "and produce structured findings in JSON format."
            )

    @staticmethod
    def _load_config(project_root: str) -> QualityConfig:
        try:
            mgr = ConfigManager(project_root)
            cfg = mgr.load_config()
            return cfg.quality
        except Exception as e:
            logger.warning("Failed to load config for review: %s — using defaults", e)
            return QualityConfig()

    @staticmethod
    def _build_profile(data: dict[str, Any], project_root: str = ".") -> LanguageProfile:
        if not data:
            detected = detect_language(project_root)
            return get_profile(detected)

        detected = detect_language(project_root)
        base = get_profile(detected)

        def _tup(v: Any) -> tuple[str, ...] | None:
            if v is None:
                return None
            if isinstance(v, (list, tuple)):
                return tuple(v)
            return (str(v),)

        return LanguageProfile(
            language=data.get("language", base.language),
            extensions=tuple(data.get("extensions", base.extensions)),
            test_command=tuple(data.get("test_command", base.test_command)),
            test_verify_command=data.get("test_verify_command", base.test_verify_command),
            lint_commands=tuple(data.get("lint_commands", base.lint_commands)),
            lint_check_commands=tuple(data.get("lint_check_commands", base.lint_check_commands)),
            syntax_check_command=(
                _tup(data.get("syntax_check_command")) if "syntax_check_command" in data else base.syntax_check_command
            ),
            package_install_prefix=data.get("package_install_prefix", base.package_install_prefix),
            lsp_language_id=data.get("lsp_language_id", base.lsp_language_id),
            skip_dirs=tuple(data.get("skip_dirs", base.skip_dirs)),
            manifest_files=tuple(data.get("manifest_files", base.manifest_files)),
            prompt_hints=data.get("prompt_hints", base.prompt_hints),
            coverage_command=(
                _tup(data.get("coverage_command")) if "coverage_command" in data else base.coverage_command
            ),
            security_command=(
                _tup(data.get("security_command")) if "security_command" in data else base.security_command
            ),
        )

    @staticmethod
    def _emit_report(report: ReviewReport) -> None:
        """Emit the review report as a structured text block."""
        lines = [
            f"Score: {report.summary_score:.2f}  |  "
            f"Errors: {report.total_errors}  |  "
            f"Warnings: {report.total_warnings}  |  "
            f"Info: {report.total_info}",
            "",
            f"{'SEV':<8} {'CATEGORY':<18} {'LOCATION':<30} DESCRIPTION",
            f"{'---':<8} {'--------':<18} {'--------':<30} -----------",
        ]
        for finding in report.findings[:20]:
            sev = finding.severity.upper()
            loc = f"{finding.file}:{finding.line}" if finding.file else "\u2014"
            lines.append(f"{sev:<8} {finding.category:<18} {loc:<30} {finding.description}")
        emit_block(M.RVRP, "Code Review Report", "\n".join(lines), max_lines=30)

    @staticmethod
    def _build_outputs(report: ReviewReport, passed: bool) -> dict[str, Any]:
        return {
            "review_passed": passed,
            "review_score": report.summary_score,
            "review_findings": [asdict(f) for f in report.findings],
            "review_errors": report.total_errors,
            "review_warnings": report.total_warnings,
        }

    @staticmethod
    def _format_repair_feedback(report: ReviewReport) -> str:
        """Format review findings as repair input."""
        parts = [
            "CODE REVIEW FAILED — fix the following issues:\n",
        ]
        for finding in report.findings:
            if finding.severity == "error":
                loc = f" [{finding.file}:{finding.line}]" if finding.file else ""
                parts.append(f"  - [{finding.severity.upper()}][{finding.category}]{loc} {finding.description}")
        parts.append("\nFix these issues and ensure all tests still pass.")
        return "\n".join(parts)
