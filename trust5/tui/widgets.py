import os
from typing import Any
from rich.box import ROUNDED
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import RichLog, Static
from ..core.event_bus import (
    K_BLOCK_END,
    K_BLOCK_LINE,
    K_BLOCK_START,
    K_MSG,
    Event,
)
from ..core.message import M
C_BG = "#0c0a08"  # Near-black with warm brown undertone
C_SURFACE = "#151210"  # Dark chocolate surface
C_BORDER = "#2a2420"  # Warm dark border
C_CHROME = "#3a322c"  # Warm separator
C_TEXT = "#e8ddd0"  # Cream white — warm primary text
C_SECONDARY = "#b0a898"  # Warm taupe — normal messages
C_MUTED = "#706860"  # Warm grey — timestamps, noise
C_DIM = "#483f38"  # Dark warm grey — decorative, faint
C_BLUE = "#d4a054"  # Warm gold — primary accent, headers, brand
C_TEAL = "#7ab08a"  # Sage green — tool operations
C_GREEN = "#8cc084"  # Warm green — success
C_AMBER = "#d4943c"  # Copper — thinking, warnings, retries
