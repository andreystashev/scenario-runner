"""Real Windows execution and process ownership regression checks."""
import os
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import server
import test_execution as execution

wait_for = execution.wait_for


@unittest.skipUnless(os.name == "nt", "Windows execution adapter")
class WindowsExecutionTests(unittest.TestCase):
    setUp = execution.ExecutionTests.setUp

    def launch_console(self):
        original_popen = subprocess.Popen
        processes = []
        def console_popen(*args, **kwargs):
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            kwargs["startupinfo"] = startup
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        def cleanup():
            server.stop_step(self.workspace, "one")
            for process in processes:
                process.wait(timeout=5)
        self.addCleanup(cleanup)
        with patch.object(server.subprocess, "Popen", side_effect=console_popen):
            server.launch_step(self.workspace, "one")
        return server.runtime_base("test", "one")

    def console_input(self, pid, text=None, keys=None):
        # Attach from a separate detached helper so unittest's own console is
        # untouched. Send real console keys, including Up/Enter, to PowerShell.
        if keys is None:
            keys = [(0, char) for char in text] + [(13, "\r")]
        script = """import json,sys
import platform_support as p
if not p.kernel.AttachConsole(int(sys.argv[1])):
    raise p.ctypes.WinError(p.ctypes.get_last_error())
p.console_keys(json.loads(sys.argv[2]))
"""
        result = subprocess.run([sys.executable, "-c", script, str(pid), json.dumps(keys)],
                                cwd=server.APP_DIR, capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.DETACHED_PROCESS)
        self.assertEqual(result.returncode, 0, result.stderr)
    def test_literal_variables_unicode_and_workdir(self):
        project = self.root / "проект с пробелами"
        project.mkdir()
        server.settings["targets"][0]["workdir"] = str(project)
        message = "Привет 'quoted'; $(throw 'injected') ` literal & value"
        self.workspace["variables"] = {"message": message}
        self.spec["command"] = "Write-Output {{message}}; Write-Output (Get-Location).Path"
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 0)
        self.assertIn(message, state["log_tail"])
        self.assertIn(str(project), state["log_tail"])

    def test_setup_and_error_codes(self):
        server.settings["targets"][0]["source_command"] = "$env:RUNNER_TEST = 'prepared'"
        self.spec["command"] = "Write-Output $env:RUNNER_TEST; throw 'expected failure'"
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 1)
        self.assertIn("prepared", state["log_tail"])
        self.assertIn("expected failure", state["log_tail"])

    def test_native_program_exit_code(self):
        self.spec["command"] = server.host_argv_command([sys.executable, "-c", "raise SystemExit(23)"])
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 23)

    def test_console_command_shell_recovery_and_close(self):
        hits = self.root / "history-hits.txt"
        quote = server.platform.shell_quote
        self.spec.update(terminal=True, command="Add-Content -LiteralPath " + quote(str(hits))
                         + " -Value 'initial'; $saved = 'kept'; Write-Output 'terminal-output'; Start-Sleep -Seconds 1")
        base = self.launch_console()
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec))["owned"] else None))
        self.assertTrue(state["terminal_open"])
        with self.assertRaises(server.ApiError):
            server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") == 0 else None))
        self.assertFalse(state["owned"])
        self.assertTrue(state["terminal_open"])
        self.assertIn("terminal-output", state["log_tail"])
        worker = server.windows_worker_pid(base)
        # The real Up key must recall the exact scenario command, without a
        # runner wrapper. Pressing Enter reruns it and keeps this shell alive.
        self.console_input(worker, keys=[(38, "\0"), (13, "\r")])
        wait_for(lambda: hits.exists() and len(hits.read_text().splitlines()) == 2)
        result_path = self.root / "manual-command.txt"
        self.console_input(worker, text="$saved + ':' + (Get-Location).Path | Set-Content -LiteralPath "
                           + quote(str(result_path)))
        wait_for(lambda: result_path.exists())
        self.assertEqual(result_path.read_text().strip(), "kept:" + str(self.root))
        self.assertTrue(server.step_status(self.workspace, self.spec)["terminal_open"])
        tty = self.root / "tty.json"
        command = server.host_argv_command([sys.executable, "-c",
            "import sys,json; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps([sys.stdin.isatty(),sys.stdout.isatty(),sys.stderr.isatty()]))", str(tty)])
        self.console_input(worker, text=command)
        wait_for(lambda: tty.exists())
        self.assertEqual(json.loads(tty.read_text()), [True, True, True])
        # Start an extra manual child: Stop must close the whole terminal tree.
        self.console_input(worker, text="Start-Sleep -Seconds 60")
        children = server.descendants(worker)
        server.runtime_file(base, ".shell.pid").unlink()
        self.assertTrue(server.step_status(self.workspace, self.spec)["terminal_open"])
        server.stop_step(self.workspace, "one")
        self.assertFalse(server.step_status(self.workspace, self.spec)["terminal_open"])
        for pid in children:
            wait_for(lambda: not server.process_alive(pid))

    def test_console_multiline_history_and_error_keeps_shell(self):
        output = self.root / "multiline.txt"
        self.spec.update(terminal=True, command="$multi = 'hello'\n$multi | Set-Content -LiteralPath "
                         + server.platform.shell_quote(str(output)) + "\nthrow 'expected terminal error'")
        base = self.launch_console()
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None), timeout=15)
        self.assertEqual(state["return_code"], 1)
        self.assertTrue(state["terminal_open"])
        self.assertEqual(output.read_text().strip(), "hello")
        self.assertIn("expected terminal error", state["log_tail"])
        worker = server.windows_worker_pid(base)
        history = self.root / "history.json"
        self.console_input(worker, text="(Get-History)[-1].CommandLine | ConvertTo-Json | Set-Content -LiteralPath "
                           + server.platform.shell_quote(str(history)))
        wait_for(lambda: history.exists())
        self.assertEqual(json.loads(history.read_text()), self.spec["command"])
        self.console_input(worker, text="exit")
        wait_for(lambda: not server.process_alive(worker))
        self.assertFalse(server.step_status(self.workspace, self.spec)["terminal_open"])

    def test_console_ctrl_c_stops_command_but_keeps_shell(self):
        self.spec.update(terminal=True, command="Start-Sleep -Seconds 60")
        base = self.launch_console()
        wait_for(lambda: server.step_status(self.workspace, self.spec)["owned"], timeout=15)
        worker = server.windows_worker_pid(base)
        script = """import sys,signal,time
import platform_support as p
signal.signal(signal.SIGINT, lambda *_: None)
if not p.kernel.AttachConsole(int(sys.argv[1])):
    raise p.ctypes.WinError(p.ctypes.get_last_error())
p.kernel.SetConsoleCtrlHandler(None, True)
if not p.kernel.GenerateConsoleCtrlEvent(0, 0):
    raise p.ctypes.WinError(p.ctypes.get_last_error())
time.sleep(.2)
"""
        result = subprocess.run([sys.executable, "-c", script, str(worker)], cwd=server.APP_DIR,
                                capture_output=True, text=True, timeout=5,
                                creationflags=subprocess.DETACHED_PROCESS)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertTrue(state["terminal_open"])
        self.assertFalse(state["owned"])
        output = self.root / "after-interrupt.txt"
        self.console_input(worker, text="'still usable' | Set-Content -LiteralPath "
                           + server.platform.shell_quote(str(output)))
        wait_for(lambda: output.exists())
        self.assertEqual(output.read_text().strip(), "still usable")

    def test_console_setup_failure_does_not_close_shell(self):
        server.settings["targets"][0]["source_command"] = "throw 'setup failed'"
        marker = self.root / "must-not-run.txt"
        self.spec.update(terminal=True, command="'unexpected' | Set-Content -LiteralPath "
                         + server.platform.shell_quote(str(marker)))
        self.launch_console()
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None), timeout=15)
        self.assertEqual(state["return_code"], 1)
        self.assertTrue(state["terminal_open"])
        self.assertFalse(marker.exists())

    def test_console_explicit_exit_closes_shell_with_its_code(self):
        self.spec.update(terminal=True, command="exit 23")
        base = self.launch_console()
        worker = server.windows_worker_pid(base)
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None), timeout=15)
        self.assertEqual(state["return_code"], 23)
        wait_for(lambda: not server.process_alive(worker))
        self.assertFalse(server.step_status(self.workspace, self.spec)["terminal_open"])

    def test_console_existing_keybinding_is_preserved(self):
        marker = self.root / "custom-key.txt"
        server.settings["targets"][0]["source_command"] = "Set-PSReadLineKeyHandler -Key F24 -ScriptBlock { 'custom key' | Set-Content -LiteralPath " + server.platform.shell_quote(str(marker)) + " }"
        self.spec.update(terminal=True, command="Write-Output 'normal command'")
        base = self.launch_console()
        wait_for(lambda: server.step_status(self.workspace, self.spec).get("return_code") == 0, timeout=15)
        worker = server.windows_worker_pid(base)
        self.console_input(worker, keys=[(0x87, "\0")])
        wait_for(lambda: marker.exists())
        self.assertEqual(marker.read_text().strip(), "custom key")

    def test_foreign_owner_record_is_rejected(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()) if unrelated.poll() is None else None)
        base = server.runtime_base("test", "one")
        token = "a" * 32
        details = server.platform.process_details(unrelated.pid)
        server.atomic_json(server.runtime_file(base, ".worker.json"), {"token": token})
        server.atomic_json(server.runtime_file(base, ".owner.json"), {
            "pid": unrelated.pid, "created": details["created"], "token": token,
        })
        server.runtime_file(base, ".pid").write_text(str(unrelated.pid))
        self.assertIsNone(server.file_pid(server.runtime_file(base, ".pid")))
        server.stop_step(self.workspace, "one")
        self.assertIsNone(unrelated.poll())

    def test_docker_arguments_are_powershell_literals(self):
        command = 'echo "quoted"; echo $$; echo $(whoami)'
        result = server.host_argv_command(["docker", "exec", "demo", "bash", "-lc", command])
        self.assertEqual(result, "& 'docker' 'exec' 'demo' 'bash' '-lc' '" + command + "'")

    def test_stop_immediately_after_launch(self):
        self.spec["command"] = "Start-Sleep -Seconds 60"
        server.launch_step(self.workspace, "one")
        base = server.runtime_base("test", "one")
        pid = server.windows_worker_pid(base)
        self.assertIsNotNone(pid)
        server.stop_step(self.workspace, "one")
        self.assertFalse(server.process_alive(pid))
        self.assertFalse(server.step_status(self.workspace, self.spec)["owned"])

    def test_native_setup_failure_prevents_command(self):
        server.settings["targets"][0]["source_command"] = server.host_argv_command(
            [sys.executable, "-c", "raise SystemExit(17)"])
        self.spec["command"] = "Write-Output 'must not run'"
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 17)
        self.assertNotIn("must not run", state["log_tail"])
