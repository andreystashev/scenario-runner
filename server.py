#!/usr/bin/env python3
"""Scenario Runner: local macOS/Linux scenario launcher."""

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
import socket
import socketserver
import sys
import subprocess
import threading
import tempfile
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_VERSION = "0.8.0"
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
    def __init__(self, status: int, message: str, *, failures: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.failures = failures


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def safe_id(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^A-Za-z0-9А-Яа-яЁё_.-]+", "-", value.strip()).strip("-.")
    return value[:80] or fallback


def unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
VARIABLE_TOKEN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_.-]*)\}\}")
RESERVED_VARIABLES = {"project_path", "bag_path", "bag_folder"}


def normalize_variables(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ApiError(400, "Переменные должны быть JSON-объектом")
    result = {}
    for key, value in raw.items():
        if not VARIABLE_NAME.fullmatch(str(key)):
            raise ApiError(400, f"Некорректное имя переменной: {key}")
        if key in RESERVED_VARIABLES:
            raise ApiError(400, f"Имя {key} зарезервировано для пути панели")
        if not isinstance(value, (str, int, float, bool)):
            raise ApiError(400, f"Значение переменной {key} должно быть текстом или числом")
        result[str(key)] = str(value)
    return result


def step_is_active(status: dict) -> bool:
    return bool(status.get("owned") or status.get("terminal_open") or status.get("status") == "starting")


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
        if not isinstance(item, dict) or not isinstance(item.get("command"), str) or not item["command"].strip():
            raise ApiError(400, f"Введите команду {index + 1}")
        step = normalize_step(item if isinstance(item, dict) else {}, index)
        if any(key in item and not isinstance(item[key], bool) for key in ("terminal", "selected")):
            raise ApiError(400, f"Режим и выбор команды {index + 1} должны быть true или false")
        if step["process_match"]:
            try:
                re.compile(step["process_match"])
            except re.error as exc:
                raise ApiError(400, f"Некорректный regex команды {index + 1}: {exc}")
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


def normalize_settings(raw: dict | None, *, strict: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    incoming_targets = raw.get("targets")
    if strict:
        if not isinstance(incoming_targets, list) or not incoming_targets:
            raise ApiError(400, "Добавьте хотя бы одну цель выполнения")
        used_ids = set()
        for item in incoming_targets:
            if not isinstance(item, dict) or item.get("type") not in ("local", "docker"):
                raise ApiError(400, "Укажите тип цели: local или docker")
            target_id = str(item.get("id") or "").strip()
            if not target_id or safe_id(target_id) != target_id:
                raise ApiError(400, "Идентификатор цели содержит недопустимые символы")
            if target_id in used_ids:
                raise ApiError(400, "Идентификаторы целей должны различаться")
            if target_id == "local" and item["type"] != "local":
                raise ApiError(400, "Идентификатор local зарезервирован для локальной цели")
            used_ids.add(target_id)
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
    valid_workspaces = []
    for workspace in workspaces:
        try:
            sync_workspace(workspace)
        except ApiError as exc:
            # Keep the original file available for repair; don't rewrite it here.
            LOG.warning("Панель %s пропущена: %s", workspace.get("id"), exc)
        else:
            valid_workspaces.append(workspace)
    workspaces = valid_workspaces


def save_workspaces(changed: dict | None = None) -> None:
    atomic_json(WORKSPACES_FILE, {"version": 1, "workspaces": workspaces})
    record_revision(changed)


def record_revision(changed: dict | None) -> None:
    global STATE_REVISION
    if changed is not None:
        STATE_REVISION += 1
        WORKSPACE_REVISIONS[str(changed["id"])] = STATE_REVISION


def commit_workspace(candidate: dict, previous: dict | None = None) -> None:
    """Persist a validated configuration before publishing it to live state.

    Call while holding LOCK. This does not roll back launched/stopped processes.
    """
    updated = [candidate if item is previous else item for item in workspaces]
    if previous is None:
        updated.append(candidate)
    atomic_json(WORKSPACES_FILE, {"version": 1, "workspaces": updated})
    if previous is None:
        workspaces.append(candidate)
    else:
        previous.clear()
        previous.update(candidate)
    record_revision(candidate)


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


def normalize_panel_appearance(raw: Any, *, strict: bool = True) -> dict:
    """Only allow a small set of presentation values, never arbitrary CSS."""
    if not isinstance(raw, dict):
        if strict:
            raise ApiError(400, "Оформление панели должно быть JSON-объектом")
        return {}
    result = {}
    for key, value in raw.items():
        valid = (
            key in ("accent", "surface", "text") and isinstance(value, str)
            and re.fullmatch(r"#[0-9a-fA-F]{6}", value)
        ) or (key == "layout" and value in ("grid", "list"))
        if not valid:
            if strict:
                raise ApiError(400, "Некорректное оформление панели")
            continue
        result[key] = value.lower() if key != "layout" else value
    return result


def sync_workspace(workspace: dict) -> None:
    preset = preset_for(workspace)
    old = workspace.get("steps") if isinstance(workspace.get("steps"), dict) else {}
    workspace["preset_id"] = preset["id"]
    workspace.setdefault("variables", {})
    workspace["appearance"] = normalize_panel_appearance(workspace.get("appearance", {}), strict=False)
    workspace["steps"] = {
        spec["id"]: {
            **old.get(spec["id"], {}),
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
        if not process_alive(value):
            return None
        # A stale PID file must never turn an unrelated process into our own.
        result = run(["ps", "-ww", "-p", str(value), "-o", "stat=,command="])
        fields = result.stdout.strip().split(None, 1)
        if len(fields) != 2 or fields[0].startswith("Z"):
            return None
        command = fields[1]
        if re.match(r"(?:\S*/)?bash\s+-lc\s", command) and f"echo $$ > {shlex.quote(str(path))}" in command:
            return value
        if str(path).endswith(".shell.pid"):
            locator_path = Path(str(path)[:-10] + ".terminal.json")
            try:
                locator = json.loads(locator_path.read_text(encoding="utf-8"))
                script_path = locator.get("script_path", "")
                if script_path and command in ("/bin/bash " + script_path, "bash " + script_path):
                    return value
            except (OSError, ValueError, AttributeError):
                pass
        if str(path).endswith(".pid") and not str(path).endswith(".shell.pid"):
            shell_path = Path(str(path)[:-4] + ".shell.pid")
            shell = file_pid(shell_path)
            if shell and value != shell and value in descendants(shell):
                return value
        return None
    except (OSError, ValueError):
        return None


def terminal_script_pid(base: Path) -> int | None:
    try:
        locator = json.loads(runtime_file(base, ".terminal.json").read_text(encoding="utf-8"))
        script_path = locator.get("script_path")
        if not isinstance(script_path, str) or not script_path:
            return None
        result = run(["ps", "-ww", "-axo", "pid=,stat=,command="])
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and not fields[1].startswith("Z") and fields[2] in ("/bin/bash " + script_path, "bash " + script_path):
                return int(fields[0])
    except (OSError, ValueError, AttributeError):
        pass
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
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{Path(args[0]).name}: нет ответа за {timeout:g} секунд")
    except OSError as exc:
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
    values = {"project_path": target_workdir}
    variables = workspace.get("variables") if isinstance(workspace.get("variables"), dict) else {}
    for key, value in variables.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", str(key)):
            if key not in RESERVED_VARIABLES:
                values[str(key)] = str(value)
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
        values.update(bag_path=bag_path, bag_folder=bag_path)
    missing = sorted(set(VARIABLE_TOKEN.findall(command)) - values.keys())
    if missing:
        raise ApiError(409, "Заполните переменные в параметрах панели: " + ", ".join(missing))
    # Substitute once: variable contents may contain literal {{other}} text.
    return VARIABLE_TOKEN.sub(lambda match: shlex.quote(values[match[1]]), command)


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
        shell_pid = file_pid(runtime_file(base, ".shell.pid")) or terminal_script_pid(base)
        rc = read_return_code(str(runtime_file(base, ".rc")))
        log = tail(runtime_file(base, ".log"))
    if not shell_pid and workspace.get("steps", {}).get(spec["id"], {}).get("terminal_close_error") and runtime_file(base, ".terminal.json").exists():
        return {"status": "terminal", "message": "Команда остановлена; окно Terminal не закрыто. Повторите закрытие.", "owned": False, "terminal_open": True, "external": False, "return_code": rc, "log_tail": log}
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
    stored = workspace.get("steps", {}).get(spec["id"], {})
    if stored.get("status") == "starting":
        pending = time.time() - stored.get("started_at", 0) < 5
        return {"status": "starting" if pending else "failed", "message": "Команда запускается…" if pending else "Процесс не появился. Проверьте терминал и повторите запуск.", "owned": False, "terminal_open": False, "external": False, "log_tail": log}
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


def open_terminal(title: str, command: str, hold_on_error: bool = False) -> dict | None:
    if hold_on_error:
        command = (
            f"{command}; runner_outer_rc=$?; "
            "if [ $runner_outer_rc -ne 0 ]; then "
            "printf '\n[Scenario Runner] Не удалось начать выполнение, код %s. "
            "Терминал оставлен открытым для диагностики.\n' \"$runner_outer_rc\"; exec bash -i; fi"
        )
    if sys.platform == "darwin":
        title = title[:120] + " [" + uuid.uuid4().hex[:8] + "]"
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        script_path = RUNTIME_DIR / ("terminal-" + uuid.uuid4().hex + ".command")
        script_path.write_text("#!/bin/bash\nexport BASH_SILENCE_DEPRECATION_WARNING=1\n" + command + "\n", encoding="utf-8")
        script_path.chmod(0o600)
        # Send one short line to the existing login shell; do not reload its profile.
        script = '''on run argv
tell application "Terminal"
    set runnerTab to do script (item 1 of argv)
    set custom title of runnerTab to (item 2 of argv)
    activate
    return tty of runnerTab
end tell
end run'''
        result = run(["/usr/bin/osascript", "-e", script,
                      "exec /bin/bash " + shlex.quote(str(script_path)), title[:140]], timeout=10)
        if result.returncode:
            raise ApiError(503, "Не удалось открыть Terminal.app. Разрешите управление Terminal "
                           "в настройках macOS → Конфиденциальность и безопасность → Автоматизация. "
                           + result.stderr.strip())
        return {"tty": result.stdout.strip(), "title": title, "script_path": str(script_path)}
    terminal = terminal_executable()
    if Path(terminal).name == "gnome-terminal":
        argv = [terminal, "--wait", f"--title={title[:140]}", "--", "bash", "-lc", command]
    else:
        argv = [terminal, "-T", title[:140], "-e", "bash", "-lc", command]
    subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def remember_terminal(base: Path, locator: dict | None) -> None:
    if locator and re.fullmatch(r"/dev/ttys\w+", locator.get("tty", "")):
        atomic_json(runtime_file(base, ".terminal.json"), locator)


def close_mac_terminal(base: Path) -> None:
    path = runtime_file(base, ".terminal.json")
    if sys.platform != "darwin" or not path.exists():
        return
    try:
        locator = json.loads(path.read_text(encoding="utf-8"))
        tty, title = locator["tty"], locator["title"]
        if not re.fullmatch(r"/dev/ttys\w+", tty) or not isinstance(title, str):
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    # Terminal only supports closing windows, not tabs. Our launch owns a new
    # single-tab window; refuse to close it if the user has added another tab.
    script = '''on run argv
 tell application "Terminal"
    if not running then return "missing"
    repeat with runnerWindow in windows
        repeat with runnerTab in tabs of runnerWindow
            if (tty of runnerTab is (item 1 of argv)) and (custom title of runnerTab is (item 2 of argv)) then
                if (count of tabs of runnerWindow) is not 1 then return "shared"
                repeat 40 times
                    if not busy of runnerTab then
                        close runnerWindow
                        return "closed"
                    end if
                    delay 0.1
                end repeat
                return "busy"
            end if
        end repeat
    end repeat
    return "missing"
end tell
end run'''
    result = run(["/usr/bin/osascript", "-e", script, tty, title], timeout=8)
    outcome = result.stdout.strip()
    if result.returncode or outcome not in ("closed", "missing"):
        message = "В окне сценария есть другие вкладки. Закройте вкладку сценария вручную." if outcome == "shared" else "Не удалось закрыть окно Terminal. Повторите остановку. " + result.stderr.strip()
        raise ApiError(503, message)
    script_path = locator.get("script_path")
    if isinstance(script_path, str):
        candidate = Path(script_path)
        if candidate.parent == RUNTIME_DIR and re.fullmatch(r"terminal-[a-f0-9]{32}\.command", candidate.name):
            candidate.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


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
    command = f"cd {shlex.quote(str(workdir))} && {start_command}"
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
    if current["status"] == "starting":
        raise ApiError(409, "Команда уже запускается; дождитесь обновления статуса")
    target = target_for(spec)
    docker = target_is_docker(target)
    if not docker and not host_workdir(workspace, spec).is_dir():
        raise ApiError(409, f"Рабочая папка не найдена: {host_workdir(workspace, spec)}")
    if docker:
        ensure_target(workspace, spec)
        validate_container_workdir(target)
    command = command_for(workspace, spec)
    terminal = bool(spec.get("terminal", True))
    base = runtime_base(str(workspace["id"]), step_id)
    for suffix in (".pid", ".shell.pid", ".rc", ".terminal.json"):
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
            "printf '\n[Scenario Runner] Команда завершена, код %s. Терминал остаётся открытым.\n' \"$runner_rc\"; bash -i; exit $?"
        )
        if terminal:
            argv = ["docker", "exec", "-it", "-w", target["workdir"], target["container_name"], "bash", "-lc", inner]
            remember_terminal(base, open_terminal(f"Scenario Runner · {spec['title']}", shlex.join(argv), hold_on_error=True))
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
        workdir = configured_workdir
        source = str(target.get("source_command") or "").strip()
        prepared_command = f"{source} && ( {command} )" if source else command
        pid_file, shell_file, rc_file, log_file = (runtime_file(base, s) for s in (".pid", ".shell.pid", ".rc", ".log"))
        if terminal:
            cleanup = f"rm -f {shlex.quote(str(pid_file))} {shlex.quote(str(shell_file))}"
            inner = (
                f"cd {shlex.quote(str(workdir))} || exit 1; echo $$ > {shlex.quote(str(shell_file))}; "
                f"trap {shlex.quote(cleanup)} EXIT; trap {shlex.quote(cleanup + '; exit 0')} HUP TERM; set -m; "
                f"( {prepared_command} ) > >(tee {shlex.quote(str(log_file))}) 2>&1 & runner_pid=$!; "
                f"echo $runner_pid > {shlex.quote(str(pid_file))}; fg %1; runner_rc=$?; "
                f"rm -f {shlex.quote(str(pid_file))}; echo $runner_rc > {shlex.quote(str(rc_file))}; "
                "printf '\n[Scenario Runner] Команда завершена, код %s. Терминал остаётся открытым.\n' \"$runner_rc\"; bash -i; exit $?"
            )
            remember_terminal(base, open_terminal(f"Scenario Runner · {spec['title']}", inner))
        else:
            wrapped = (
                f"echo $$ > {shlex.quote(str(pid_file))}; trap {shlex.quote('rm -f ' + shlex.quote(str(pid_file)))} EXIT; trap 'exit 143' HUP TERM; "
                f"( {prepared_command} ); runner_rc=$?; trap - EXIT; rm -f {shlex.quote(str(pid_file))}; "
                f"echo $runner_rc > {shlex.quote(str(rc_file))}; exit $runner_rc"
            )
            log = log_file.open("wb")
            try:
                process = subprocess.Popen(["bash", "-lc", wrapped], cwd=workdir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                threading.Thread(target=process.wait, daemon=True).start()
            finally:
                log.close()
    workspace["steps"][step_id].update(status="starting", message="Команда запускается…", started_at=time.time())


def descendants(root: int) -> list[int]:
    result = run(["ps", "-eo", "pid=,ppid="], timeout=3)
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        values = line.split()
        if len(values) == 2 and all(value.isdigit() for value in values):
            children.setdefault(int(values[1]), []).append(int(values[0]))
    output: list[int] = []
    def collect(pid: int) -> None:
        if pid in visited:
            return
        visited.add(pid)
        for child in children.get(pid, []):
            collect(child)
        output.append(pid)
    visited: set[int] = set()
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
    base = runtime_base(str(workspace["id"]), step_id)
    if target_is_docker(target):
        prefix = f"/tmp/scenario-runner-{safe_id(str(workspace['id']))}-{safe_id(step_id)}"
        if container_running(target):
            result = run(["docker", "exec", target["container_name"], "python3", "-c", CONTAINER_STOP_SCRIPT, prefix], timeout=8)
            if result.returncode:
                raise ApiError(500, result.stderr.strip() or "Не удалось остановить команду в контейнере")
            if container_terminal_pids(target, prefix):
                raise ApiError(500, "Терминал сценария остался открыт; повторите остановку")
    else:
        roots = [file_pid(runtime_file(base, ".pid")), file_pid(runtime_file(base, ".shell.pid")) or terminal_script_pid(base)]
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
    try:
        close_mac_terminal(base)
    except ApiError:
        workspace["steps"][step_id]["terminal_close_error"] = True
        raise
    workspace["steps"][step_id].pop("terminal_close_error", None)
    workspace["steps"][step_id].update(status="stopped", message="Остановлено пользователем")


def refresh_status_cache() -> None:
    with LOCK:
        workspace_copies = copy.deepcopy(workspaces)
        target_copies = copy.deepcopy(settings["targets"])
        revisions = copy.deepcopy(WORKSPACE_REVISIONS)
    runtime_states = {
        str(workspace["id"]): {
            step_id: [step.get("status"), step.get("started_at")]
            for step_id, step in workspace["steps"].items()
        }
        for workspace in workspace_copies
    }
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
        STATUS_CACHE.update({"workspaces": visible, "targets": target_states, "revisions": revisions, "runtime_states": runtime_states, "updated_at": now_iso()})


def run_workspace(workspace: dict) -> None:
    chosen = [spec for spec in preset_for(workspace)["steps"] if workspace["steps"][spec["id"]].get("selected")]
    if not chosen:
        raise ApiError(409, "Выберите хотя бы одну команду")
    pending = []
    for spec in chosen:
        status = step_status(workspace, spec)
        if step_is_active(status) or status.get("external"):
            continue
        command_for(workspace, spec)
        if not target_is_docker(spec) and not host_workdir(workspace, spec).is_dir():
            raise ApiError(409, f"Рабочая папка не найдена: {host_workdir(workspace, spec)}")
        pending.append(spec)
    if not pending:
        return
    # Preflight every selected command before launching any. If a runtime
    # failure happens later, persist the commands which already started.
    try:
        for spec in pending:
            launch_step(workspace, spec["id"])
    finally:
        save_workspaces(workspace)


def stop_workspace(workspace: dict) -> None:
    errors = []
    for spec in reversed(preset_for(workspace)["steps"]):
        if step_is_active(step_status(workspace, spec)):
            try:
                stop_step(workspace, spec["id"])
            except ApiError as exc:
                errors.append({"title": spec["title"], "message": str(exc)})
    save_workspaces(workspace)
    if errors:
        details = "; ".join(f"{item['title']}: {item['message']}" for item in errors)
        raise ApiError(503, "Не все команды остановлены. " + details, failures=errors)


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
            cached_runtime = cached.get("runtime_states", {}).get(str(workspace["id"]), {})
            item["steps"] = {}
            for spec in item["preset"]["steps"]:
                step = {**workspace["steps"][spec["id"]], **cached_steps.get(spec["id"], {"status": "idle", "message": "Статус обновляется…", "owned": False, "terminal_open": False, "external": False, "log_tail": ""})}
                step["selected"] = workspace["steps"][spec["id"]]["selected"]
                stored = workspace["steps"][spec["id"]]
                # Configuration edits must not replace an observed completion
                # with the old persisted "starting" status. Override only a
                # launch/stop transition not yet observed by the status worker.
                if [stored.get("status"), stored.get("started_at")] != cached_runtime.get(spec["id"]):
                    step["status"] = workspace["steps"][spec["id"]]["status"]
                    step["message"] = workspace["steps"][spec["id"]]["message"]
                    if step["status"] == "starting":
                        step["owned"] = False
                        step["terminal_open"] = False
                        step.pop("return_code", None)
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


class LocalServer(ThreadingHTTPServer):
    """Loopback HTTP server whose startup does not require reverse DNS."""

    def __init__(self, address, handler):
        self.address_family = socket.AF_INET6 if address[0] == "::1" else socket.AF_INET
        super().__init__(address, handler)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    server_version = f"ScenarioRunner/{APP_VERSION}"

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
            if length < 0:
                raise ApiError(400, "Некорректная длина запроса")
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
        try:
            host = urllib.parse.urlparse("http://" + self.headers.get("Host", ""))
            valid_host = host.hostname in ("127.0.0.1", "localhost", "::1") and (host.port or 80) == self.server.server_port
        except ValueError:
            valid_host = False
        if not valid_host:
            raise ApiError(403, "Некорректный Host: разрешён только локальный адрес сервера")
        origin = self.headers.get("Origin")
        if not origin:
            return
        try:
            parsed = urllib.parse.urlparse(origin)
            allowed = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and (parsed.port or 80) == self.server.server_port
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
            self.check_origin()
            parts = self.path_parts()
            if parts == ["api", "health"]:
                self.json_response({"app": "scenario-runner", "version": APP_VERSION,
                                    "instance_id": getattr(self.server, "instance_id", ""),
                                    "config": str(CONFIG_DIR.resolve())})
                return
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
                    normalized = normalize_settings({"targets": body["targets"]}, strict=True)
                    available = {target["id"] for target in normalized["targets"]}
                    required = {str(step.get("target_id") or "local") for preset in presets.values() for step in preset["steps"]}
                    missing = sorted(required - available)
                    if missing:
                        raise ApiError(409, "Нельзя удалить используемые цели: " + ", ".join(missing))
                    changed = {target["id"] for target in normalized["targets"] if target not in settings["targets"]}
                    for workspace in workspaces:
                        for spec in preset_for(workspace)["steps"]:
                            if spec["target_id"] in changed:
                                status = step_status(workspace, spec)
                                if step_is_active(status):
                                    raise ApiError(409, "Сначала завершите команды изменяемой цели")
                    atomic_json(SETTINGS_FILE, normalized)
                    settings.clear()
                    settings.update(normalized)
                elif parts == ["api", "presets"]:
                    preset_id = str(body.get("id") or "") or None
                    if preset_id:
                        for workspace in workspaces:
                            if workspace.get("preset_id") != preset_id:
                                continue
                            if any(
                                step_is_active(step_status(workspace, spec))
                                for spec in preset_for(workspace)["steps"]
                            ):
                                raise ApiError(409, "Пресет сейчас выполняется. Сначала завершите его команды")
                    item = normalize_preset(body, preset_id)
                    if not preset_id and item["id"] in presets:
                        raise ApiError(409, "Пресет с таким идентификатором уже существует. Измените название или откройте существующий пресет.")
                    available = {target["id"] for target in settings["targets"]}
                    missing = sorted({step["target_id"] for step in item["steps"]} - available)
                    if missing:
                        raise ApiError(409, "В сценарии выбраны отсутствующие цели: " + ", ".join(missing))
                    atomic_json(PRESETS_DIR / f"{item['id']}.json", item)
                    presets[item["id"]] = item
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
                        "variables": normalize_variables(body.get("variables", {})),
                        "appearance": normalize_panel_appearance(body.get("appearance", {})),
                        "preset_id": preset_id, "created_at": now_iso(), "steps": {},
                    }
                    sync_workspace(workspace)
                    commit_workspace(workspace)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "update":
                    previous = self.workspace(parts[2])
                    workspace = copy.deepcopy(previous)
                    if "name" in body and str(body["name"]).strip():
                        workspace["name"] = str(body["name"]).strip()[:160]
                    if "bag_path" in body:
                        workspace["bag_path"] = str(body["bag_path"]).strip()
                    if "variables" in body:
                        workspace["variables"] = normalize_variables(body["variables"])
                    if "appearance" in body:
                        workspace["appearance"] = normalize_panel_appearance(body["appearance"])
                    if "preset_id" in body:
                        if body["preset_id"] not in presets:
                            raise ApiError(404, "Пресет не найден")
                        if any(
                            step_is_active(step_status(workspace, spec))
                            for spec in preset_for(workspace)["steps"]
                        ):
                            raise ApiError(409, "Сначала завершите активные команды")
                        workspace["preset_id"] = body["preset_id"]
                        workspace["steps"] = {}
                        sync_workspace(workspace)
                    commit_workspace(workspace, previous)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "selection":
                    previous = self.workspace(parts[2])
                    if not isinstance(body.get("selected"), bool):
                        raise ApiError(400, "Передайте selected: true или false")
                    workspace = copy.deepcopy(previous)
                    for step in workspace["steps"].values():
                        step["selected"] = body["selected"]
                    commit_workspace(workspace, previous)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "select":
                    if not isinstance(body.get("selected"), bool):
                        raise ApiError(400, "Передайте selected: true или false")
                    previous = self.workspace(parts[2])
                    workspace = copy.deepcopy(previous)
                    step_id = parts[4]
                    if step_id not in workspace["steps"]:
                        raise ApiError(404, "Команда не найдена")
                    workspace["steps"][step_id]["selected"] = body["selected"]
                    commit_workspace(workspace, previous)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "run":
                    workspace = self.workspace(parts[2])
                    launch_step(workspace, parts[4])
                    save_workspaces(workspace)
                elif len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "steps" and parts[5] == "stop":
                    workspace = self.workspace(parts[2])
                    stop_step(workspace, parts[4])
                    save_workspaces(workspace)
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "run":
                    run_workspace(self.workspace(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "stop":
                    stop_workspace(self.workspace(parts[2]))
                else:
                    raise ApiError(404, "Метод API не найден")
            self.json_response(state_payload())
        except ApiError as exc:
            payload = {"error": str(exc)}
            if exc.failures is not None:
                payload["failures"] = exc.failures
            self.json_response(payload, exc.status)
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
                        if step_is_active(state):
                            raise ApiError(409, "Сначала завершите команды этой панели")
                    remaining = [item for item in workspaces if item is not workspace]
                    atomic_json(WORKSPACES_FILE, {"version": 1, "workspaces": remaining})
                    workspaces.remove(workspace)
                elif len(parts) == 3 and parts[:2] == ["api", "presets"]:
                    preset_id = parts[2]
                    if any(item.get("preset_id") == preset_id for item in workspaces):
                        raise ApiError(409, "Пресет используется панелью")
                    if preset_id not in presets:
                        raise ApiError(404, "Пресет не найден")
                    (PRESETS_DIR / f"{preset_id}.json").unlink(missing_ok=True)
                    presets.pop(preset_id)
                else:
                    raise ApiError(404, "Метод API не найден")
            self.json_response(state_payload())
        except ApiError as exc:
            self.json_response({"error": str(exc)}, exc.status)
        except Exception as exc:
            LOG.error("Необработанная ошибка: %s\n%s", exc, traceback.format_exc())
            self.json_response({"error": f"Внутренняя ошибка: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="Native scenario runner")
    parser.add_argument("--host", default=os.getenv("SCENARIO_RUNNER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SCENARIO_RUNNER_PORT", "8010")))
    parser.add_argument("--instance-id", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Сервер выполняет команды без авторизации: используйте только loopback host")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Загрузка конфигурации: %s", CONFIG_DIR)
    load_all()
    refresh_status_cache()
    threading.Thread(target=status_worker, name="status-worker", daemon=True).start()
    LOG.info("Открытие локального HTTP-порта %s:%s", args.host, args.port)
    server = LocalServer((args.host, args.port), Handler)
    server.instance_id = args.instance_id
    LOG.info("Scenario Runner %s: http://%s:%s", APP_VERSION, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
