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
  if (step.status === "failed") return "Ошибка";
  if (step.status === "starting") return "Запуск";
  if (step.owned) return "Работает";
  if (step.terminal_open) return "Терминал открыт";
  if (step.external) return "Внешний процесс";
  if (step.return_code === 0) return "Выполнено";
  if (step.status === "stopped") return "Остановлено";
  return "Готово";
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
  if (!preset.name?.trim()) return "Введите название сценария";
  if (!Array.isArray(preset.steps) || !preset.steps.length)
    return "Добавьте хотя бы одну команду";
  for (const [index, step] of preset.steps.entries()) {
    if (!step.command?.trim()) return `Введите команду ${index + 1}`;
    if (!targets.some((target) => target.id === step.target_id))
      return `Выберите существующую цель для команды ${index + 1}`;
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
        "Имя переменной: латинские буквы, цифры, _, . или -. Начните с буквы или _.",
      );
    if (reservedVariables.has(key))
      throw new Error(`${key} — встроенная переменная пути`);
    if (Object.hasOwn(variables, key))
      throw new Error(`Переменная ${key} указана дважды`);
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
    throw new Error("JSON должен содержать сценарий со списком steps");
  const preset = {
    id: "",
    name: typeof raw.name === "string" ? raw.name : "",
    description: typeof raw.description === "string" ? raw.description : "",
    workdir: String(raw.workdir || raw.project_path || ""),
    steps: raw.steps.map((step, index) => {
      if (!step || typeof step.command !== "string")
        throw new Error(`Некорректная команда ${index + 1}`);
      if (
        ["terminal", "selected"].some(
          (key) => key in step && typeof step[key] !== "boolean",
        )
      )
        throw new Error(
          `Режим и выбор команды ${index + 1} должны быть true или false`,
        );
      const execution = String(step.execution || "");
      return {
        id: makeId(),
        title: String(step.title || `Команда ${index + 1}`),
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
  let result = `${name} — копия`,
    number = 2;
  while (names.includes(result)) result = `${name} — копия ${number++}`;
  return result;
}
