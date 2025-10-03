import logging
import os
import random
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from stabilize import StageExecution, Task, TaskResult
from ..core.lang import LanguageProfile
from ..core.message import M, emit
logger = logging.getLogger(__name__)
SUBPROCESS_TIMEOUT = 120
