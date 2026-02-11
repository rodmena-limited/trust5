import logging
import os
from typing import Any
import yaml
from pydantic import BaseModel, Field

class PlanGateConfig(BaseModel):
    require_baseline: bool = True

class RunGateConfig(BaseModel):
    max_errors: int = 0
    max_type_errors: int = 0
    max_lint_errors: int = 0
    allow_regression: bool = False

class SyncGateConfig(BaseModel):
    max_errors: int = 0
    max_warnings: int = 10
    require_clean_lsp: bool = True
