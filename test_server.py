import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ScenarioRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.bag = self.project / "bags" / "demo"
        self.bag.mkdir(parents=True)
        server.settings = server.normalize_settings({"targets": [
            {"id": "local", "name": "Ubuntu", "type": "local", "workdir": str(self.project)},
            {"id": "docker", "name": "Docker", "type": "docker", "container_name": "test_container", "host_workdir": str(self.project), "workdir": "/workspace"},
        ]})
        server.presets = {"test": server.normalize_preset({"name": "Test", "steps": [{"id": "command-2", "command": "echo ok", "target_id": "docker"}]}, "test")}
        server.workspaces = []
        self.workspace = {
            "id": "panel-test", "name": "Test", "preset_id": "test",
            "bag_path": str(self.bag), "steps": {},
        }
        server.sync_workspace(self.workspace)

    def test_host_command_receives_real_host_paths(self):
        spec = {"command": "echo {{project_path}} {{bag_path}}", "target_id": "local", "terminal": False}
        command = server.command_for(self.workspace, spec)
        self.assertIn(str(self.project), command)
        self.assertIn(str(self.bag), command)

    def test_container_command_maps_bag_below_project(self):
        spec = {"command": "ros2 bag play {{bag_folder}}", "target_id": "docker", "terminal": False}
        command = server.command_for(self.workspace, spec)
        self.assertIn("/workspace/bags/demo", command)

    def test_container_bag_outside_project_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.workspace["bag_path"] = str(outside)
        spec = {"command": "echo {{bag_path}}", "target_id": "docker", "terminal": False}
        with self.assertRaises(server.ApiError):
            server.command_for(self.workspace, spec)

    def test_external_process_is_detected_but_not_owned(self):
        spec = {"id": "viewer", "target_id": "docker", "terminal": True, "process_match": "viewer.*config"}
        snapshot = {
            "running": True, "pids": {}, "codes": {}, "logs": {},
            "commands": [[42, "viewer --config /workspace/config"]],
        }
        status = server.step_status(self.workspace, spec, snapshot)
        self.assertTrue(status["external"])
        self.assertFalse(status["owned"])

    def test_own_pid_wins_over_external_match(self):
        spec = {"id": "viewer", "target_id": "docker", "terminal": True, "process_match": "viewer"}
        prefix = "/tmp/scenario-runner-panel-test-viewer"
        snapshot = {
            "running": True, "pids": {prefix + ".pid": 42}, "codes": {}, "logs": {},
            "commands": [[42, "viewer"]],
        }
        status = server.step_status(self.workspace, spec, snapshot)
        self.assertTrue(status["owned"])
        self.assertFalse(status["external"])

    def test_empty_external_pattern_does_not_scan(self):
        spec = {"id": "command", "target_id": "docker", "terminal": False, "process_match": ""}
        snapshot = {"running": True, "pids": {}, "codes": {}, "logs": {}, "commands": [[99, "anything"]]}
        self.assertEqual(server.external_pids(self.workspace, spec, snapshot), [])

    def test_preset_cannot_be_empty(self):
        with self.assertRaises(server.ApiError):
            server.normalize_preset({"name": "Empty", "steps": []})

    def test_old_execution_modes_are_migrated(self):
        local = server.normalize_step({"execution": "host_background"}, 0)
        docker = server.normalize_step({"execution": "container_terminal"}, 1)
        self.assertEqual((local["target_id"], local["terminal"]), ("local", False))
        self.assertEqual((docker["target_id"], docker["terminal"]), ("docker", True))

    def test_legacy_settings_are_migrated(self):
        migrated = server.normalize_settings({"project_path": str(self.project), "container_name": "custom"})
        docker = next(item for item in migrated["targets"] if item["type"] == "docker")
        self.assertEqual(docker["host_workdir"], str(self.project))
        self.assertEqual(docker["container_name"], "custom")

    def test_custom_variable_is_shell_quoted(self):
        self.workspace["variables"] = {"message": "hello world"}
        spec = {"command": "echo {{message}}", "target_id": "local", "terminal": False}
        self.assertEqual(server.command_for(self.workspace, spec), "echo 'hello world'")

    def test_missing_target_never_falls_back_to_local(self):
        with self.assertRaises(server.ApiError):
            server.target_for({"target_id": "removed-container"})

    @patch("server.run")
    def test_invalid_container_workdir_reports_detected_path(self, mocked_run):
        mocked_run.side_effect = [
            server.subprocess.CompletedProcess([], 1, "", ""),
            server.subprocess.CompletedProcess([], 0, "/home/demo/project\n", ""),
        ]
        target = {"container_name": "demo", "workdir": "/wrong"}
        with self.assertRaisesRegex(server.ApiError, "/home/demo/project"):
            server.validate_container_workdir(target)

    def test_terminal_is_recovered_after_pid_file_was_removed(self):
        spec = {"id": "command-2", "target_id": "docker", "terminal": True, "process_match": ""}
        prefix = "/tmp/scenario-runner-panel-test-command-2"
        snapshot = {"running": True, "pids": {}, "codes": {prefix + ".rc": 0}, "logs": {},
                    "commands": [[42, f"bash -lc echo $$ > {prefix}.shell.pid; bash -i"]]}
        status = server.step_status(self.workspace, spec, snapshot)
        self.assertTrue(status["terminal_open"])
        self.assertEqual(status["status"], "terminal")

    def test_stopped_status_does_not_revert_to_idle(self):
        spec = {"id": "command-2", "target_id": "docker", "terminal": True, "process_match": ""}
        self.workspace["steps"]["command-2"] = {"selected": True, "status": "stopped", "message": "Остановлено пользователем"}
        snapshot = {"running": True, "pids": {}, "codes": {}, "logs": {}, "commands": []}
        status = server.step_status(self.workspace, spec, snapshot)
        self.assertEqual(status["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
