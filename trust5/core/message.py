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
