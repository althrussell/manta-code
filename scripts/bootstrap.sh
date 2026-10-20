#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
manta_python="${MANTA_PYTHON:-python3}"
"$manta_python" -c 'import sys; sys.exit("Manta requires Python 3.12 or newer") if sys.version_info < (3, 12) else None'
"$manta_python" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.lock
pip install --no-deps -e '.[dev,agent]'
pip check
manta --version
pytest
ruff check .
