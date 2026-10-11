#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "${SCENARIO_RUNNER_PYTHON:-python3}" "$PROJECT_DIR/launcher.py" stop "$@"
