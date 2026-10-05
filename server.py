#!/usr/bin/env python3
"""Scenario Runner: native Ubuntu scenario launcher with a local web UI."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_VERSION = "0.4.0"
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_DIR = Path(os.getenv("SCENARIO_RUNNER_CONFIG_DIR", "~/.config/scenario-runner")).expanduser()
CACHE_DIR = Path(os.getenv("SCENARIO_RUNNER_CACHE_DIR", "~/.cache/scenario-runner")).expanduser()
PRESETS_DIR = CONFIG_DIR / "presets"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
WORKSPACES_FILE = CONFIG_DIR / "workspaces.json"
RUNTIME_DIR = CACHE_DIR / "runtime"
LOCK = threading.RLock()
ACTION_LOCK = threading.RLock()
LOG = logging.getLogger("scenario-runner")
STATUS_LOCK = threading.Lock()
STATUS_CACHE: dict[str, Any] = {"workspaces": [], "targets": [], "revisions": {}, "updated_at": None}
WORKSPACE_REVISIONS: dict[str, int] = {}
STATE_REVISION = 0

DEFAULT_SETTINGS = {
    "targets": [
        {
            "id": "local", "name": "Этот компьютер", "type": "local",
            "workdir": "~", "source_command": "",
        },
    ],
}

CONTAINER_STOP_SCRIPT = r'''import os,signal,sys,time
prefix=sys.argv[1]
markers=(f"echo $$ > {prefix}.shell.pid",f"echo $$ > {prefix}.pid")
def processes():
 result={}
 for entry in os.listdir("/proc"):
  if not entry.isdigit(): continue
  try:
   with open(f"/proc/{entry}/cmdline","rb") as stream:
    command=stream.read().replace(b"\0",b" ").decode("utf-8","replace").strip()
   with open(f"/proc/{entry}/stat") as stream: stat=stream.read()
   fields=stat[stat.rfind(")")+2:].split()
   if fields[0]!="Z": result[int(entry)]=(int(fields[1]),command)
  except (OSError,ValueError,IndexError): pass
 return result
current=processes()
roots=[pid for pid,(_,cmd) in current.items() if cmd.startswith("bash -lc ") and any(marker in cmd for marker in markers)]
def descendants(root,table):
 found=[]
 for child,(parent,_) in table.items():
  if parent==root:
   found.extend(descendants(child,table)); found.append(child)
 return found
targets=[]
for root in roots: targets.extend(descendants(root,current)); targets.append(root)
targets=list(dict.fromkeys(targets))
for sig in (signal.SIGTERM,signal.SIGKILL):
 for pid in targets:
  if pid in processes():
   try: os.kill(pid,sig)
   except ProcessLookupError: pass
 time.sleep(.35)
alive=[pid for pid in targets if pid in processes()]
if alive:
 print("Processes remain: "+", ".join(map(str,alive)),file=sys.stderr); sys.exit(1)
for suffix in (".pid",".shell.pid"):
 try: os.unlink(prefix+suffix)
 except FileNotFoundError: pass
'''
settings: dict[str, Any] = {}
presets: dict[str, dict] = {}
workspaces: list[dict] = []


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def safe_id(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^A-Za-z0-9А-Яа-яЁё_.-]+", "-", value.strip()).strip("-.")
    return value[:80] or fallback


def unique_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000):x}"


def normalize_step(raw: dict, index: int) -> dict:
    legacy_execution = str(raw.get("execution") or "")
    target_id = str(raw.get("target_id") or ("docker" if legacy_execution.startswith("container_") else "local"))
    terminal = bool(raw.get("terminal", legacy_execution.endswith("_terminal") if legacy_execution else True))
    return {
        "id": safe_id(str(raw.get("id") or f"command-{index + 1}"), f"command-{index + 1}"),
        "title": str(raw.get("title") or f"Команда {index + 1}").strip()[:160],
        "command": str(raw.get("command") or "").strip(),
        "target_id": safe_id(target_id, "local"),
        "terminal": terminal,
        "selected": bool(raw.get("selected", True)),
        "process_match": str(raw.get("process_match") or "").strip()[:1000],
    }


def normalize_preset(raw: dict, preset_id: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "Пресет должен быть JSON-объектом")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Введите название пресета")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ApiError(400, "Добавьте хотя бы одну команду")
    result_steps = []
    used = set()
    for index, item in enumerate(raw_steps):
        step = normalize_step(item if isinstance(item, dict) else {}, index)
        base, candidate, number = step["id"], step["id"], 2
        while candidate in used:
            candidate = f"{base}-{number}"
            number += 1
        step["id"] = candidate
        used.add(candidate)
        result_steps.append(step)
    result_id = safe_id(preset_id or str(raw.get("id") or name), "preset")
    return {
        "id": result_id,
        "name": name[:160],
        "description": str(raw.get("description") or "").strip()[:500],
        "builtin": False,
        "version": 1,
        "workdir": str(raw.get("workdir") or raw.get("project_path") or "").strip(),
        "steps": result_steps,
        "updated_at": now_iso(),
    }


def normalize_target(raw: dict, index: int) -> dict:
    target_type = "docker" if str(raw.get("type")) == "docker" else "local"
    target_id = safe_id(str(raw.get("id") or raw.get("name") or f"target-{index + 1}"), f"target-{index + 1}")
    result = {
        "id": target_id,
        "name": str(raw.get("name") or target_id).strip()[:120],
        "type": target_type,
        "workdir": str(raw.get("workdir") or ("~" if target_type == "local" else "/")).strip(),
        "source_command": str(raw.get("source_command") or "").strip(),
    }
    if target_type == "docker":
        result.update({
            "container_name": str(raw.get("container_name") or target_id).strip(),
            "host_workdir": str(raw.get("host_workdir") or "~").strip(),
            "start_command": str(raw.get("start_command") or "").strip(),
        })
    return result


def normalize_settings(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    incoming_targets = raw.get("targets")
    if not isinstance(incoming_targets, list) or not incoming_targets:
        # Migration from Scenario Runner 0.1 settings.
        incoming_targets = copy.deepcopy(DEFAULT_SETTINGS["targets"])
        if any(raw.get(key) for key in ("project_path", "container_name", "container_workdir")):
            incoming_targets.append({
                "id": "docker", "name": "Docker", "type": "docker",
                "host_workdir": str(raw.get("project_path") or "~"),
                "container_name": str(raw.get("container_name") or "container"),
                "workdir": str(raw.get("container_workdir") or "/"),
            })
    targets = []
    used = set()
    for index, item in enumerate(incoming_targets):
        target = normalize_target(item if isinstance(item, dict) else {}, index)
        if target["id"] in used:
            target["id"] = unique_id(target["id"])
        used.add(target["id"])
        targets.append(target)
    if not any(item["type"] == "local" for item in targets):
        targets.insert(0, copy.deepcopy(DEFAULT_SETTINGS["targets"][0]))
    return {"targets": targets}


def load_all() -> None:
    global settings, presets, workspaces
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        incoming = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings = normalize_settings(incoming)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        settings = normalize_settings(DEFAULT_SETTINGS)
    loaded = {}
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            item = normalize_preset(json.loads(path.read_text(encoding="utf-8")), path.stem)
            loaded[item["id"]] = item
        except Exception as exc:
            LOG.warning("Пресет %s пропущен: %s", path, exc)
    presets = loaded
    try:
        incoming = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
        values = incoming.get("workspaces", []) if isinstance(incoming, dict) else []
        workspaces = [item for item in values if isinstance(item, dict) and item.get("id")]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        workspaces = []
    for workspace in workspaces:
        sync_workspace(workspace)


def save_workspaces(changed: dict | None = None) -> None:
    global STATE_REVISION
    if changed is not None:
        STATE_REVISION += 1
        WORKSPACE_REVISIONS[str(changed["id"])] = STATE_REVISION
    atomic_json(WORKSPACES_FILE, {"version": 1, "workspaces": workspaces})


def preset_for(workspace: dict) -> dict:
    preset = presets.get(str(workspace.get("preset_id")))
    if not preset:
        raise ApiError(409, "Сценарий рабочей панели не найден. Выберите или создайте сценарий")
    return preset


def target_for(spec: dict) -> dict:
    target_id = str(spec.get("target_id") or "local")
    target = next((item for item in settings["targets"] if item["id"] == target_id), None)
    if not target:
        raise ApiError(409, f"Цель выполнения не найдена: {target_id}. Выберите другую цель в редакторе сценария")
    return target


def target_is_docker(spec_or_target: dict) -> bool:
    target = spec_or_target if spec_or_target.get("type") else target_for(spec_or_target)
    return target.get("type") == "docker"


def sync_workspace(workspace: dict) -> None:
    preset = preset_for(workspace)
    old = workspace.get("steps") if isinstance(workspace.get("steps"), dict) else {}
    workspace["preset_id"] = preset["id"]
    workspace.setdefault("variables", {})
    workspace["steps"] = {
        spec["id"]: {
            "selected": bool(old.get(spec["id"], {}).get("selected", spec.get("selected", True))),
            "status": str(old.get(spec["id"], {}).get("status") or "idle"),
            "message": str(old.get(spec["id"], {}).get("message") or ""),
        }
        for spec in preset["steps"]
    }


def host_workdir(workspace: dict, spec: dict) -> Path:
    preset_value = str(preset_for(workspace).get("workdir") or "").strip()
    target = target_for(spec)
    value = preset_value or str(target.get("host_workdir") or target.get("workdir") or "~")
    return Path(os.path.expandvars(value)).expanduser().resolve()


def runtime_base(workspace_id: str, step_id: str) -> Path:
    return RUNTIME_DIR / f"{safe_id(workspace_id)}-{safe_id(step_id)}"


def runtime_file(base: Path, suffix: str) -> Path:
    return Path(str(base) + suffix)


def process_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def file_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
        return value if process_alive(value) else None
    except (OSError, ValueError):
        return None


def tail(path: Path, amount: int = 3000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - amount))
            return stream.read(amount).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def run(args: list[str], timeout: float = 4) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def container_running(target: dict) -> bool:
    result = run(["docker", "inspect", "-f", "{{.State.Running}}", target["container_name"]])
    return result.returncode == 0 and result.stdout.strip() == "true"


def container_file_pid(target: dict, path: str) -> int | None:
    if not container_running(target):
        return None
    result = run(["docker", "exec", target["container_name"], "bash", "-lc", f"test -s {shlex.quote(path)} && cat {shlex.quote(path)}"])
    if result.returncode or not result.stdout.strip().isdigit():
        return None
    pid = int(result.stdout.strip())
    probe = run(["docker", "exec", target["container_name"], "kill", "-0", str(pid)])
    return pid if probe.returncode == 0 else None


def container_snapshot(target: dict) -> dict:
    """Collect all container runtime files and process commands in one exec."""
    if not container_running(target):
        return {"running": False, "pids": {}, "codes": {}, "logs": {}, "commands": []}
    script = r'''import glob,json,os
result={"running":True,"pids":{},"codes":{},"logs":{},"commands":[]}
for path in glob.glob("/tmp/scenario-runner-*"):
 try:
  if path.endswith((".pid",".shell.pid")):
   with open(path) as stream: pid=int(stream.read(64).strip())
   try: os.kill(pid,0); result["pids"][path]=pid
   except OSError: pass
  elif path.endswith(".rc"):
   with open(path) as stream: result["codes"][path]=int(stream.read(64).strip())
  elif path.endswith(".log"):
   with open(path,"rb") as stream:
    stream.seek(0,2); stream.seek(max(0,stream.tell()-3000)); result["logs"][path]=stream.read(3000).decode("utf-8","replace")
 except (OSError,ValueError): pass
try:
 for entry in os.listdir("/proc"):
  if not entry.isdigit(): continue
  try:
   with open(f"/proc/{entry}/cmdline","rb") as stream: command=stream.read().replace(b"\0",b" ").decode("utf-8","replace").strip()
   if command: result["commands"].append([int(entry),command])
  except OSError: pass
except OSError: pass
print(json.dumps(result))'''
    result = run(["docker", "exec", target["container_name"], "python3", "-c", script], timeout=6)
    try:
        return json.loads(result.stdout) if result.returncode == 0 else {"running": True, "pids": {}, "codes": {}, "logs": {}, "commands": []}
    except json.JSONDecodeError:
        return {"running": True, "pids": {}, "codes": {}, "logs": {}, "commands": []}


def read_return_code(path: str, target: dict | None = None) -> int | None:
    try:
        if target:
            result = run(["docker", "exec", target["container_name"], "cat", path])
            value = result.stdout.strip() if result.returncode == 0 else ""
        else:
            value = Path(path).read_text(encoding="utf-8").strip()
        return int(value) if re.fullmatch(r"-?\d+", value) else None
    except (OSError, ValueError):
        return None


def command_for(workspace: dict, spec: dict) -> str:
    command = str(spec.get("command") or "").strip()
    if not command:
        raise ApiError(409, "Команда не заполнена")
    target = target_for(spec)
    docker = target_is_docker(target)
    local_workdir = host_workdir(workspace, spec)
    target_workdir = str(target.get("workdir") or "/") if docker else str(local_workdir)
    command = command.replace("{{project_path}}", shlex.quote(target_workdir))
    variables = workspace.get("variables") if isinstance(workspace.get("variables"), dict) else {}
    for key, value in variables.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", str(key)):
            command = command.replace("{{" + str(key) + "}}", shlex.quote(str(value)))
    bag_value = str(workspace.get("bag_path") or "").strip()
    if "{{bag_path}}" in command or "{{bag_folder}}" in command:
        if not bag_value:
            raise ApiError(409, "Для этой команды укажите папку бэга в панели")
        host_bag = Path(os.path.expandvars(bag_value)).expanduser().resolve()
        if not host_bag.exists():
            raise ApiError(409, f"Путь к бэгу не найден: {host_bag}")
        if not docker:
            bag_path = str(host_bag)
        else:
            try:
                relative = host_bag.relative_to(local_workdir)
            except ValueError as exc:
                raise ApiError(409, "Для Docker-цели путь должен находиться внутри её каталога на хосте") from exc
            bag_path = str(Path(target_workdir) / relative)
        command = command.replace("{{bag_path}}", shlex.quote(bag_path))
        command = command.replace("{{bag_folder}}", shlex.quote(bag_path))
    return command


def external_pids(workspace: dict, spec: dict, snapshot: dict | None = None) -> list[int]:
    pattern = str(spec.get("process_match") or "").strip()
    target = target_for(spec)
    if not pattern or not target_is_docker(target):
        return []
    folder = Path(str(workspace.get("bag_path") or "")).name
    pattern = pattern.replace("{{bag_folder_regex}}", re.escape(folder))
    if snapshot is not None:
        try:
            regex = re.compile(pattern)
            return [int(pid) for pid, command in snapshot.get("commands", []) if regex.search(command)]
        except re.error:
            return []
    if not container_running(target):
        return []
    result = run(["docker", "exec", target["container_name"], "pgrep", "-f", "--", pattern])
    return [int(value) for value in result.stdout.split() if value.isdigit() and int(value) > 1]


def container_terminal_pids(target: dict, prefix: str, snapshot: dict | None = None) -> list[int]:
    """Recover Scenario Runner shells if an old stop attempt removed their PID files."""
    if snapshot is None:
        if not container_running(target):
            return []
        result = run(["docker", "exec", target["container_name"], "ps", "-eo", "pid=,args="])
        if result.returncode:
            return []
        commands = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                commands.append((int(parts[0]), parts[1]))
    else:
        commands = snapshot.get("commands", [])
    marker = f"echo $$ > {prefix}.shell.pid"
    return [int(pid) for pid, command in commands if command.startswith("bash -lc ") and marker in command]


def step_status(workspace: dict, spec: dict, snapshot: dict | None = None) -> dict:
    base = runtime_base(str(workspace["id"]), spec["id"])
    target = target_for(spec)
    is_container = target_is_docker(target)
    if is_container:
        prefix = f"/tmp/scenario-runner-{safe_id(str(workspace['id']))}-{safe_id(spec['id'])}"
        if snapshot is not None:
            owned_pid = snapshot.get("pids", {}).get(prefix + ".pid")
            shell_pid = snapshot.get("pids", {}).get(prefix + ".shell.pid")
            rc = snapshot.get("codes", {}).get(prefix + ".rc")
            log = str(snapshot.get("logs", {}).get(prefix + ".log") or "").strip()
        else:
            owned_pid = container_file_pid(target, prefix + ".pid")
            shell_pid = container_file_pid(target, prefix + ".shell.pid")
            rc = read_return_code(prefix + ".rc", target)
            log = ""
        if snapshot is None and container_running(target):
            result = run(["docker", "exec", target["container_name"], "tail", "-c", "3000", prefix + ".log"])
            log = result.stdout.strip() if result.returncode == 0 else ""
        if spec.get("terminal") and not shell_pid:
            recovered = container_terminal_pids(target, prefix, snapshot)
            shell_pid = recovered[0] if recovered else None
    else:
        owned_pid = file_pid(runtime_file(base, ".pid"))
        shell_pid = file_pid(runtime_file(base, ".shell.pid"))
        rc = read_return_code(str(runtime_file(base, ".rc")))
        log = tail(runtime_file(base, ".log"))
    found_external = bool(external_pids(workspace, spec, snapshot)) if not owned_pid and not shell_pid else False
    if owned_pid:
        return {"status": "running", "message": "Процесс сценария работает", "owned": True, "terminal_open": bool(shell_pid), "external": False, "log_tail": log}
    if shell_pid:
        message = "Команда завершена; терминал открыт для ручной работы" if rc is not None else "Терминал сценария открыт"
        return {"status": "terminal", "message": message, "owned": False, "terminal_open": True, "external": False, "return_code": rc, "log_tail": log}
    if found_external:
        return {"status": "external", "message": "Найден процесс, запущенный вне Scenario Runner. Он не будет перезапущен или остановлен", "owned": False, "terminal_open": False, "external": True, "log_tail": ""}
    if workspace.get("steps", {}).get(spec["id"], {}).get("status") == "stopped":
        return {"status": "stopped", "message": "Остановлено пользователем", "owned": False, "terminal_open": False, "external": False, "log_tail": log}
    if rc is not None:
        return {"status": "stopped" if rc == 0 else "failed", "message": f"Команда завершена, код {rc}", "owned": False, "terminal_open": False, "external": False, "return_code": rc, "log_tail": log}
    return {"status": "idle", "message": "Готово к запуску", "owned": False, "terminal_open": False, "external": False, "log_tail": log}


def public_workspace(workspace: dict, snapshots: dict[str, dict] | None = None) -> dict:
    sync_workspace(workspace)
    result = copy.deepcopy(workspace)
    preset = preset_for(workspace)
    result["preset"] = copy.deepcopy(preset)
    result["steps"] = {
        spec["id"]: {
            **workspace["steps"][spec["id"]],
            **step_status(workspace, spec, (snapshots or {}).get(target_for(spec)["id"]) if target_is_docker(spec) else None),
        }
        for spec in preset["steps"]
    }
    return result


def terminal_executable() -> str:
    value = shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator")
    if not value:
        raise ApiError(503, "Не найден системный терминал gnome-terminal")
    return value


def open_terminal(title: str, command: str, hold_on_error: bool = False) -> None:
    terminal = terminal_executable()
    if hold_on_error:
        command = (
            f"{command}; runner_outer_rc=$?; "
            "if [ $runner_outer_rc -ne 0 ]; then "
            "printf '\n[Scenario Runner] Не удалось начать выполнение, код %s. "
            "Терминал оставлен открытым для диагностики.\n' \"$runner_outer_rc\"; exec bash -i; fi"
        )
    if Path(terminal).name == "gnome-terminal":
        argv = [terminal, "--wait", f"--title={title[:140]}", "--", "bash", "-lc", command]
    else:
        argv = [terminal, "-T", title[:140], "-e", "bash", "-lc", command]
    subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_target(workspace: dict, spec: dict) -> None:
    target = target_for(spec)
    if not target_is_docker(target) or container_running(target):
        return
    workdir = host_workdir(workspace, spec)
    start_command = str(target.get("start_command") or "").strip()
    if not start_command:
        raise ApiError(409, f"Контейнер {target['container_name']} остановлен, а команда запуска цели не настроена")
    if not workdir.is_dir():
        raise ApiError(409, f"Рабочая папка цели не найдена: {workdir}")
    command = f"cd {shlex.quote(str(workdir))}; {start_command}"
    open_terminal(f"Scenario Runner · {target['name']}", command)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if container_running(target):
            return
        time.sleep(0.5)
    raise ApiError(503, f"Контейнер {target['container_name']} не появился за 35 секунд. Проверьте терминал запуска цели")


def validate_container_workdir(target: dict) -> None:
    workdir = str(target.get("workdir") or "/")
    result = run(["docker", "exec", target["container_name"], "test", "-d", workdir])
    if result.returncode == 0:
        return
    detected = run(["docker", "exec", target["container_name"], "pwd"])
    suggestion = detected.stdout.strip()
    suffix = f" Вероятный путь: {suggestion}" if suggestion else ""
    raise ApiError(409, f"В контейнере {target['container_name']} нет рабочей папки {workdir}.{suffix}")


def launch_step(workspace: dict, step_id: str) -> None:
    preset = preset_for(workspace)
    spec = next((item for item in preset["steps"] if item["id"] == step_id), None)
    if not spec:
        raise ApiError(404, "Команда не найдена")
    current = step_status(workspace, spec)
    if current["external"]:
        raise ApiError(409, "Команда уже запущена вне Scenario Runner; новый экземпляр не создан")
    if current["owned"] or current["terminal_open"]:
        raise ApiError(409, "Команда или её терминал уже открыты")
    target = target_for(spec)
    docker = target_is_docker(target)
    if docker:
        ensure_target(workspace, spec)
        validate_container_workdir(target)
    command = command_for(workspace, spec)
    terminal = bool(spec.get("terminal", True))
    base = runtime_base(str(workspace["id"]), step_id)
    for suffix in (".pid", ".shell.pid", ".rc"):
        runtime_file(base, suffix).unlink(missing_ok=True)
    if docker:
        prefix = f"/tmp/scenario-runner-{safe_id(str(workspace['id']))}-{safe_id(step_id)}"
        run(["docker", "exec", target["container_name"], "rm", "-f", prefix + ".pid", prefix + ".shell.pid", prefix + ".rc", prefix + ".log"])
        source = str(target.get("source_command") or "").strip()
        prepare = f"{source} && " if source else ""
        source_condition = source or "true"
        inner = (
            f"echo $$ > {shlex.quote(prefix + '.shell.pid')}; "
            f"trap {shlex.quote('rm -f ' + prefix + '.pid ' + prefix + '.shell.pid')} EXIT; "
            "trap 'exit 143' HUP TERM; "
            f"if {source_condition}; then set -m; "
            f"( {command} ) > >(tee {shlex.quote(prefix + '.log')}) 2>&1 & "
            f"runner_pid=$!; echo $runner_pid > {shlex.quote(prefix + '.pid')}; fg %1; runner_rc=$?; "
            f"rm -f {shlex.quote(prefix + '.pid')}; echo $runner_rc > {shlex.quote(prefix + '.rc')}; "
            "else runner_rc=127; printf '\n[Scenario Runner] Не удалось подготовить окружение цели.\n'; fi; "
            "printf '\n[Scenario Runner] Команда завершена, код %s. Терминал остаётся открытым.\n' \"$runner_rc\"; bash -i"
        )
        if terminal:
            argv = ["docker", "exec", "-it", "-w", target["workdir"], target["container_name"], "bash", "-lc", inner]
            open_terminal(f"Scenario Runner · {spec['title']}", shlex.join(argv), hold_on_error=True)
        else:
            background = (
                f"echo $$ > {shlex.quote(prefix + '.pid')}; trap {shlex.quote('rm -f ' + prefix + '.pid')} EXIT HUP TERM; "
                f"{prepare}( {command} ) > {shlex.quote(prefix + '.log')} 2>&1; runner_rc=$?; trap - EXIT; "
                f"rm -f {shlex.quote(prefix + '.pid')}; echo $runner_rc > {shlex.quote(prefix + '.rc')}"
            )
            result = run(["docker", "exec", "-d", "-w", target["workdir"], target["container_name"], "bash", "-lc", background])
            if result.returncode:
                raise ApiError(500, result.stderr.strip() or "docker exec завершился с ошибкой")
    else:
        configured_workdir = host_workdir(workspace, spec)
        workdir = configured_workdir if configured_workdir.is_dir() else Path.home()
        source = str(target.get("source_command") or "").strip()
        prepared_command = f"{source} && ( {command} )" if source else command
        pid_file, shell_file, rc_file, log_file = (runtime_file(base, s) for s in (".pid", ".shell.pid", ".rc", ".log"))
        if terminal:
            cleanup = f"rm -f {shlex.quote(str(pid_file))} {shlex.quote(str(shell_file))}"
            inner = (
                f"cd {shlex.quote(str(workdir))}; echo $$ > {shlex.quote(str(shell_file))}; "
                f"trap {shlex.quote(cleanup)} EXIT; trap {shlex.quote(cleanup + '; exit 0')} HUP TERM; set -m; "
                f"( {prepared_command} ) > >(tee {shlex.quote(str(log_file))}) 2>&1 & runner_pid=$!; "
                f"echo $runner_pid > {shlex.quote(str(pid_file))}; fg %1; runner_rc=$?; "
                f"rm -f {shlex.quote(str(pid_file))}; echo $runner_rc > {shlex.quote(str(rc_file))}; "
                "printf '\n[Scenario Runner] Команда завершена, код %s. Терминал остаётся открытым.\n' \"$runner_rc\"; bash -i"
            )
            open_terminal(f"Scenario Runner · Ubuntu · {spec['title']}", inner)
        else:
            wrapped = (
                f"echo $$ > {shlex.quote(str(pid_file))}; trap {shlex.quote('rm -f ' + str(pid_file))} EXIT HUP TERM; "
                f"( {prepared_command} ); runner_rc=$?; trap - EXIT; rm -f {shlex.quote(str(pid_file))}; "
                f"echo $runner_rc > {shlex.quote(str(rc_file))}; exit $runner_rc"
            )
            log = log_file.open("wb")
            try:
                subprocess.Popen(["bash", "-lc", wrapped], cwd=workdir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                log.close()
    workspace["steps"][step_id].update(status="starting", message="Команда запускается…")


def descendants(root: int) -> list[int]:
    result = run(["ps", "-eo", "pid=,ppid="], timeout=3)
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        values = line.split()
        if len(values) == 2 and all(value.isdigit() for value in values):
            children.setdefault(int(values[1]), []).append(int(values[0]))
    output: list[int] = []
    def collect(pid: int) -> None:
        for child in children.get(pid, []):
            collect(child)
        output.append(pid)
    collect(root)
    return output


def stop_step(workspace: dict, step_id: str) -> None:
    spec = next((item for item in preset_for(workspace)["steps"] if item["id"] == step_id), None)
    if not spec:
        raise ApiError(404, "Команда не найдена")
    state = step_status(workspace, spec)
    if state["external"]:
        raise ApiError(409, "Внешний процесс не принадлежит Scenario Runner и не будет остановлен")
    target = target_for(spec)
    if target_is_docker(target):
        prefix = f"/tmp/scenario-runner-{safe_id(str(workspace['id']))}-{safe_id(step_id)}"
        if container_running(target):
            result = run(["docker", "exec", target["container_name"], "python3", "-c", CONTAINER_STOP_SCRIPT, prefix], timeout=8)
            if result.returncode:
                raise ApiError(500, result.stderr.strip() or "Не удалось остановить команду в контейнере")
            if container_terminal_pids(target, prefix):
                raise ApiError(500, "Терминал сценария остался открыт; повторите остановку")
    else:
        base = runtime_base(str(workspace["id"]), step_id)
        roots = [file_pid(runtime_file(base, ".pid")), file_pid(runtime_file(base, ".shell.pid"))]
        targets = []
        for pid in roots:
            if pid:
                targets.extend(descendants(pid))
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in dict.fromkeys(targets):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            if sig == signal.SIGTERM:
                time.sleep(0.4)
        runtime_file(base, ".pid").unlink(missing_ok=True)
        runtime_file(base, ".shell.pid").unlink(missing_ok=True)
    workspace["steps"][step_id].update(status="stopped", message="Остановлено пользователем")


def refresh_status_cache() -> None:
    with LOCK:
        workspace_copies = copy.deepcopy(workspaces)
        target_copies = copy.deepcopy(settings["targets"])
        revisions = copy.deepcopy(WORKSPACE_REVISIONS)
    snapshots = {
        target["id"]: container_snapshot(target)
        for target in target_copies if target["type"] == "docker"
    }
    visible = [public_workspace(item, snapshots) for item in workspace_copies]
    target_states = [
        {
            **target,
            "running": True if target["type"] == "local" else bool(snapshots.get(target["id"], {}).get("running")),
        }
        for target in target_copies
    ]
    with STATUS_LOCK:
        STATUS_CACHE.update({"workspaces": visible, "targets": target_states, "revisions": revisions, "updated_at": now_iso()})


def status_worker() -> None:
    while True:
        try:
            refresh_status_cache()
        except Exception:
            LOG.exception("Не удалось обновить статусы")
        time.sleep(2)


def state_payload() -> dict:
    with STATUS_LOCK:
        cached = copy.deepcopy(STATUS_CACHE)
    with LOCK:
        cached_by_id = {str(item["id"]): item for item in cached["workspaces"]}
        visible = []
        for workspace in workspaces:
            sync_workspace(workspace)
            old = cached_by_id.get(str(workspace["id"]), {})
            item = copy.deepcopy(workspace)
            item["preset"] = copy.deepcopy(preset_for(workspace))
            cached_steps = old.get("steps", {}) if old.get("preset_id") == workspace.get("preset_id") else {}
            fresh = WORKSPACE_REVISIONS.get(str(workspace["id"]), 0) > cached.get("revisions", {}).get(str(workspace["id"]), 0)
            item["steps"] = {}
            for spec in item["preset"]["steps"]:
                step = {**workspace["steps"][spec["id"]], **cached_steps.get(spec["id"], {"status": "idle", "message": "Статус обновляется…", "owned": False, "terminal_open": False, "external": False, "log_tail": ""})}
                step["selected"] = workspace["steps"][spec["id"]]["selected"]
                if fresh:
                    step["status"] = workspace["steps"][spec["id"]]["status"]
                    step["message"] = workspace["steps"][spec["id"]]["message"]
                    if step["status"] == "stopped":
                        step["owned"] = False
                        step["terminal_open"] = False
                item["steps"][spec["id"]] = step
            visible.append(item)
        target_states = cached["targets"] or [{**item, "running": item["type"] == "local"} for item in settings["targets"]]
        return {
            "version": APP_VERSION,
            "settings": copy.deepcopy(settings),
            "presets": [copy.deepcopy(item) for item in presets.values()],
            "workspaces": visible,
            "targets": target_states,
            "status_updated_at": cached["updated_at"],
            "paths": {"config": str(CONFIG_DIR), "runtime": str(RUNTIME_DIR)},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ScenarioRunner/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def json_response(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(415, "Для API требуется Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ApiError(413, "Слишком большой запрос")
            value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, json.JSONDecodeError):
            raise ApiError(400, "Некорректный JSON")

    def path_parts(self) -> list[str]:
        return [urllib.parse.unquote(value) for value in urllib.parse.urlparse(self.path).path.split("/") if value]

    def check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urllib.parse.urlparse(origin)
        try:
            allowed = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == self.server.server_port
        except ValueError:
            allowed = False
        if not allowed:
            raise ApiError(403, "Запрос из другого сайта отклонён")

    def workspace(self, workspace_id: str) -> dict:
        workspace = next((item for item in workspaces if str(item["id"]) == workspace_id), None)
        if not workspace:
            raise ApiError(404, "Панель не найдена")
        return workspace

    def do_GET(self) -> None:
        try:
            parts = self.path_parts()
            if parts == ["api", "state"]:
                self.json_response(state_payload())
                return
            path = STATIC_DIR / ("index.html" if not parts else "/".join(parts))
            try:
                path = path.resolve()
                path.relative_to(STATIC_DIR.resolve())
            except ValueError:
                raise ApiError(404, "Файл не найден")
            if not path.is_file():
                path = STATIC_DIR / "index.html"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except ApiError as exc:
            self.json_response({"error": str(exc)}, exc.status)

    def do_POST(self) -> None:
        try:
            self.check_origin()
            parts = self.path_parts()
            body = self.body()
            with ACTION_LOCK, LOCK:
                if parts == ["api", "settings"]:
                    if "targets" not in body or not isinstance(body["targets"], list):
                        raise ApiError(400, "Передайте список целей выполнения")
                    normalized = normalize_settings({"targets": body["targets"]})
                    available = {target["id"] for target in normalized["targets"]}
                    required = {str(step.get("target_id") or "local") for preset in presets.values() for step in preset["steps"]}
                    missing = sorted(required - available)
                    if missing:
                        raise ApiError(409, "Нельзя удалить используемые цели: " + ", ".join(missing))
                    settings.clear()
                    settings.update(normalized)
                    atomic_json(SETTINGS_FILE, settings)
                elif parts == ["api", "presets"]:
                    preset_id = str(body.get("id") or "") or None
                    if preset_id:
                        for workspace in workspaces:
                            if workspace.get("preset_id") != preset_id:
                                continue
                            if any(
                                (status := step_status(workspace, spec))["owned"] or status["terminal_open"]
                                for spec in preset_for(workspace)["steps"]
                            ):
                                raise ApiError(409, "Пресет сейчас выполняется. Сначала завершите его команды")
                    item = normalize_preset(body, preset_id)
                    available = {target["id"] for target in settings["targets"]}
                    missing = sorted({step["target_id"] for step in item["steps"]} - available)
                    if missing:
                        raise ApiError(409, "В сценарии выбраны отсутствующие цели: " + ", ".join(missing))
                    presets[item["id"]] = item
                    atomic_json(PRESETS_DIR / f"{item['id']}.json", item)
                elif parts == ["api", "workspaces"]:
                    name = str(body.get("name") or "").strip()
                    if not name:
                        raise ApiError(400, "Введите название панели")
                    preset_id = str(body.get("preset_id") or "")
                    if preset_id not in presets:
                        raise ApiError(404, "Пресет не найден")
                    workspace = {
                        "id": unique_id("panel"), "name": name[:160],
                        "bag_path": str(body.get("bag_path") or "").strip(),
                        "variables": body.get("variables") if isinstance(body.get("variables"), dict) else {},
                        "preset_id": preset_id, "created_at": now_iso(), "steps": {},
                    }
                    sync_workspace(workspace)
                    workspaces.append(workspace)
                    save_workspaces(workspace)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "update":
                    workspace = self.workspace(parts[2])
                    if "name" in body and str(body["name"]).strip():
                        workspace["name"] = str(body["name"]).strip()[:160]
                    if "bag_path" in body:
                        workspace["bag_path"] = str(body["bag_path"]).strip()
                    if "variables" in body and isinstance(body["variables"], dict):
                        workspace["variables"] = {str(key): str(value) for key, value in body["variables"].items()}
                    if "preset_id" in body:
                        if body["preset_id"] not in presets:
                            raise ApiError(404, "Пресет не найден")
                        if any(
                            (status := step_status(workspace, spec))["owned"] or status["terminal_open"]
                            for spec in preset_for(workspace)["steps"]
                        ):
                            raise ApiError(409, "Сначала завершите активные команды")
                        workspace["preset_id"] = body["preset_id"]
                        workspace["steps"] = {}
                        sync_workspace(workspace)
                    save_workspaces(workspace)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "select":
                    workspace = self.workspace(parts[2])
                    step_id = parts[4]
                    if step_id not in workspace["steps"]:
                        raise ApiError(404, "Команда не найдена")
                    workspace["steps"][step_id]["selected"] = bool(body.get("selected"))
                    save_workspaces(workspace)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "run":
                    workspace = self.workspace(parts[2])
                    launch_step(workspace, parts[4])
                    save_workspaces(workspace)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "stop":
                    workspace = self.workspace(parts[2])
                    stop_step(workspace, parts[4])
                    save_workspaces(workspace)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "run":
                    workspace = self.workspace(parts[2])
                    chosen = [spec for spec in preset_for(workspace)["steps"] if workspace["steps"][spec["id"]].get("selected")]
                    if not chosen:
                        raise ApiError(409, "Выберите хотя бы одну команду")
                    for spec in chosen:
                        state = step_status(workspace, spec)
                        if not state["owned"] and not state["terminal_open"] and not state["external"]:
                            launch_step(workspace, spec["id"])
                    save_workspaces(workspace)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "stop":
                    workspace = self.workspace(parts[2])
                    for spec in reversed(preset_for(workspace)["steps"]):
                        state = step_status(workspace, spec)
                        if state["owned"] or state["terminal_open"]:
                            stop_step(workspace, spec["id"])
                    save_workspaces(workspace)
                else:
                    raise ApiError(404, "Метод API не найден")
            self.json_response(state_payload())
        except ApiError as exc:
            self.json_response({"error": str(exc)}, exc.status)
        except Exception as exc:
            LOG.error("Необработанная ошибка: %s\n%s", exc, traceback.format_exc())
            self.json_response({"error": f"Внутренняя ошибка: {exc}"}, 500)

    def do_DELETE(self) -> None:
        try:
            self.check_origin()
            parts = self.path_parts()
            with ACTION_LOCK, LOCK:
                if len(parts) == 3 and parts[:2] == ["api", "workspaces"]:
                    workspace = self.workspace(parts[2])
                    for spec in preset_for(workspace)["steps"]:
                        state = step_status(workspace, spec)
                        if state["owned"] or state["terminal_open"]:
                            raise ApiError(409, "Сначала завершите команды этой панели")
                    workspaces.remove(workspace)
                    save_workspaces()
                elif len(parts) == 3 and parts[:2] == ["api", "presets"]:
                    preset_id = parts[2]
                    if any(item.get("preset_id") == preset_id for item in workspaces):
                        raise ApiError(409, "Пресет используется панелью")
                    if preset_id not in presets:
                        raise ApiError(404, "Пресет не найден")
                    presets.pop(preset_id)
                    (PRESETS_DIR / f"{preset_id}.json").unlink(missing_ok=True)
                else:
                    raise ApiError(404, "Метод API не найден")
            self.json_response(state_payload())
        except ApiError as exc:
            self.json_response({"error": str(exc)}, exc.status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Native scenario runner")
    parser.add_argument("--host", default=os.getenv("SCENARIO_RUNNER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SCENARIO_RUNNER_PORT", "8010")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_all()
    refresh_status_cache()
    threading.Thread(target=status_worker, name="status-worker", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("Scenario Runner %s: http://%s:%s", APP_VERSION, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
