import glob
import os
from typing import Any
MAX_FILE_CONTENT = 6000
MAX_TOTAL_CONTEXT = 30000
_FALLBACK_EXTENSIONS = (".py", ".go", ".ts", ".js", ".rs", ".java", ".rb")
