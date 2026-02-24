from typing import Any

from ..core.message import M

# ─── Color Palette ───────────────────────────────────────────────────────────
# Warm "posh" palette: champagne whites, warm greys, gold/copper/rose accents.

C_BG = "#0c0a08"
C_SURFACE = "#151210"
C_BORDER = "#2a2420"
C_CHROME = "#3a322c"

C_TEXT = "#e8ddd0"
C_SECONDARY = "#b0a898"
C_MUTED = "#706860"
C_DIM = "#483f38"

# Semantic accents
C_BLUE = "#d4a054"  # Gold — info, primary
C_TEAL = "#7ab08a"  # Sage — tools
C_GREEN = "#8cc084"  # Green — success
C_AMBER = "#d4943c"  # Copper — warnings, thinking
C_RED = "#c87070"  # Rose — errors
C_LAVENDER = "#b08cb8"  # Mauve — stages
C_PURPLE = "#a87fd4"  # Purple — watchdog

C_DIM_GREEN = "#68986c"
C_DIM_AMBER = "#a07838"
C_DIM_RED = "#985858"
C_DIM_LAVENDER = "#887098"
C_DIM_PURPLE = "#7c5ca0"

# ─── Unicode Symbols ─────────────────────────────────────────────────────────
# Clean, intuitive symbols for each message type

SYM_DOT = "\u25cf"  # ● — info/status
SYM_SUCCESS = "\u2713"  # ✓ — success
SYM_ERROR = "\u2717"  # ✗ — error
SYM_WARNING = "\u26a0"  # ⚠ — warning
SYM_ARROW = "\u25b8"  # ▸ — action/tool call
SYM_GEAR = "\u2699"  # ⚙ — processing/work
SYM_SEARCH = "\u2315"  # ⌕ — search/grep
SYM_FOLDER = "\u25b7"  # ▷ — file operations
SYM_STAR = "\u2605"  # ★ — quality/important
SYM_CIRCLE = "\u25cb"  # ○ — stage transitions
SYM_PLAY = "\u25b6"  # ▶ — workflow start
SYM_STOP = "\u25a0"  # ■ — workflow end
SYM_WATCHDOG = "\u25c9"  # ◉ — watchdog
SYM_RETRY = "\u21bb"  # ↻ — retry/loop
SYM_JUMP = "\u21e8"  # ⇨ — jump
SYM_SKIP = "\u21b7"  # ↷ — skip
SYM_CODE = "\u270e"  # ✎ — code/edit
SYM_SHELL = "\u25a1"  # □ — shell/bash
SYM_PACKAGE = "\u25e6"  # ◦ — package
SYM_INPUT = "\u25c0"  # ◀ — input
SYM_OUTPUT = "\u25b6"  # ▶ — output
SYM_QUESTION = "\u25d0"  # ◐ — question

# ─── Theme Map ───────────────────────────────────────────────────────────────
# marker: unicode symbol with optional padding for alignment
# color: accent color for the symbol
# pill: True = render with colored background (major events)

THEME: dict[str, dict[str, Any]] = {
    # Agent / LLM
    M.ATRN: {"marker": f"{SYM_GEAR} ", "color": C_DIM, "title": "Turn", "pill": False},
    M.ATHK: {"marker": f"{SYM_GEAR} ", "color": C_AMBER, "title": "Thinking", "pill": False},
    M.ARSP: {"marker": f"{SYM_ARROW} ", "color": C_GREEN, "title": "Response", "pill": False},
    M.ASUM: {"marker": f"{SYM_ARROW} ", "color": C_GREEN, "title": "Summary", "pill": False},
    M.AERR: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Error", "pill": True},
    M.ARTY: {"marker": f"{SYM_RETRY} ", "color": C_AMBER, "title": "Retry", "pill": True},
    M.AFBK: {"marker": f"{SYM_RETRY} ", "color": C_AMBER, "title": "Fallback", "pill": True},
    # Context / LLM Communication
    M.CSYS: {"marker": f"{SYM_DOT} ", "color": C_DIM, "title": "System", "pill": False},
    M.CUSR: {"marker": f"{SYM_INPUT} ", "color": C_BLUE, "title": "Input", "pill": False},
    M.CAST: {"marker": f"{SYM_OUTPUT} ", "color": C_SECONDARY, "title": "Assistant", "pill": False},
    M.CTLC: {"marker": f"{SYM_ARROW} ", "color": C_TEAL, "title": "Tool Call", "pill": False},
    M.CTLR: {"marker": f"{SYM_ARROW} ", "color": C_DIM, "title": "Result", "pill": False},
    # Tools
    M.TCAL: {"marker": f"{SYM_ARROW} ", "color": C_TEAL, "title": "Tool", "pill": False},
    M.TRES: {"marker": f"{SYM_ARROW} ", "color": C_DIM, "title": "Result", "pill": False},
    M.TBSH: {"marker": f"{SYM_SHELL}", "color": C_TEAL, "title": "Shell", "pill": False},
    M.TWRT: {"marker": f"{SYM_CODE}", "color": C_GREEN, "title": "Write", "pill": False},
    M.TRED: {"marker": f"{SYM_FOLDER}", "color": C_BLUE, "title": "Read", "pill": False},
    M.TEDT: {"marker": f"{SYM_CODE}", "color": C_GREEN, "title": "Edit", "pill": False},
    M.TGLB: {"marker": f"{SYM_FOLDER}", "color": C_DIM, "title": "Glob", "pill": False},
    M.TGRP: {"marker": f"{SYM_SEARCH}", "color": C_DIM, "title": "Search", "pill": False},
    M.TPKG: {"marker": f"{SYM_PACKAGE}", "color": C_TEAL, "title": "Package", "pill": False},
    M.TINI: {"marker": f"{SYM_PLAY} ", "color": C_TEAL, "title": "Init", "pill": False},
    # Workflow
    M.WSTR: {"marker": f"{SYM_PLAY} ", "color": C_BLUE, "title": "Workflow", "pill": True},
    M.WSUC: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Success", "pill": True},
    M.WFAL: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Failed", "pill": True},
    M.WTMO: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Timeout", "pill": True},
    M.WRCV: {"marker": f"{SYM_RETRY} ", "color": C_AMBER, "title": "Recovered", "pill": True},
    M.WSTG: {"marker": f"{SYM_CIRCLE} ", "color": C_LAVENDER, "title": "Stage", "pill": True},
    M.WJMP: {"marker": f"{SYM_JUMP} ", "color": C_AMBER, "title": "Jump", "pill": True},
    M.WSKP: {"marker": f"{SYM_SKIP} ", "color": C_MUTED, "title": "Skipped", "pill": True},
    M.WINT: {"marker": f"{SYM_STOP} ", "color": C_RED, "title": "Interrupted", "pill": True},
    # Validation / Testing
    M.VRUN: {"marker": f"{SYM_GEAR} ", "color": C_LAVENDER, "title": "Testing", "pill": True},
    M.VPAS: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Tests Passed", "pill": True},
    M.VFAL: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Tests Failed", "pill": True},
    M.VTST: {"marker": f"{SYM_DOT} ", "color": C_DIM_LAVENDER, "title": "Test Output", "pill": False},
    # Repair
    M.RSTR: {"marker": f"{SYM_GEAR} ", "color": C_AMBER, "title": "Repair", "pill": True},
    M.REND: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Repair Done", "pill": True},
    M.RFAL: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Repair Failed", "pill": True},
    M.RJMP: {"marker": f"{SYM_JUMP} ", "color": C_AMBER, "title": "Repair Jump", "pill": True},
    M.RSKP: {"marker": f"{SYM_SKIP} ", "color": C_MUTED, "title": "Repair Skipped", "pill": True},
    # Quality Gate
    M.QRUN: {"marker": f"{SYM_STAR} ", "color": C_LAVENDER, "title": "Quality", "pill": True},
    M.QPAS: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Quality OK", "pill": True},
    M.QFAL: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Quality Failed", "pill": True},
    M.QJMP: {"marker": f"{SYM_JUMP} ", "color": C_AMBER, "title": "Quality Jump", "pill": True},
    M.QVAL: {"marker": f"{SYM_DOT} ", "color": C_DIM_LAVENDER, "title": "Validation", "pill": False},
    M.QRPT: {"marker": f"{SYM_STAR} ", "color": C_AMBER, "title": "Quality Report", "pill": False},
    # Code Review
    M.RVST: {"marker": f"{SYM_SEARCH}", "color": C_LAVENDER, "title": "Review", "pill": True},
    M.RVPS: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Review OK", "pill": True},
    M.RVFL: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Review Failed", "pill": True},
    M.RVRP: {"marker": f"{SYM_DOT} ", "color": C_AMBER, "title": "Review Report", "pill": False},
    # System
    M.SINF: {"marker": f"{SYM_DOT} ", "color": C_BLUE, "title": "Info", "pill": True},
    M.SWRN: {"marker": f"{SYM_WARNING}", "color": C_AMBER, "title": "Warning", "pill": True},
    M.SERR: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Error", "pill": True},
    # Artifacts / Code
    M.KDIF: {"marker": f"{SYM_CODE}", "color": C_TEAL, "title": "Diff", "pill": False},
    M.KCOD: {"marker": f"{SYM_CODE}", "color": C_TEAL, "title": "Code", "pill": False},
    M.PPLN: {"marker": f"{SYM_PLAY} ", "color": C_BLUE, "title": "Plan", "pill": True},
    # Loop
    M.LSTR: {"marker": f"{SYM_RETRY} ", "color": C_LAVENDER, "title": "Loop", "pill": True},
    M.LEND: {"marker": f"{SYM_SUCCESS} ", "color": C_GREEN, "title": "Loop Done", "pill": True},
    M.LFIX: {"marker": f"{SYM_GEAR} ", "color": C_TEAL, "title": "Fix", "pill": False},
    M.LERR: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Loop Error", "pill": True},
    M.LITR: {"marker": f"{SYM_RETRY} ", "color": C_DIM_LAVENDER, "title": "Iteration", "pill": False},
    M.LDIG: {"marker": f"{SYM_DOT} ", "color": C_DIM_LAVENDER, "title": "Diagnostics", "pill": False},
    # Watchdog
    M.WDST: {"marker": f"{SYM_WATCHDOG}", "color": C_PURPLE, "title": "Watchdog", "pill": True},
    M.WDOK: {"marker": f"{SYM_DOT} ", "color": C_DIM_PURPLE, "title": "Watchdog OK", "pill": False},
    M.WDWN: {"marker": f"{SYM_WARNING}", "color": C_PURPLE, "title": "Watchdog", "pill": True},
    M.WDER: {"marker": f"{SYM_ERROR} ", "color": C_RED, "title": "Watchdog", "pill": True},
    # User
    M.UASK: {"marker": f"{SYM_QUESTION}", "color": C_AMBER, "title": "Question", "pill": True},
    M.UAUT: {"marker": f"{SYM_ARROW} ", "color": C_AMBER, "title": "Auto-Answer", "pill": True},
}

_DEFAULT_THEME: dict[str, Any] = {"marker": f"{SYM_DOT} ", "color": C_SECONDARY, "title": "", "pill": False}


def get_theme(code: str) -> dict[str, Any]:
    return THEME.get(code, _DEFAULT_THEME)


# Events that route ONLY to status bars, never to the main log.
STATUS_BAR_ONLY: frozenset[str] = frozenset(
    {
        M.MMDL,
        M.MPRF,
        M.MTKN,
        M.MCTX,
        M.MBDG,
        M.SELP,
        M.SPRG,
        M.FCHG,
        M.GSTS,
        M.CREQ,
        M.CRES,
        M.CTKN,
        M.CMDL,
        M.ATRN,
        M.SDBG,
        M.SDB,
        M.SCFG,
        M.PPRG,
        M.TRES,
        M.CTLR,
    }
)
