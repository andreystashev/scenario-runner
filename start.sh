#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${SCENARIO_RUNNER_PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    echo 'Нужен Python 3.9+. Установите Python и задайте SCENARIO_RUNNER_PYTHON=/путь/к/python3.' >&2
    exit 1
fi
exec "$PYTHON" "$PROJECT_DIR/launcher.py" start "$@"
