from __future__ import annotations
import logging
import re
from dataclasses import dataclass
import yaml
logger = logging.getLogger(__name__)
_DEFAULT = PlanConfig()
_EARS_TAG_RE = re.compile(
    r"^\s*-\s*\[(UBIQ|EVENT|STATE|UNWNT|OPTNL|COMPLX)\]\s*(.+)",
    re.IGNORECASE,
)
