"""Pytest bootstrap: make the repo-root ``evals`` package importable in tests.

``manta_code`` is installed editable (src layout), but the eval harness lives at
the repo root as ``evals/`` and is not packaged, so add the repo root to
``sys.path`` for the test session.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
