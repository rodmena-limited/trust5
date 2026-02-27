"""Language detection and context building for multi-language trust5 support.

Profile definitions (LanguageProfile dataclass, PROFILES dict, lookup tables)
live in ``trust5.core.lang_profiles``.  This module re-exports them so that
existing ``from trust5.core.lang import ...`` statements continue to work.
"""

import glob as _glob
import os

# Re-export everything that other modules import from trust5.core.lang.
# This keeps all existing import paths backward-compatible.
from .lang_profiles import (  # noqa: F401 — re-exports for backward compat
    _COMMON_SKIP,
    _EXT_TO_LANG,
    _EXTENSION_MAP,
    _FRAMEWORK_MARKERS,
    _MANIFEST_TO_LANG,
    _SKIP_DIRS_DETECT,
    _build_ext_map,
)
from .lang_profiles import (
    PROFILES as PROFILES,
)
from .lang_profiles import (
    LanguageProfile as LanguageProfile,
)


def _manifest_exists(project_root: str, manifest: str) -> bool:
    """Check if a manifest file exists, supporting glob patterns (e.g. *.csproj)."""
    if "*" in manifest or "?" in manifest:
        return bool(_glob.glob(os.path.join(project_root, manifest)))
    return os.path.exists(os.path.join(project_root, manifest))


def detect_language(project_root: str) -> str:
    """Detect project language from manifest files, then file extensions."""
    # Primary: check for manifest/config files (most reliable signal)
    for lang, profile in PROFILES.items():
        for manifest in profile.manifest_files:
            if _manifest_exists(project_root, manifest):
                return lang
    for manifest, lang in _MANIFEST_TO_LANG.items():
        if _manifest_exists(project_root, manifest):
            return lang

    # Secondary: scan for source file extensions (helps when manifest files
    # haven't been created yet, e.g. pipeline build time before setup stage).
    return _detect_by_extensions(project_root)


def _detect_by_extensions(project_root: str) -> str:
    """Count source files by extension and return the dominant language."""
    _build_ext_map()
    counts: dict[str, int] = {}
    try:
        for entry in os.scandir(project_root):
            if entry.is_dir():
                if entry.name in _SKIP_DIRS_DETECT or entry.name.startswith("."):
                    continue
                # Scan one level deep for source files
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_file():
                            ext = os.path.splitext(sub.name)[1]
                            lang = _EXT_TO_LANG.get(ext)
                            if lang:
                                counts[lang] = counts.get(lang, 0) + 1
                except (PermissionError, OSError):
                    pass
            elif entry.is_file():
                ext = os.path.splitext(entry.name)[1]
                lang = _EXT_TO_LANG.get(ext)
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    except (PermissionError, FileNotFoundError, OSError):
        pass

    if not counts:
        return "unknown"
    return max(counts, key=lambda k: counts[k])


def detect_framework(project_root: str) -> str | None:
    """Detect framework from config/manifest files (GAP 10)."""
    for marker, framework in _FRAMEWORK_MARKERS.items():
        if os.path.exists(os.path.join(project_root, marker)):
            return framework
    return None


def _generic_profile(language: str) -> LanguageProfile:
    """Build a minimal profile for languages without a dedicated profile.

    The planner's SETUP_COMMANDS and QUALITY_CONFIG take full responsibility
    for tool commands. This profile only provides detection metadata.
    """
    return LanguageProfile(
        language=language,
        extensions=_EXTENSION_MAP.get(language, ()),
        test_command=("echo", "no default test command — see QUALITY_CONFIG"),
        test_verify_command="echo 'run tests via QUALITY_CONFIG'",
        lint_commands=(),
        lint_check_commands=(),
        syntax_check_command=None,
        package_install_prefix="",
        lsp_language_id=language,
        skip_dirs=_COMMON_SKIP,
        manifest_files=(),
        prompt_hints=(
            f"Language: {language}. No built-in profile — the planner MUST provide "
            f"SETUP_COMMANDS and QUALITY_CONFIG for this language."
        ),
    )


def get_profile(language: str) -> LanguageProfile:
    """Get language profile by name. Returns a generic profile for unknown languages."""
    if language in PROFILES:
        return PROFILES[language]
    return _generic_profile(language)


def build_language_context(profile: LanguageProfile) -> str:
    """Build LLM context string from a language profile.

    This injects all language-specific details so that prompts can reference
    "the Project Language section" instead of hardcoding language-specific commands.
    """
    fw = detect_framework(".")
    fw_line = f"- Detected framework: {fw}\n" if fw else ""

    test_cmd = " ".join(profile.test_command) if profile.test_command else "(none)"
    lint_cmds = "; ".join(profile.lint_commands) if profile.lint_commands else "(none)"
    cov_cmd = " ".join(profile.coverage_command) if profile.coverage_command else "(none)"
    syntax_cmd = " ".join(profile.syntax_check_command) if profile.syntax_check_command else "(none)"
    src_roots = ", ".join(profile.source_roots) if profile.source_roots else "(flat layout)"
    manifests = ", ".join(profile.manifest_files) if profile.manifest_files else "(none)"
    path_var = profile.path_env_var or "(none)"

    # Import convention guidance based on source roots.
    if profile.source_roots:
        roots = ", ".join(f"`{r}/`" for r in profile.source_roots)
        import_convention = (
            f"\n**Import Convention:** This project uses a source-root layout ({roots}). "
            f"The source root directory is added to the module path at runtime. "
            f"Imports MUST NOT include the source root prefix. "
            f"For example, if source root is `src/` and the package is `mylib`, "
            f"import as `from mylib.module import X`, NOT `from src.mylib.module import X`.\n"
        )
    else:
        import_convention = ""

    lang_upper = profile.language.upper()

    # Package markers: only mention them for languages that have them.
    pkg_markers = ""
    if profile.language == "python":
        pkg_markers = "- Package markers: `__init__.py` in every package directory\n"
    elif profile.language == "go":
        pkg_markers = "- Package markers: none (Go uses directory-based packages)\n"
    elif profile.language in ("java", "kotlin", "scala"):
        pkg_markers = "- Package markers: none (uses directory structure matching package declaration)\n"
    else:
        pkg_markers = "- Package markers: none required\n"

    # Unknown language guidance
    if profile.language == "unknown":
        unknown_hint = (
            "\n**The project language could not be auto-detected.** "
            "Infer the language from the user's request. "
            "If the request mentions a specific language (e.g., 'C compiler', 'Go server'), use that language. "
            "Do NOT default to Python.\n\n"
        )
    else:
        unknown_hint = ""

    return (
        f"## Project Language\n\n"
        f"**IMPORTANT: This project uses {lang_upper}. "
        f"ALL code, file extensions, build tools, and test frameworks "
        f"MUST be for {lang_upper}. Do NOT use conventions from other languages.**\n\n"
        f"{unknown_hint}"
        f"{profile.prompt_hints}\n\n"
        f"- Test verification command: {profile.test_verify_command}\n"
        f"- Full test command: {test_cmd}\n"
        f"- Lint commands: {lint_cmds}\n"
        f"- Coverage command: {cov_cmd}\n"
        f"- Syntax check command: {syntax_cmd}\n"
        f"- Source extensions: {', '.join(profile.extensions)}\n"
        f"- Package manager: {profile.package_install_prefix}\n"
        f"- Source roots: {src_roots}\n"
        f"- Path env var: {path_var}\n"
        f"- Manifest files: {manifests}\n"
        f"{pkg_markers}"
        f"{fw_line}"
        f"{import_convention}"
        f"\nIMPORTANT: The working directory is the project root. "
        f"Do NOT cd to /testbed or other paths. "
        f"STOP immediately after tests pass and files are verified — "
        f"return your summary.\n"
    )
