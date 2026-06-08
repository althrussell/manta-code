#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
manta init
manta route "add a settings page and tests"
manta run "add a settings page and tests" --dry-run --max-usd 1
