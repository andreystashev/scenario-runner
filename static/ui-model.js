import { t } from "./i18n.js";
/** Pure UI rules shared by the editor, dashboard and regression tests. */
export const reservedVariables = new Set([
  "project_path",
  "bag_path",
  "bag_folder",
]);
export const variableNamePattern = /^[A-Za-z_][A-Za-z0-9_.-]*$/;

export function isActive(step = {}) {
  return Boolean(
    step.owned || step.terminal_open || step.status === "starting",
  );
}

export function statusLabel(step = {}) {
  if (step.status === "failed") return t("Failed");
  if (step.status === "starting") return t("Starting");
  if (step.owned) return t("Running");
  if (step.terminal_open) return t("Terminal open");
  if (step.external) return t("External process");
  if (step.return_code === 0) return t("Completed");
  if (step.status === "stopped") return t("Stopped");
  return t("Ready");
}

export function matchesWorkspace(workspace, query, filter = "all") {
  const states = Object.values(workspace.steps || {});
  if (filter === "active" && !states.some(isActive)) return false;
  if (filter === "failed" && !states.some((step) => step.status === "failed"))
    return false;
  const text = [
    workspace.name,
    workspace.bag_path,
    workspace.preset.name,
    workspace.preset.description,
    ...workspace.preset.steps.flatMap((step) => [
      step.title,
      step.command,
      step.target_id,
    ]),
  ]
    .join(" ")
    .toLocaleLowerCase();
  return query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .every((word) => text.includes(word));
}

export function validatePreset(preset, targets) {
  if (!preset.name?.trim()) return t("Enter a scenario name");
  if (!Array.isArray(preset.steps) || !preset.steps.length)
    return t("Add at least one command");
  for (const [index, step] of preset.steps.entries()) {
    if (!step.command?.trim())
      return t("Enter command {number}", { number: index + 1 });
    if (!targets.some((target) => target.id === step.target_id))
      return t("Choose an available environment for command {number}", {
        number: index + 1,
      });
  }
  return "";
}

export function variablesFromRows(rows) {
  const variables = Object.create(null);
  for (const row of rows) {
    const key = row.key.trim();
    if (!key && !row.value.trim()) continue;
    if (!variableNamePattern.test(key))
      throw new Error(
        t(
          "Variable names must start with a letter or _. Use Latin letters, digits, _, . or -.",
        ),
      );
    if (reservedVariables.has(key))
      throw new Error(t("{name} is a built-in path variable", { name: key }));
    if (Object.hasOwn(variables, key))
      throw new Error(t("Variable {name} is duplicated", { name: key }));
    variables[key] = row.value;
  }
  return variables;
}

export function importPreset(raw, targets, makeId) {
  if (
    !raw ||
    typeof raw !== "object" ||
    Array.isArray(raw) ||
    !Array.isArray(raw.steps)
  )
    throw new Error(
      t("The JSON file must contain a scenario with a steps array"),
    );
  const preset = {
    id: "",
    name: typeof raw.name === "string" ? raw.name : "",
    description: typeof raw.description === "string" ? raw.description : "",
    workdir: String(raw.workdir || raw.project_path || ""),
    steps: raw.steps.map((step, index) => {
      if (!step || typeof step.command !== "string")
        throw new Error(t("Invalid command {number}", { number: index + 1 }));
      if (
        ["terminal", "selected"].some(
          (key) => key in step && typeof step[key] !== "boolean",
        )
      )
        throw new Error(
          t(
            "Run mode and selection for command {number} must be true or false",
            { number: index + 1 },
          ),
        );
      const execution = String(step.execution || "");
      return {
        id: makeId(),
        title: String(
          step.title || t("Command {number}", { number: index + 1 }),
        ),
        command: step.command,
        target_id:
          step.target_id ||
          (execution.startsWith("container_") ? "docker" : "local"),
        terminal:
          typeof step.terminal === "boolean"
            ? step.terminal
            : execution
              ? execution.endsWith("_terminal")
              : true,
        selected: step.selected !== false,
        process_match: String(step.process_match || ""),
      };
    }),
  };
  const error = validatePreset(preset, targets);
  if (error) throw new Error(error);
  return preset;
}

export function copyName(name, names) {
  let result = t("{name} — copy", { name: name }),
    number = 2;
  while (names.includes(result))
    result = t("{name} — copy {number}", { name: name, number: number++ });
  return result;
}
