import { test } from "node:test";
import assert from "node:assert/strict";
import {
  copyName,
  importPreset,
  isActive,
  matchesWorkspace,
  statusLabel,
  variablesFromRows,
} from "../static/ui-model.js";

const targets = [{ id: "local" }];
const preset = {
  name: "Backup",
  steps: [{ title: "Archive", command: "tar project", target_id: "local" }],
};
test("pending launches and open terminals are active; external processes are not owned", () => {
  assert.equal(isActive({ status: "starting" }), true);
  assert.equal(isActive({ terminal_open: true }), true);
  assert.equal(isActive({ external: true }), false);
  assert.equal(statusLabel({ status: "failed", return_code: 1 }), "Ошибка");
  assert.equal(statusLabel({ return_code: 0, status: "stopped" }), "Выполнено");
});
test("search matches all terms across names and commands with case ignored", () => {
  const workspace = {
    name: "My PROJECT",
    preset,
    steps: { a: { status: "failed" } },
  };
  assert.equal(matchesWorkspace(workspace, "project TAR", "failed"), true);
  assert.equal(matchesWorkspace(workspace, "project", "active"), false);
  assert.equal(matchesWorkspace(workspace, "missing"), false);
});
test("variables retain literal values and reject duplicates, reserved and malformed names", () => {
  assert.equal(
    variablesFromRows([{ key: "message", value: '{{other}}; "hi"' }]).message,
    '{{other}}; "hi"',
  );
  for (const rows of [
    [
      { key: "message", value: "1" },
      { key: " message ", value: "2" },
    ],
    [{ key: "project_path", value: "/wrong" }],
    [{ key: "1name", value: "x" }],
    [{ key: "", value: "x" }],
  ])
    assert.throws(() => variablesFromRows(rows));
  assert.equal(
    variablesFromRows([{ key: "__proto__", value: "safe" }]).__proto__,
    "safe",
  );
});
test("import always creates a new draft, preserving legacy execution modes", () => {
  const imported = importPreset(
    {
      ...preset,
      id: "existing",
      steps: [{ command: "echo ok", execution: "host_background" }],
    },
    targets,
    () => "new-step",
  );
  assert.equal(imported.id, "");
  assert.equal(imported.steps[0].terminal, false);
  assert.equal(imported.steps[0].id, "new-step");
  assert.throws(() =>
    importPreset({ ...preset, steps: [{ command: "" }] }, targets, () => "id"),
  );
  assert.throws(() =>
    importPreset(
      { ...preset, steps: [{ command: "echo", target_id: "missing" }] },
      targets,
      () => "id",
    ),
  );
  assert.throws(() =>
    importPreset(
      { ...preset, steps: [{ command: "echo", terminal: "false" }] },
      targets,
      () => "id",
    ),
  );
});
test("copies get distinct human-readable names", () => {
  assert.equal(
    copyName("Backup", ["Backup — копия", "Backup — копия 2"]),
    "Backup — копия 3",
  );
});
