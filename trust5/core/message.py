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
_stream_local = threading.local()

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

class M(StrEnum):
    ATRN = 'ATRN'
    ATHK = 'ATHK'
    ARSP = 'ARSP'
    ASUM = 'ASUM'
    AERR = 'AERR'
    ARTY = 'ARTY'
    AFBK = 'AFBK'
    TCAL = 'TCAL'
    TRES = 'TRES'
    TBSH = 'TBSH'
    TRED = 'TRED'
    TWRT = 'TWRT'
    TEDT = 'TEDT'
    TGLB = 'TGLB'
    TGRP = 'TGRP'
    TPKG = 'TPKG'
    TINI = 'TINI'
    TGIT = 'TGIT'
    CSYS = 'CSYS'
    CUSR = 'CUSR'
    CAST = 'CAST'
    CTLC = 'CTLC'
    CTLR = 'CTLR'
    CTRM = 'CTRM'
    CTKN = 'CTKN'
    CMDL = 'CMDL'
    CREQ = 'CREQ'
    CRES = 'CRES'
    WSTR = 'WSTR'
    WEND = 'WEND'
    WSUC = 'WSUC'
    WFAL = 'WFAL'
    WTMO = 'WTMO'
    WCAN = 'WCAN'
    WRCV = 'WRCV'
    WSTG = 'WSTG'
    WJMP = 'WJMP'
    WSKP = 'WSKP'
    WINT = 'WINT'
    VRUN = 'VRUN'
    VPAS = 'VPAS'
    VFAL = 'VFAL'
    VSYN = 'VSYN'
    VTST = 'VTST'
    RSTR = 'RSTR'
    REND = 'REND'
    RFAL = 'RFAL'
    RSKP = 'RSKP'
    RJMP = 'RJMP'
    QRUN = 'QRUN'
    QPAS = 'QPAS'
    QFAL = 'QFAL'
    QVAL = 'QVAL'
    QRPT = 'QRPT'
    QJMP = 'QJMP'
    LSTR = 'LSTR'
    LITR = 'LITR'
    LEND = 'LEND'
    LFIX = 'LFIX'
    LDIG = 'LDIG'
    LERR = 'LERR'
    UASK = 'UASK'
    UANS = 'UANS'
    UAUT = 'UAUT'
    SINF = 'SINF'
    SWRN = 'SWRN'
    SERR = 'SERR'
    SDBG = 'SDBG'
    SCFG = 'SCFG'
    SLSP = 'SLSP'
    SRCV = 'SRCV'
    SDB = 'SDB_'
    KDIF = 'KDIF'
    KCOD = 'KCOD'
    KFMT = 'KFMT'
    KBLD = 'KBLD'
    PTDO = 'PTDO'
    PPLN = 'PPLN'
    PPRG = 'PPRG'
    PDNE = 'PDNE'
    MTKN = 'MTKN'
    MCTX = 'MCTX'
    MBDG = 'MBDG'
    MMDL = 'MMDL'
    MPRF = 'MPRF'
    FCHG = 'FCHG'
    SELP = 'SELP'
    SPRG = 'SPRG'
    GSTS = 'GSTS'
