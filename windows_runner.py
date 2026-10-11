"""Dedicated Windows command supervisor; launched only by server.py."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import platform_support as platform


def powershell():
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise OSError("PowerShell was not found")
    return executable


def execute_background(base, descriptor):
    def file(suffix):
        return Path(str(base) + suffix)
    rc = 1
    try:
        with file(".log").open("wb") as log:
            child = subprocess.Popen([powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                      "-File", str(file(".ps1"))], cwd=descriptor["workdir"],
                                     stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                file(".pid").write_text(str(os.getpid()), encoding="utf-8")
                while True:
                    chunk = child.stdout.read1(8192)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                rc = child.wait()
            finally:
                child.stdout.close()
                if child.poll() is None:
                    child.kill()
                child.wait()
    except Exception as exc:
        with file(".log").open("ab") as log:
            log.write((str(exc) + "\n").encode("utf-8"))
    finally:
        file(".pid").unlink(missing_ok=True)
        file(".rc").write_text(str(rc), encoding="utf-8")
    return rc


def terminal_bootstrap(base, descriptor):
    """A one-shot prompt hook observes a command entered by native PSReadLine.

    No output pipes, command execution wrapper, or replacement REPL are involved.
    The original prompt is restored after the first command. A temporarily
    reserved, unbound function key leaves the user's existing bindings intact.
    """
    quote = platform.shell_quote
    state_name = "__scenario_runner_" + descriptor["token"]
    script = r'''
Import-Module PSReadLine -ErrorAction Stop
$global:STATE = @{
    descriptor = Get-Content -LiteralPath DESCRIPTOR -Raw -Encoding UTF8 | ConvertFrom-Json
    ready = READY
    pidFile = PIDFILE
    rcFile = RCFILE
    workerPid = WORKERPID
    started = $false
    announced = $false
    prompt = (Get-Item Function:\prompt).ScriptBlock
    key = $null
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$Host.UI.RawUI.WindowTitle = $global:STATE.descriptor.title
try { Start-Transcript -LiteralPath LOGFILE -Force -ErrorAction Stop | Out-Null } catch { Write-Warning $_ }
if ($global:STATE.descriptor.source) {
    $setupCode = 0
    try {
        . ([scriptblock]::Create($global:STATE.descriptor.source))
        if (-not $?) {
            $setupCode = 1
            if ($LASTEXITCODE) { $setupCode = $LASTEXITCODE }
        }
    } catch {
        Write-Error $_ -ErrorAction Continue
        $setupCode = 1
    }
    if ($setupCode) {
        [IO.File]::WriteAllText($global:STATE.rcFile, [string]$setupCode)
        [IO.File]::WriteAllText($global:STATE.ready, 'setup-failed')
        Remove-Variable STATE -Scope Global
        return
    }
}
$global:LASTEXITCODE = 0
$boundKeys = @(Get-PSReadLineKeyHandler -Bound | ForEach-Object { $_.Key })
$launchKey = 24..13 | Where-Object { $boundKeys -notcontains "F$_" } | Select-Object -First 1
if (-not $launchKey) { throw 'No unused PSReadLine function key is available for launching the command' }
$global:STATE.key = "F$launchKey"
$global:STATE.virtualKey = 111 + $launchKey
Set-PSReadLineKeyHandler -Key $global:STATE.key -ScriptBlock {
    [IO.File]::WriteAllText($global:STATE.pidFile, [string]$global:STATE.workerPid)
    $global:STATE.started = $true
    [Microsoft.PowerShell.PSConsoleReadLine]::Insert($global:STATE.descriptor.command)
    [Microsoft.PowerShell.PSConsoleReadLine]::AcceptLine()
}
function global:prompt {
    $success = $?
    $nativeCode = $global:LASTEXITCODE
    $state = $global:STATE
    if ($state.started) {
        $code = 0
        if ($nativeCode) { $code = $nativeCode } elseif (-not $success) { $code = 1 }
        [IO.File]::WriteAllText($state.rcFile, [string]$code)
        Remove-Item -LiteralPath $state.pidFile -Force -ErrorAction SilentlyContinue
        Set-Item Function:\global:prompt -Value $state.prompt
        Remove-PSReadLineKeyHandler -Key $state.key
        Remove-Variable STATE -Scope Global
    } elseif (-not $state.announced) {
        $state.announced = $true
        [IO.File]::WriteAllText($state.ready, 'key:' + $state.virtualKey)
    }
    & $state.prompt
}
'''
    # Replace fixed placeholders once; descriptor/user text never becomes PS code.
    import re
    values = {
        "STATE": state_name, "DESCRIPTOR": quote(str(base) + ".worker.json"),
        "READY": quote(str(base) + ".ready"), "PIDFILE": quote(str(base) + ".pid"),
        "RCFILE": quote(str(base) + ".rc"), "LOGFILE": quote(str(base) + ".log"),
        "WORKERPID": str(os.getpid()),
    }
    return re.sub(r"\b(?:STATE|DESCRIPTOR|READY|PIDFILE|RCFILE|LOGFILE|WORKERPID)\b",
                  lambda match: values[match[0]], script)


def execute_terminal(base, descriptor):
    def file(suffix):
        return Path(str(base) + suffix)
    file(".shell.pid").write_text(str(os.getpid()), encoding="utf-8")
    file(".terminal.ps1").write_text(terminal_bootstrap(base, descriptor), encoding="utf-8-sig")
    child = subprocess.Popen([powershell(), "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass",
                              "-File", str(file(".terminal.ps1"))], cwd=descriptor["workdir"])
    try:
        deadline = time.monotonic() + 15
        while not file(".ready").exists():
            if child.poll() is not None:
                raise RuntimeError("PowerShell exited before its interactive prompt was ready")
            if time.monotonic() >= deadline:
                raise RuntimeError("PowerShell interactive prompt did not become ready")
            time.sleep(.03)
        ready = file(".ready").read_text(encoding="utf-8")
        if ready.startswith("key:"):
            platform.console_keys([(int(ready[4:]), "\0")])  # Accept the literal, possibly multiline command.
        rc = child.wait()
        # 'exit' deliberately closes the shell without reaching another prompt.
        if not file(".rc").exists():
            file(".rc").write_text(str(rc), encoding="utf-8")
        return rc
    finally:
        file(".pid").unlink(missing_ok=True)
        file(".shell.pid").unlink(missing_ok=True)
        if child.poll() is None:
            child.kill()
        child.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    base = Path(args.base)
    def file(suffix):
        return Path(str(base) + suffix)
    descriptor = json.loads(file(".worker.json").read_text(encoding="utf-8"))
    if descriptor["token"] != args.token:
        raise RuntimeError("Command instance changed before startup")
    if descriptor["terminal"]:
        # Ctrl+C belongs to the interactive PowerShell/program. The invisible
        # supervisor must not unwind its job and close the entire terminal.
        signal.signal(signal.SIGINT, lambda *_: None)
        signal.signal(signal.SIGBREAK, lambda *_: None)
    try:
        # Children inherit the supervisor's job at creation. An immediate Stop
        # cannot leave a child behind between Popen and a later job assignment.
        with platform.process_job():
            return execute_terminal(base, descriptor) if descriptor["terminal"] else execute_background(base, descriptor)
    except Exception as exc:
        with file(".log").open("ab") as log:
            log.write((str(exc) + "\n").encode("utf-8"))
        file(".rc").write_text("1", encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
