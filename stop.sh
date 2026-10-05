#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/scenario-runner"
PID_FILE="$RUNTIME_DIR/server.pid"
if systemctl --user is-active scenario-runner.service >/dev/null 2>&1; then
    systemctl --user stop scenario-runner.service
fi
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
    fi
    rm -f "$PID_FILE"
fi
echo "Scenario Runner остановлен. Запущенные команды сценариев не прерывались."
