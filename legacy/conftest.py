"""Pytest bootstrap for the frozen Python suite (retired orchestrator).

Inserts this directory at the head of `sys.path` so `import orchestrator`,
`import control_api_v2`, `from pipeline import ...`, and `from tests import
...` resolve exactly as they did when these files lived at the repo root.
Run from here: `python -m pytest tests/`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
