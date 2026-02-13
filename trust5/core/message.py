import sys
import threading
from datetime import datetime
from enum import StrEnum

from .event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    K_STREAM_END,
    K_STREAM_START,
    K_STREAM_TOKEN,
    Event,
    get_bus,
)


class M(StrEnum):
    # ── Agent / LLM Communication ──
    ATRN = "ATRN"  # agent turn start (Turn X/Y)
    ATHK = "ATHK"  # agent thinking / reasoning
    ARSP = "ARSP"  # agent text response (assistant content)
    ASUM = "ASUM"  # agent summary / final answer
    AERR = "AERR"  # agent or LLM error
    ARTY = "ARTY"  # LLM retry (with backoff)
    AFBK = "AFBK"  # LLM model fallback

    # ── Tool Calls & Results ──
    TCAL = "TCAL"  # tool call (generic, before execution)
    TRES = "TRES"  # tool result (generic, after execution)
    TBSH = "TBSH"  # tool: Bash
    TRED = "TRED"  # tool: Read file
    TWRT = "TWRT"  # tool: Write file
    TEDT = "TEDT"  # tool: Edit file
    TGLB = "TGLB"  # tool: Glob / list files
    TGRP = "TGRP"  # tool: Grep / search
    TPKG = "TPKG"  # tool: package install
    TINI = "TINI"  # tool: init project
    TGIT = "TGIT"  # tool: git operations

    # ── Context / Prompt Flow (LLM ↔ Agent) ──
    CSYS = "CSYS"  # system prompt sent to LLM
    CUSR = "CUSR"  # user message sent to LLM
    CAST = "CAST"  # assistant message from LLM (raw)
    CTLC = "CTLC"  # tool_call from LLM (name + args)
    CTLR = "CTLR"  # tool result sent back to LLM
    CTRM = "CTRM"  # context trimmed / history pruned
    CTKN = "CTKN"  # token count / context size info
    CMDL = "CMDL"  # model info (name, tier)
    CREQ = "CREQ"  # LLM API request metadata (model, msg count, tool count)
    CRES = "CRES"  # LLM API response metadata (tokens, finish reason)

    # ── Workflow / Pipeline ──
    WSTR = "WSTR"  # workflow started
    WEND = "WEND"  # workflow ended
    WSUC = "WSUC"  # workflow succeeded
    WFAL = "WFAL"  # workflow failed
    WTMO = "WTMO"  # workflow timeout
    WCAN = "WCAN"  # workflow canceled
    WRCV = "WRCV"  # workflow recovered on startup
    WSTG = "WSTG"  # stage started / transition
    WJMP = "WJMP"  # stage jump (jump_to)
    WSKP = "WSKP"  # stage skipped
    WINT = "WINT"  # workflow interrupted (signal)

    # ── Validation / Testing ──
    VRUN = "VRUN"  # validation running
    VPAS = "VPAS"  # validation passed (all tests OK)
    VFAL = "VFAL"  # validation failed (tests failing)
    VSYN = "VSYN"  # syntax check result
    VTST = "VTST"  # raw test output

    # ── Repair ──
    RSTR = "RSTR"  # repair started
    REND = "REND"  # repair completed
    RFAL = "RFAL"  # repair failed
    RSKP = "RSKP"  # repair skipped (no failures)
    RJMP = "RJMP"  # repair jumping back to validate

    # ── Quality Gate ──
    QRUN = "QRUN"  # quality gate running
    QPAS = "QPAS"  # quality gate passed
    QFAL = "QFAL"  # quality gate failed
    QVAL = "QVAL"  # individual validator result
    QRPT = "QRPT"  # quality report summary
    QJMP = "QJMP"  # quality jumping to repair

    # ── Loop (Ralph) ──
    LSTR = "LSTR"  # loop started
    LITR = "LITR"  # loop iteration
    LEND = "LEND"  # loop ended (no issues)
    LFIX = "LFIX"  # loop fixing issue
    LDIG = "LDIG"  # loop diagnostics count
    LERR = "LERR"  # loop error

    # ── User Interaction ──
    UASK = "UASK"  # question to user
    UANS = "UANS"  # answer from user
    UAUT = "UAUT"  # auto-answer (non-interactive)

    # ── System / Infrastructure ──
    SINF = "SINF"  # system info
    SWRN = "SWRN"  # system warning
    SERR = "SERR"  # system error
    SDBG = "SDBG"  # system debug
    SCFG = "SCFG"  # config message
    SLSP = "SLSP"  # LSP diagnostic
    SRCV = "SRCV"  # recovery message
    SDB = "SDB_"  # database operation

    # ── Code Artifacts ──
    KDIF = "KDIF"  # diff / patch
    KCOD = "KCOD"  # code block / snippet
    KFMT = "KFMT"  # format / lint output
    KBLD = "KBLD"  # build output

    # ── Planning / Progress ──
    PTDO = "PTDO"  # todo / task list
    PPLN = "PPLN"  # plan step
    PPRG = "PPRG"  # progress update
    PDNE = "PDNE"  # done / complete marker

    # ── Model / Token Tracking (TUI status bar) ──
    MTKN = "MTKN"  # token usage (in/out/total per call)
    MCTX = "MCTX"  # context window remaining
    MBDG = "MBDG"  # budget / rate limit info
    MMDL = "MMDL"  # model metadata (name, provider, tier)
    MPRF = "MPRF"  # provider info (active provider, auth status)

    # ── TUI Layout Events ──
    FCHG = "FCHG"  # file changed (path + action)
    SELP = "SELP"  # stage elapsed time
    SPRG = "SPRG"  # stage progress (current/total)
    GSTS = "GSTS"  # git status (branch, dirty, commits)


TOOL_CODE_MAP = {
    "Bash": M.TBSH,
    "Read": M.TRED,
    "Write": M.TWRT,
    "Edit": M.TEDT,
    "Glob": M.TGLB,
    "Grep": M.TGRP,
    "InstallPackage": M.TPKG,
    "InitProject": M.TINI,
    "AskUserQuestion": M.UASK,
}


_enabled = True
_print_fallback = True


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def set_print_fallback(value: bool) -> None:
    """Disable print fallback when TUI is active.

    When Textual owns the terminal, any print() to stdout/stderr corrupts
    the layout. Call set_print_fallback(False) before starting the TUI so
    emit functions silently drop events when the bus isn't ready yet.
    """
    global _print_fallback
    _print_fallback = value


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def emit(code: M, message: str, *, truncate: int = 0, label: str = "") -> None:
    if not _enabled:
        return
    if truncate > 0 and len(message) > truncate:
        message = message[:truncate] + f"... [{len(message) - truncate} chars]"
    bus = get_bus()
    if bus is not None:
        bus.publish(Event(kind=K_MSG, code=code.value, ts=_ts(), msg=message, label=label))
        # Also print if fallback is on and no one is listening on the bus
        # (pre-TUI state: bus exists but TUI hasn't subscribed yet)
        if _print_fallback and not bus._listeners:
            print(f"{{{code.value}}}{_ts()} {message}", flush=True)
    elif _print_fallback:
        print(f"{{{code.value}}}{_ts()} {message}", flush=True)


def emit_block(code: M, label: str, content: str, *, max_lines: int = 0) -> None:
    if not _enabled:
        return
    lines = content.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... [{len(lines) - max_lines} more lines]"]
    bus = get_bus()
    if bus is not None:
        ts = _ts()
        cv = code.value
        bus.publish(Event(kind=K_BLOCK_START, code=cv, ts=ts, label=label))
        for line in lines:
            bus.publish(Event(kind=K_BLOCK_LINE, code=cv, ts=ts, msg=line))
        bus.publish(Event(kind=K_BLOCK_END, code=cv, ts=ts))
        if _print_fallback and not bus._listeners:
            tag = f"{{{cv}}}"
            print(f"{tag}{_ts()} \u250c\u2500\u2500 {label}", flush=True)
            for line in lines:
                print(f"{tag}{_ts()}  \u2502 {line}", flush=True)
            print(f"{tag}{_ts()} \u2514\u2500\u2500", flush=True)
    elif _print_fallback:
        tag = f"{{{code.value}}}"
        print(f"{tag}{_ts()} \u250c\u2500\u2500 {label}", flush=True)
        for line in lines:
            print(f"{tag}{_ts()}  \u2502 {line}", flush=True)
        print(f"{tag}{_ts()} \u2514\u2500\u2500", flush=True)


_stream_local = threading.local()


def emit_stream_start(code: M, label: str) -> None:
    if not _enabled:
        return
    _stream_local.code = code.value
    bus = get_bus()
    if bus is not None:
        bus.publish(Event(kind=K_STREAM_START, code=code.value, ts=_ts(), label=label))
    elif _print_fallback:
        print(f"{{{code.value}}}{_ts()} {label}", end="", flush=True)


def emit_stream_token(token: str) -> None:
    if not _enabled:
        return
    code = getattr(_stream_local, "code", "")
    bus = get_bus()
    if bus is not None:
        bus.publish(Event(kind=K_STREAM_TOKEN, code=code, ts=_ts(), msg=token))
    elif _print_fallback:
        sys.stdout.write(token)
        sys.stdout.flush()


def emit_stream_end() -> None:
    if not _enabled:
        return
    code = getattr(_stream_local, "code", "")
    bus = get_bus()
    if bus is not None:
        bus.publish(Event(kind=K_STREAM_END, code=code, ts=_ts()))
    elif _print_fallback:
        print("", flush=True)


def tool_code(name: str) -> M:
    return TOOL_CODE_MAP.get(name, M.TCAL)
