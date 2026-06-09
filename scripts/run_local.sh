#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
manta init
manta doctor
# Launch the interactive TUI (optionally pass a profile): manta -p <profile>
manta
