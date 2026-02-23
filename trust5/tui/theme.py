from typing import Any

from ..core.message import M

# ─── Color Palette ───────────────────────────────────────────────────────────
# Warm "posh" palette: champagne whites, warm greys, gold/copper/rose accents.
# Designed to feel welcoming and refined — not neon-hacker.

C_BG = "#0c0a08"  # Near-black with warm brown undertone
C_SURFACE = "#151210"  # Dark chocolate surface
C_BORDER = "#2a2420"  # Warm dark border
C_CHROME = "#3a322c"  # Warm separator

C_TEXT = "#e8ddd0"  # Cream white — warm primary text
C_SECONDARY = "#b0a898"  # Warm taupe — normal messages
C_MUTED = "#706860"  # Warm grey — timestamps, noise
C_DIM = "#483f38"  # Dark warm grey — decorative, faint

# Semantic accents — warm hues, each visually distinct
C_BLUE = "#d4a054"  # Warm gold — primary accent, headers, brand
C_TEAL = "#7ab08a"  # Sage green — tool operations
C_GREEN = "#8cc084"  # Warm green — success
C_AMBER = "#d4943c"  # Copper — thinking, warnings, retries
C_RED = "#c87070"  # Dusty rose — errors, failures
C_LAVENDER = "#b08cb8"  # Dusty mauve — stages, validation
C_PURPLE = "#a87fd4"  # Warm purple — watchdog monitoring

# Dim variants for subtle/background use
C_DIM_TEAL = "#5a8068"
C_DIM_GREEN = "#68986c"
C_DIM_AMBER = "#a07838"
C_DIM_RED = "#985858"
C_DIM_LAVENDER = "#887098"
C_DIM_PURPLE = "#7c5ca0"

# ─── Theme Map ───────────────────────────────────────────────────────────────
# marker: 4-char fixed-width badge text (for left-column alignment)
# color: accent color for the marker
# pill: True = render marker with colored background (major events)

THEME: dict[str, dict[str, Any]] = {
    # Agent
    M.ATHK: {"marker": " .. ", "color": C_AMBER, "title": "Thinking", "pill": False},
    M.ARSP: {"marker": " >> ", "color": C_GREEN, "title": "Response", "pill": False},
    M.ASUM: {"marker": " >> ", "color": C_GREEN, "title": "Summary", "pill": False},
    M.AERR: {"marker": " !! ", "color": C_RED, "title": "Error", "pill": True},
    M.ARTY: {"marker": " <> ", "color": C_AMBER, "title": "Retry", "pill": True},
    M.AFBK: {"marker": " <> ", "color": C_AMBER, "title": "Fallback", "pill": True},
    # Context
    M.CSYS: {"marker": " SYS", "color": C_DIM, "title": "System", "pill": False},
    M.CUSR: {"marker": " IN ", "color": C_BLUE, "title": "Input", "pill": False},
    M.CAST: {"marker": " AI ", "color": C_SECONDARY, "title": "Assistant", "pill": False},
    M.CTLC: {"marker": "  > ", "color": C_TEAL, "title": "Call", "pill": False},
    M.CTLR: {"marker": "  < ", "color": C_DIM, "title": "Result", "pill": False},
    # Tools
    M.TCAL: {"marker": "  > ", "color": C_TEAL, "title": "Tool", "pill": False},
    M.TRES: {"marker": "  < ", "color": C_DIM, "title": "Result", "pill": False},
    M.TBSH: {"marker": "  $ ", "color": C_TEAL, "title": "Shell", "pill": False},
    M.TWRT: {"marker": "  W ", "color": C_GREEN, "title": "Write", "pill": False},
    M.TRED: {"marker": "  R ", "color": C_BLUE, "title": "Read", "pill": False},
    M.TEDT: {"marker": "  E ", "color": C_GREEN, "title": "Edit", "pill": False},
    M.TGLB: {"marker": "  G ", "color": C_DIM_TEAL, "title": "Glob", "pill": False},
    M.TGRP: {"marker": "  S ", "color": C_DIM_TEAL, "title": "Search", "pill": False},
    M.TPKG: {"marker": " PKG", "color": C_TEAL, "title": "Package", "pill": False},
    M.TINI: {"marker": "INIT", "color": C_TEAL, "title": "Init", "pill": False},
    # Workflow
    M.WSTR: {"marker": " >> ", "color": C_BLUE, "title": "Workflow", "pill": True},
    M.WSUC: {"marker": " OK ", "color": C_GREEN, "title": "Success", "pill": True},
    M.WFAL: {"marker": "FAIL", "color": C_RED, "title": "Failed", "pill": True},
    M.WTMO: {"marker": " TO ", "color": C_RED, "title": "Timeout", "pill": True},
    M.WRCV: {"marker": " RC ", "color": C_AMBER, "title": "Recovered", "pill": True},
    M.WSTG: {"marker": " >> ", "color": C_LAVENDER, "title": "Stage", "pill": True},
    M.WJMP: {"marker": " JMP", "color": C_AMBER, "title": "Jump", "pill": True},
    M.WSKP: {"marker": "SKIP", "color": C_MUTED, "title": "Skipped", "pill": True},
    M.WINT: {"marker": " INT", "color": C_RED, "title": "Interrupted", "pill": True},
    # Validation
    M.VRUN: {"marker": "TEST", "color": C_LAVENDER, "title": "Validation", "pill": True},
    M.VPAS: {"marker": "PASS", "color": C_GREEN, "title": "Tests Passed", "pill": True},
    M.VFAL: {"marker": "FAIL", "color": C_RED, "title": "Tests Failed", "pill": True},
    M.VTST: {"marker": "TEST", "color": C_DIM_LAVENDER, "title": "Test Output", "pill": False},
    # Repair
    M.RSTR: {"marker": " FIX", "color": C_AMBER, "title": "Repair", "pill": True},
    M.REND: {"marker": " FIX", "color": C_GREEN, "title": "Repair Done", "pill": True},
    M.RFAL: {"marker": " FIX", "color": C_RED, "title": "Repair Failed", "pill": True},
    M.RJMP: {"marker": " JMP", "color": C_AMBER, "title": "Repair Jump", "pill": True},
    M.RSKP: {"marker": "SKIP", "color": C_MUTED, "title": "Repair Skipped", "pill": True},
    # Quality
    M.QRUN: {"marker": "GATE", "color": C_LAVENDER, "title": "Quality", "pill": True},
    M.QPAS: {"marker": "GATE", "color": C_GREEN, "title": "Quality OK", "pill": True},
    M.QFAL: {"marker": "GATE", "color": C_RED, "title": "Quality Failed", "pill": True},
    M.QJMP: {"marker": " JMP", "color": C_AMBER, "title": "Quality Jump", "pill": True},
    M.QVAL: {"marker": "  QA", "color": C_DIM_LAVENDER, "title": "Validation", "pill": False},
    M.QRPT: {"marker": " QA ", "color": C_AMBER, "title": "Quality Report", "pill": False},
    # Code Review
    M.RVST: {"marker": " REV", "color": C_LAVENDER, "title": "Review", "pill": True},
    M.RVPS: {"marker": " REV", "color": C_GREEN, "title": "Review OK", "pill": True},
    M.RVFL: {"marker": " REV", "color": C_RED, "title": "Review Failed", "pill": True},
    M.RVRP: {"marker": " RV ", "color": C_AMBER, "title": "Review Report", "pill": False},
    # System
    M.SINF: {"marker": "  i ", "color": C_BLUE, "title": "Info", "pill": True},
    M.SWRN: {"marker": "  ! ", "color": C_AMBER, "title": "Warning", "pill": True},
    M.SERR: {"marker": " !! ", "color": C_RED, "title": "Error", "pill": True},
    # Artifacts
    M.KDIF: {"marker": "DIFF", "color": C_TEAL, "title": "Diff", "pill": False},
    M.KCOD: {"marker": "CODE", "color": C_TEAL, "title": "Code", "pill": False},
    M.PPLN: {"marker": "PLAN", "color": C_BLUE, "title": "Plan", "pill": True},
    # Loop
    M.LSTR: {"marker": "LOOP", "color": C_LAVENDER, "title": "Loop", "pill": True},
    M.LEND: {"marker": "LOOP", "color": C_GREEN, "title": "Loop Done", "pill": True},
    M.LFIX: {"marker": " FIX", "color": C_TEAL, "title": "Fix", "pill": False},
    M.LERR: {"marker": " !! ", "color": C_RED, "title": "Loop Error", "pill": True},
    M.LITR: {"marker": "LOOP", "color": C_DIM_LAVENDER, "title": "Iteration", "pill": False},
    M.LDIG: {"marker": " DX ", "color": C_DIM_LAVENDER, "title": "Diagnostics", "pill": False},
    # Watchdog
    M.WDST: {"marker": " W~ ", "color": C_PURPLE, "title": "Watchdog", "pill": True},
    M.WDOK: {"marker": " W. ", "color": C_DIM_PURPLE, "title": "Watchdog OK", "pill": False},
    M.WDWN: {"marker": " W! ", "color": C_PURPLE, "title": "Watchdog", "pill": True},
    M.WDER: {"marker": " W!!", "color": C_RED, "title": "Watchdog", "pill": True},
    # Blocked
    M.UASK: {"marker": "  ?", "color": C_AMBER, "title": "Question", "pill": True},
    M.UAUT: {"marker": "AUTO", "color": C_AMBER, "title": "Auto-Answer", "pill": True},
}

_DEFAULT_THEME: dict[str, Any] = {"marker": "  . ", "color": C_SECONDARY, "title": "", "pill": False}


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
        M.CTLR,  # Tool internals — result size & LLM routing
    }
)
