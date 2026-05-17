"""Root conftest — ensures scenarios/ and bin/ are importable."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))