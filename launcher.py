#!/usr/bin/env python3
"""Dependency-free, serialized macOS/Linux server lifecycle."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("SCENARIO_RUNNER_CACHE_DIR", "~/.cache/scenario-runner")).expanduser().resolve()
CONFIG_DIR = Path(os.getenv("SCENARIO_RUNNER_CONFIG_DIR", "~/.config/scenario-runner")).expanduser().resolve()
RECORD = CACHE_DIR / "server.json"
LOG_FILE = CACHE_DIR / "server.log"


def read_record():
    try:
        value = json.loads(RECORD.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def owns_process(record):
    pid, token = record.get("pid"), record.get("instance_id")
    if not isinstance(pid, int) or pid <= 1 or not isinstance(token, str) or not token:
        return False
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=,command="],
                            capture_output=True, text=True)
    fields = result.stdout.strip().split(None, 1)
    return (len(fields) == 2 and not fields[0].startswith("Z")
            and str(APP_DIR / "server.py") in fields[1]
            and "--instance-id " + token in fields[1])


def health(port):
    try:
        # Ignore HTTP_PROXY: this request must stay on the local machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=.5) as response:
            value = json.loads(response.read(8192))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def matches(value, record):
    return (value.get("app") == "scenario-runner"
            and value.get("instance_id") == record.get("instance_id")
            and value.get("config") == str(CONFIG_DIR))


def show_url(port):
    url = f"http://127.0.0.1:{port}"
    print(f"Scenario Runner: {url}\nЛог: {LOG_FILE}")
    if os.getenv("SCENARIO_RUNNER_NO_OPEN") != "1":
        webbrowser.open(url)


def start(port):
    record = read_record()
    if owns_process(record):
        old_port = record.get("port")
        if old_port != port:
            raise RuntimeError(f"Сервер уже запущен на порту {old_port}. Выполните ./stop.sh перед сменой порта.")
        if not matches(health(port), record):
            raise RuntimeError("Запущенный сервер не отвечает. Проверьте журнал или выполните ./stop.sh.")
        show_url(port)
        return
    if health(port):
        raise RuntimeError(f"Порт {port} уже занят другим сервером. Выберите SCENARIO_RUNNER_PORT.")
    token = uuid.uuid4().hex
    with LOG_FILE.open("ab") as log:
        process = subprocess.Popen([sys.executable, str(APP_DIR / "server.py"), "--host", "127.0.0.1",
                                    "--port", str(port), "--instance-id", token], cwd=APP_DIR,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    record = {"pid": process.pid, "instance_id": token, "port": port}
    try:
        temporary = RECORD.with_suffix(".tmp")
        temporary.write_text(json.dumps(record) + "\n")
        temporary.replace(RECORD)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Сервер завершился при запуске (код {process.returncode}). Лог: {LOG_FILE}")
            if matches(health(port), record):
                show_url(port)
                return
            time.sleep(.1)
        raise RuntimeError(f"Сервер не ответил за 10 секунд. Лог: {LOG_FILE}")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        RECORD.unlink(missing_ok=True)
        raise


def stop():
    record = read_record()
    if owns_process(record):
        os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while owns_process(record) and time.monotonic() < deadline:
            time.sleep(.1)
        if owns_process(record):
            raise RuntimeError("Сервер не завершился. PID-файл сохранён; проверьте журнал.")
    RECORD.unlink(missing_ok=True)
    print("Scenario Runner остановлен. Запущенные команды сценариев не прерывались.")


def main():
    parser = argparse.ArgumentParser(description="Запуск и остановка Scenario Runner")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    try:
        port = int(os.getenv("SCENARIO_RUNNER_PORT", "8010"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        parser.error("SCENARIO_RUNNER_PORT должен быть числом от 1 до 65535")
    if args.foreground:
        os.execv(sys.executable, [sys.executable, str(APP_DIR / "server.py"), "--host", "127.0.0.1", "--port", str(port)])
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (CACHE_DIR / "launcher.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "start":
                start(port)
            else:
                stop()
    except (OSError, RuntimeError) as exc:
        print(f"Scenario Runner: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
