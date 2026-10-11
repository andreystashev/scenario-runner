"""Configuration regressions through a real, isolated HTTP server."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.LocalServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        settings = server.normalize_settings(None)
        preset = server.normalize_preset({"name": "Test", "steps": [{"command": "echo ok", "terminal": False}]}, "test")
        workspace = {"id": "panel-test", "name": "Original", "preset_id": "test", "steps": {}, "variables": {}}
        values = {
            "CONFIG_DIR": root, "PRESETS_DIR": root / "presets",
            "SETTINGS_FILE": root / "settings.json", "WORKSPACES_FILE": root / "workspaces.json",
            "RUNTIME_DIR": root / "runtime", "settings": settings,
            "presets": {"test": preset}, "workspaces": [workspace],
            "WORKSPACE_REVISIONS": {}, "STATE_REVISION": 0,
            "STATUS_CACHE": {"workspaces": [], "targets": [], "revisions": {}, "updated_at": None},
        }
        for name, value in values.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.sync_workspace(workspace)
        server.atomic_json(server.SETTINGS_FILE, settings)
        server.atomic_json(server.PRESETS_DIR / "test.json", preset)
        server.save_workspaces()
        for name in ("error", "info", "warning"):
            patcher = patch.object(server.LOG, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.before_workspace = copy.deepcopy(workspace)
        self.before_disk = server.WORKSPACES_FILE.read_bytes()

    def request(self, path, body=None, method="POST", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request(method, path, None if body is None else json.dumps(body),
                               headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_workspace_unchanged(self):
        self.assertEqual(server.workspaces, [self.before_workspace])
        self.assertEqual(server.WORKSPACES_FILE.read_bytes(), self.before_disk)
        self.assertEqual(server.STATE_REVISION, 0)
        self.assertEqual(server.WORKSPACE_REVISIONS, {})

    def test_bad_variables_do_not_change_workspace(self):
        for variables in ({"bad name": "x"}, {"project_path": "/wrong"}, {"nested": {}}, []):
            status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed", "variables": variables})
            self.assertEqual(status, 400)
            self.assert_workspace_unchanged()

    def test_panel_appearance_is_persisted_and_other_panels_inherit_defaults(self):
        appearance = {"accent": "#AABBEE", "surface": "#ffffff", "layout": "list"}
        status, state = self.request("/api/workspaces/panel-test/update", {"appearance": appearance})
        self.assertEqual(status, 200)
        expected = {**appearance, "accent": "#aabbee"}
        self.assertEqual(state["workspaces"][0]["appearance"], expected)
        disk = json.loads(server.WORKSPACES_FILE.read_text())
        self.assertEqual(disk["workspaces"][0]["appearance"], expected)
        status, state = self.request("/api/workspaces", {"name": "Another", "preset_id": "test"})
        self.assertEqual(status, 200)
        self.assertEqual(state["workspaces"][1]["appearance"], {})
        status, state = self.request("/api/workspaces/panel-test/update", {"appearance": {}})
        self.assertEqual(status, 200)
        self.assertEqual(state["workspaces"][0]["appearance"], {})

    def test_invalid_panel_appearance_and_failed_writes_leave_configuration_intact(self):
        for appearance in ([], {"text": "red"}, {"accent": "url(evil)"}, {"layout": "unknown"}, {"css": "display:none"}):
            status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed", "appearance": appearance})
            self.assertEqual(status, 400)
            self.assert_workspace_unchanged()
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/workspaces/panel-test/update", {"appearance": {"accent": "#aabbee"}})
        self.assertEqual(status, 500)
        self.assert_workspace_unchanged()

    def test_legacy_panel_and_invalid_saved_appearance_load_with_safe_defaults(self):
        workspace = {"id": "legacy", "preset_id": "test", "appearance": {"accent": "url(evil)", "text": "#eeeeee"}}
        server.sync_workspace(workspace)
        self.assertEqual(workspace["appearance"], {"text": "#eeeeee"})
        workspace.pop("appearance")
        server.sync_workspace(workspace)
        self.assertEqual(workspace["appearance"], {})

    def test_selection_is_atomic_and_persisted(self):
        status, state = self.request("/api/workspaces/panel-test/selection", {"selected": False})
        self.assertEqual(status, 200)
        self.assertFalse(state["workspaces"][0]["steps"]["command-1"]["selected"])
        before = copy.deepcopy(server.workspaces)
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/workspaces/panel-test/selection", {"selected": True})
        self.assertEqual(status, 500)
        self.assertEqual(server.workspaces, before)

    def test_invalid_target_ids_are_rejected_without_silent_renaming(self):
        before = copy.deepcopy(server.settings)
        targets = before["targets"]
        for invalid in ([], targets + targets, [{**targets[0], "id": "bad id"}], [{**targets[0], "type": "unknown"}]):
            status, _ = self.request("/api/settings", {"targets": invalid})
            self.assertEqual(status, 400)
            self.assertEqual(server.settings, before)

    def test_selection_requires_boolean(self):
        for suffix in ("selection", "steps/command-1/select"):
            status, _ = self.request("/api/workspaces/panel-test/" + suffix, {"selected": "false"})
            self.assertEqual(status, 400)
            self.assert_workspace_unchanged()

    def test_pending_launch_blocks_preset_changes_and_panel_removal(self):
        with patch.object(server, "step_status", return_value={"status": "starting", "owned": False, "terminal_open": False}):
            status, _ = self.request("/api/workspaces/panel-test", method="DELETE")
            self.assertEqual(status, 409)
            status, _ = self.request("/api/presets", {"id": "test", "name": "Test", "steps": [{"command": "echo new"}]})
            self.assertEqual(status, 409)
        self.assert_workspace_unchanged()

    def test_batch_preflight_rejects_before_any_command_starts(self):
        server.presets["test"]["steps"].append(server.normalize_step({"id": "second", "command": "echo {{missing}}", "terminal": False}, 1))
        server.sync_workspace(server.workspaces[0])
        with patch.object(server, "launch_step") as launch:
            status, body = self.request("/api/workspaces/panel-test/run", {})
        self.assertEqual(status, 409)
        self.assertIn("missing", body["error"])
        launch.assert_not_called()

    def test_batch_stop_continues_after_one_terminal_fails(self):
        server.presets["test"]["steps"].append(server.normalize_step({"id": "second", "command": "echo ok", "terminal": False}, 1))
        server.sync_workspace(server.workspaces[0])
        with patch.object(server, "step_status", return_value={"owned": True, "terminal_open": False}), patch.object(server, "stop_step", side_effect=[server.ApiError(503, "close failed"), None]) as stop:
            status, body = self.request("/api/workspaces/panel-test/stop", {})
        self.assertEqual(status, 503)
        self.assertIn("close failed", body["error"])
        self.assertEqual(body["failures"], [{"title": "Команда 2", "message": "close failed"}])
        self.assertEqual(stop.call_count, 2)

    def test_started_steps_are_saved_after_later_launch_failure(self):
        server.presets["test"]["steps"].append(server.normalize_step({"id": "second", "command": "echo ok", "terminal": False}, 1))
        server.sync_workspace(server.workspaces[0])
        def launch(workspace, step_id):
            if step_id == "second":
                raise server.ApiError(503, "Terminal unavailable")
            workspace["steps"][step_id]["status"] = "starting"
        with patch.object(server, "step_status", return_value={"status": "idle", "owned": False, "terminal_open": False}), patch.object(server, "launch_step", side_effect=launch):
            status, _ = self.request("/api/workspaces/panel-test/run", {})
        self.assertEqual(status, 503)
        disk = json.loads(server.WORKSPACES_FILE.read_text())
        self.assertEqual(disk["workspaces"][0]["steps"]["command-1"]["status"], "starting")


    def test_new_launch_does_not_inherit_cached_completion(self):
        workspace = server.workspaces[0]
        workspace["steps"]["command-1"].update(status="starting", started_at=12345)
        server.record_revision(workspace)
        cached = copy.deepcopy(workspace)
        cached["steps"]["command-1"].update(status="terminal", return_code=0, terminal_open=True, owned=False)
        server.STATUS_CACHE["workspaces"] = [cached]
        state = server.state_payload()
        step = state["workspaces"][0]["steps"]["command-1"]
        self.assertEqual(step["status"], "starting")
        self.assertEqual(step["started_at"], 12345)
        self.assertNotIn("return_code", step)
        self.assertFalse(step["terminal_open"])

    def test_selection_does_not_restore_old_starting_status(self):
        workspace = server.workspaces[0]
        workspace["steps"]["command-1"].update(status="starting", started_at=12345)
        base = server.runtime_base("panel-test", "command-1")
        server.RUNTIME_DIR.mkdir()
        server.runtime_file(base, ".rc").write_text("2")
        server.refresh_status_cache()
        status, state = self.request("/api/workspaces/panel-test/selection", {"selected": False})
        self.assertEqual(status, 200)
        step = state["workspaces"][0]["steps"]["command-1"]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["return_code"], 2)
        self.assertFalse(step["selected"])


    def test_foreign_host_is_rejected_for_reads_and_writes(self):
        for method in ("GET", "POST"):
            status, _ = self.request("/api/state" if method == "GET" else "/api/settings", {}, method=method,
                                     headers={"Content-Type": "application/json", "Host": f"evil.example:{self.http.server_port}"})
            self.assertEqual(status, 403)

    def test_foreign_origin_cannot_mutate_configuration(self):
        status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed"},
                                 headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assert_workspace_unchanged()

    def test_new_preset_cannot_overwrite_existing_slug(self):
        before = copy.deepcopy(server.presets)
        status, _ = self.request("/api/presets", {"name": "test", "steps": [{"command": "echo new"}]})
        self.assertEqual(status, 409)
        self.assertEqual(server.presets, before)

    def test_active_target_cannot_change_under_running_command(self):
        targets = copy.deepcopy(server.settings["targets"])
        targets[0]["workdir"] = "/other"
        before = copy.deepcopy(server.settings)
        with patch.object(server, "step_status", return_value={"owned": True, "terminal_open": False}):
            status, _ = self.request("/api/settings", {"targets": targets})
        self.assertEqual(status, 409)
        self.assertEqual(server.settings, before)


    def test_invalid_preset_does_not_partially_rename_workspace(self):
        status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed", "preset_id": "missing"})
        self.assertEqual(status, 404)
        self.assert_workspace_unchanged()
        status, state = self.request("/api/state", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(state["workspaces"][0]["name"], "Original")

    def test_active_workspace_rejection_keeps_other_fields(self):
        with patch.object(server, "step_status", return_value={"owned": True, "terminal_open": False}):
            status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed", "preset_id": "test"})
        self.assertEqual(status, 409)
        self.assert_workspace_unchanged()

    def test_disk_failure_keeps_workspace_and_revision(self):
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/workspaces/panel-test/update", {"name": "Changed", "variables": {"x": "new"}})
        self.assertEqual(status, 500)
        self.assert_workspace_unchanged()

    def test_successful_update_is_persisted_and_visible(self):
        status, state = self.request("/api/workspaces/panel-test/update", {"name": "Changed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["workspaces"][0]["name"], "Changed")
        self.assertEqual(json.loads(server.WORKSPACES_FILE.read_text())["workspaces"][0]["name"], "Changed")
        self.assertEqual(server.WORKSPACE_REVISIONS["panel-test"], 1)

    def test_failed_creation_does_not_leave_phantom_workspace(self):
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/workspaces", {"name": "New", "preset_id": "test"})
        self.assertEqual(status, 500)
        self.assert_workspace_unchanged()

    def test_failed_selection_keeps_checkbox(self):
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/workspaces/panel-test/steps/command-1/select", {"selected": False})
        self.assertEqual(status, 500)
        self.assert_workspace_unchanged()

    def test_failed_settings_write_keeps_live_targets(self):
        original = copy.deepcopy(server.settings)
        disk = server.SETTINGS_FILE.read_bytes()
        targets = copy.deepcopy(original["targets"])
        targets[0]["name"] = "Changed"
        with patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/settings", {"targets": targets})
        self.assertEqual(status, 500)
        self.assertEqual(server.settings, original)
        self.assertEqual(server.SETTINGS_FILE.read_bytes(), disk)

    def test_failed_preset_write_keeps_live_preset(self):
        original = copy.deepcopy(server.presets)
        disk = (server.PRESETS_DIR / "test.json").read_bytes()
        with patch.object(server, "step_status", return_value={"owned": False, "terminal_open": False}), \
             patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, _ = self.request("/api/presets", {"id": "test", "name": "Changed", "steps": [{"command": "echo new"}]})
        self.assertEqual(status, 500)
        self.assertEqual(server.presets, original)
        self.assertEqual((server.PRESETS_DIR / "test.json").read_bytes(), disk)

    def test_failed_workspace_deletion_returns_json_and_keeps_panel(self):
        with patch.object(server, "step_status", return_value={"owned": False, "terminal_open": False}), \
             patch.object(server, "atomic_json", side_effect=OSError("disk full")):
            status, body = self.request("/api/workspaces/panel-test", method="DELETE")
        self.assertEqual(status, 500)
        self.assertIn("error", body)
        self.assert_workspace_unchanged()

    def test_failed_preset_deletion_keeps_preset(self):
        unused = server.normalize_preset({"name": "Unused", "steps": [{"command": "echo ok"}]}, "unused")
        server.presets["unused"] = unused
        server.atomic_json(server.PRESETS_DIR / "unused.json", unused)
        with patch.object(Path, "unlink", side_effect=OSError("read only")):
            status, body = self.request("/api/presets/unused", method="DELETE")
        self.assertEqual(status, 500)
        self.assertIn("error", body)
        self.assertEqual(server.presets["unused"], unused)
        self.assertTrue((server.PRESETS_DIR / "unused.json").exists())

    def test_atomic_write_failure_keeps_previous_file_and_removes_temp(self):
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                server.atomic_json(server.WORKSPACES_FILE, {"changed": True})
        self.assertEqual(server.WORKSPACES_FILE.read_bytes(), self.before_disk)
        self.assertEqual(list(server.CONFIG_DIR.glob("*.tmp")), [])

    def test_negative_content_length_returns_error_without_hanging(self):
        status, _ = self.request("/api/settings", {}, headers={"Content-Type": "application/json", "Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_orphan_workspace_does_not_prevent_loading_valid_panels(self):
        orphan = {"id": "orphan", "preset_id": "missing", "steps": {}}
        server.atomic_json(server.WORKSPACES_FILE, {"workspaces": [self.before_workspace, orphan]})
        disk = server.WORKSPACES_FILE.read_bytes()
        server.load_all()
        self.assertEqual([item["id"] for item in server.workspaces], ["panel-test"])
        self.assertEqual(server.WORKSPACES_FILE.read_bytes(), disk)


if __name__ == "__main__":
    unittest.main()
