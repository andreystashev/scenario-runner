#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/scenario-runner"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/scenario-runner.service"
PORT="${SCENARIO_RUNNER_PORT:-8010}"
URL="http://127.0.0.1:$PORT"
mkdir -p "$RUNTIME_DIR"

if [ "${1:-}" = "--foreground" ]; then
    exec python3 "$PROJECT_DIR/server.py" --port "$PORT"
fi

SERVER_PID=""
SERVICE_MODE="false"
mkdir -p "$(dirname "$SERVICE_FILE")"
SERVICE_TEMPLATE="$(mktemp)"
trap 'rm -f "$SERVICE_TEMPLATE"' EXIT
printf '%s\n' "[Unit]
Description=Scenario Runner
After=graphical-session.target docker.service

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 $PROJECT_DIR/server.py --port $PORT
Restart=on-failure
RestartSec=1

[Install]
WantedBy=default.target" > "$SERVICE_TEMPLATE"

SERVICE_CHANGED="false"
if ! cmp -s "$SERVICE_TEMPLATE" "$SERVICE_FILE"; then
    install -m 0644 "$SERVICE_TEMPLATE" "$SERVICE_FILE"
    SERVICE_CHANGED="true"
fi

if [ "$SERVICE_CHANGED" = "false" ] && systemctl --user is-active scenario-runner.service >/dev/null 2>&1; then
    SERVICE_MODE="true"
elif systemctl --user daemon-reload >/dev/null 2>&1 && systemctl --user restart scenario-runner.service >/dev/null 2>&1; then
    SERVICE_MODE="true"
fi
if [ "$SERVICE_MODE" = "true" ]; then
    rm -f "$PID_FILE"
else
    if [ -f "$PID_FILE" ]; then
        OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        [ -z "$OLD_PID" ] || ! kill -0 "$OLD_PID" 2>/dev/null || kill "$OLD_PID"
    fi
    nohup python3 "$PROJECT_DIR/server.py" --port "$PORT" >"$LOG_FILE" 2>&1 &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"
fi

for _ in $(seq 1 50); do
    if curl -fsS "$URL/api/state" >/dev/null 2>&1; then
        echo "Scenario Runner: $URL"
        if [ "$SERVICE_MODE" = "true" ]; then
            echo "Лог: journalctl --user -u scenario-runner.service -f"
        else
            echo "Лог: $LOG_FILE"
        fi
        xdg-open "$URL" >/dev/null 2>&1 || true
        exit 0
    fi
    if [ -n "$SERVER_PID" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Не удалось запустить Scenario Runner:" >&2
        tail -n 40 "$LOG_FILE" >&2 || true
        exit 1
    fi
    sleep 0.1
done

echo "Scenario Runner не ответил за 5 секунд. Лог: $LOG_FILE" >&2
systemctl --user status scenario-runner.service --no-pager >&2 || true
exit 1
