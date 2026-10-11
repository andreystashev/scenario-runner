"""Real local processes, with isolated runtime/config and harmless commands."""
import json
import fcntl
import termios
import pty
import os
import socket
import select
import threading
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher
import server


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError("Timed out waiting for process state")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner with spaces '")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = {"id": "test", "preset_id": "test", "steps": {}}
        preset = server.normalize_preset({"name": "Test", "steps": [
            {"id": "one", "command": "printf 'hello from macOS/Linux'; exit 7", "terminal": False}]}, "test")
        for name, value in {"RUNTIME_DIR": self.root, "settings": server.normalize_settings({"targets": [
                {"id": "local", "workdir": str(self.root)}]}), "presets": {"test": preset}}.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.sync_workspace(self.workspace)
        self.spec = preset["steps"][0]

    def test_background_output_exit_and_restart(self):
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 7)
        self.assertEqual(state["status"], "failed")
        self.assertIn("hello from macOS/Linux", state["log_tail"])
        self.spec["command"] = "echo recovered"
        server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") is not None else None))
        self.assertEqual(state["return_code"], 0)
        self.assertIn("recovered", state["log_tail"])

    def test_stop_process_tree_and_leave_unrelated_process(self):
        self.spec["command"] = "sleep 60 & wait"
        unrelated = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()) if unrelated.poll() is None else None)
        self.addCleanup(lambda: server.stop_step(self.workspace, "one"))
        server.launch_step(self.workspace, "one")
        wait_for(lambda: server.step_status(self.workspace, self.spec)["owned"])
        root = server.file_pid(server.runtime_file(server.runtime_base("test", "one"), ".pid"))
        children = wait_for(lambda: [pid for pid in server.descendants(root) if pid != root])
        server.stop_step(self.workspace, "one")
        self.assertIsNone(unrelated.poll())
        self.assertFalse(server.step_status(self.workspace, self.spec)["owned"])
        for pid in children:
            result = server.run(["ps", "-p", str(pid), "-o", "stat="])
            self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"))

    def test_stale_pid_cannot_own_or_stop_unrelated_process(self):
        unrelated = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()) if unrelated.poll() is None else None)
        path = server.runtime_file(server.runtime_base("test", "one"), ".pid")
        path.write_text(str(unrelated.pid))
        self.assertIsNone(server.file_pid(path))
        server.stop_step(self.workspace, "one")
        self.assertIsNone(unrelated.poll())

    def test_bad_workdir_rejected_before_execution(self):
        server.settings["targets"][0]["workdir"] = str(self.root / "missing")
        with self.assertRaisesRegex(server.ApiError, "Рабочая папка"):
            server.launch_step(self.workspace, "one")

    def test_terminal_wrapper_tracks_live_command_and_shell(self):
        self.spec.update(terminal=True, command="echo terminal-output; sleep 1")
        master, slave = pty.openpty()
        self.addCleanup(lambda: os.close(master))
        self.addCleanup(lambda: os.close(slave))
        drain_stop = threading.Event()
        def drain():
            while not drain_stop.is_set():
                try:
                    if select.select([master], [], [], .1)[0]:
                        os.read(master, 8192)
                except OSError:
                    break
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        self.addCleanup(lambda: (drain_stop.set(), reader.join(timeout=1)))
        closer = patch.object(server, "close_mac_terminal", return_value=None)
        closer.start()
        self.addCleanup(closer.stop)
        processes = []
        def open_in_pty(title, command):
            script_path = self.root / ("terminal-" + "a" * 32 + ".command")
            script_path.write_text(command + "\n")
            process = subprocess.Popen(["/bin/bash", str(script_path)], stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=True,
                                       preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
            processes.append(process)
            return {"tty": "/dev/ttysTest", "title": "Test [unique]", "script_path": str(script_path)}
        def cleanup():
            server.stop_step(self.workspace, "one")
            for process in processes:
                process.wait(timeout=3)
        self.addCleanup(cleanup)
        with patch.object(server, "open_terminal", side_effect=open_in_pty):
            server.launch_step(self.workspace, "one")
        state = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec))["owned"] else None))
        self.assertTrue(state["terminal_open"])
        self.assertEqual(state["status"], "running")
        finished = wait_for(lambda: (s if (s := server.step_status(self.workspace, self.spec)).get("return_code") == 0 else None))
        self.assertTrue(finished["terminal_open"])
        self.assertFalse(finished["owned"])
        self.assertIn("terminal-output", finished["log_tail"])
        # Simulate a PID file removed while the dedicated Terminal shell is alive.
        server.runtime_file(server.runtime_base("test", "one"), ".shell.pid").unlink()
        self.assertTrue(server.step_status(self.workspace, self.spec)["terminal_open"])
        server.stop_step(self.workspace, "one")
        self.assertFalse(server.step_status(self.workspace, self.spec)["terminal_open"])


    def test_pending_launch_cannot_duplicate(self):
        self.spec["terminal"] = True
        with patch.object(server, "open_terminal", return_value=None) as terminal:
            server.launch_step(self.workspace, "one")
            with self.assertRaises(server.ApiError):
                server.launch_step(self.workspace, "one")
        terminal.assert_called_once()

    def test_mac_terminal_quotes_command_as_data(self):
        command = 'echo "quoted"; printf "\\n"'
        with patch.object(server.sys, "platform", "darwin"), patch.object(server, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            locator = server.open_terminal('Title "with quotes"', command)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/osascript")
            self.assertNotIn(command, argv[2])
            script_path = Path(locator["script_path"])
            self.assertEqual(argv[3], "exec /bin/bash " + server.shlex.quote(str(script_path)))
            self.assertIn(command, script_path.read_text())
            self.assertNotIn("\n", argv[3])
            self.assertTrue(argv[4].startswith('Title "with quotes" ['))

    def test_mac_permission_error_visible(self):
        with patch.object(server.sys, "platform", "darwin"), patch.object(server, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "Not authorized (-1743)")
            with self.assertRaisesRegex(server.ApiError, "Автоматизация"):
                server.open_terminal("Test", "echo test")

    def test_terminal_close_failure_preserves_locator_and_retry_button(self):
        base = server.runtime_base("test", "one")
        server.remember_terminal(base, {"tty": "/dev/ttys999", "title": "Test [unique]"})
        with patch.object(server.sys, "platform", "darwin"), patch.object(server, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "shared", "")
            with self.assertRaisesRegex(server.ApiError, "другие вкладки"):
                server.stop_step(self.workspace, "one")
            self.assertTrue(server.runtime_file(base, ".terminal.json").exists())
            server.sync_workspace(self.workspace)
            self.assertTrue(server.step_status(self.workspace, self.spec)["terminal_open"])
            run.return_value = subprocess.CompletedProcess([], 0, "closed", "")
            server.stop_step(self.workspace, "one")
            script = run.call_args.args[0][2]
            self.assertIn("close runnerWindow", script)
            self.assertNotIn("close runnerTab", script)
        self.assertFalse(server.runtime_file(base, ".terminal.json").exists())


    def test_preset_validation_and_unique_ids(self):
        for step in ({"command": ""}, {"command": "echo ok", "process_match": "["}, "wrong"):
            with self.assertRaises(server.ApiError):
                server.normalize_preset({"name": "Test", "steps": [step]})
        self.assertEqual(len({server.unique_id("test") for _ in range(1000)}), 1000)

    def test_http_start_does_not_require_working_reverse_dns(self):
        with patch.object(server.socket, "getfqdn", side_effect=OSError("DNS unavailable")):
            http = server.LocalServer(("127.0.0.1", 0), server.Handler)
            self.addCleanup(http.server_close)
        self.assertEqual(http.server_name, "127.0.0.1")
        self.assertGreater(http.server_port, 0)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launcher space '")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = {**os.environ, "SCENARIO_RUNNER_CACHE_DIR": str(self.root / "cache"),
                    "SCENARIO_RUNNER_CONFIG_DIR": str(self.root / "config"),
                    "SCENARIO_RUNNER_PORT": str(self.port), "SCENARIO_RUNNER_NO_OPEN": "1"}
        self.addCleanup(lambda: self.call("stop"))

    def call(self, action, **env):
        return subprocess.run([sys.executable, str(launcher.APP_DIR / "launcher.py"), action],
                              env={**self.env, **env}, capture_output=True, text=True, timeout=15)

    def test_start_repeat_stop_and_port_change(self):
        result = self.call("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        record_path = self.root / "cache" / "server.json"
        record = json.loads(record_path.read_text())
        self.assertEqual(self.call("start").returncode, 0)
        self.assertEqual(json.loads(record_path.read_text()), record)
        self.assertNotEqual(self.call("start", SCENARIO_RUNNER_PORT=str(self.port + 1)).returncode, 0)
        self.assertEqual(self.call("stop").returncode, 0)
        self.assertFalse(record_path.exists())
        self.assertEqual(self.call("stop").returncode, 0)

    def test_shell_entrypoints(self):
        env = {**self.env, "SCENARIO_RUNNER_PYTHON": sys.executable}
        for script in ("start", "start.command", "stop.sh"):
            result = subprocess.run([str(launcher.APP_DIR / script)], env=env, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_foreign_pid_record_never_killed(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "server.json").write_text(json.dumps({"pid": os.getpid(), "instance_id": "foreign"}))
        self.assertEqual(self.call("stop").returncode, 0)
        os.kill(os.getpid(), 0)

    def test_occupied_port_does_not_report_success_or_kill_listener(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
            sock.listen()
            result = self.call("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "cache" / "server.json").exists())
            self.assertEqual(sock.getsockname()[1], self.port)

    def test_concurrent_start_creates_one_server(self):
        command = [sys.executable, str(launcher.APP_DIR / "launcher.py"), "start"]
        processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        def cleanup_launchers():
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        self.addCleanup(cleanup_launchers)
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, out + err)
        self.assertEqual(self.call("stop").returncode, 0)
