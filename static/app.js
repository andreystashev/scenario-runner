import {
  palettes,
  appearanceDefaults,
  colorKeys,
  normalizeAppearance,
  panelAppearance,
  colorTokens,
  contrast,
  isColor,
} from "./appearance.js";
import {
  t,
  getLanguage,
  setLanguage,
  translateDocument,
  serverText,
} from "./i18n.js";
import { request as api, workspacePath } from "./api.js";
import {
  copyName,
  importPreset,
  isActive,
  matchesWorkspace,
  statusLabel,
  validatePreset,
  variablesFromRows,
} from "./ui-model.js";
const $ = (selector) => document.querySelector(selector);
const el = (tag, className = "", text = "") => {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== "") node.textContent = text;
  return node;
};
const createId = (prefix) => `${prefix}-${crypto.randomUUID()}`;
let state = null,
  mutation = 0,
  pollBusy = false,
  connected = true,
  pendingMutations = 0;
let mutationQueue = Promise.resolve(),
  renderedPresets = "";
let draft = null,
  savedDraft = "",
  settingsDraft = null,
  variablesWorkspaceId = null,
  variablesDraft = [];
let activeOverlay = null,
  returnFocus = null,
  appearanceBeforeSettings = null;
let searchQuery = "",
  statusFilter = "all",
  toastTimer = null,
  lastInteractionAt = 0;
const detailState = new Map();
let appearance = loadAppearance();
let panelAppearanceDraft = {};
function loadAppearance() {
  try {
    return normalizeAppearance(
      JSON.parse(localStorage.getItem("scenario-runner-appearance-v1") || "{}"),
    );
  } catch {
    return normalizeAppearance(null);
  }
}
function applyColors(node, values) {
  for (const [key, value] of Object.entries(colorTokens(values)))
    node.style.setProperty(key, value);
  node.dataset.layout = values.layout;
}
function fontStack() {
  if (appearance.fontFamily === "mono")
    return '"Ubuntu Mono", "IBM Plex Mono", monospace';
  if (appearance.fontFamily === "serif")
    return 'Georgia, "Times New Roman", serif';
  if (appearance.fontFamily === "custom" && appearance.customFont.trim())
    return `"${appearance.customFont.replaceAll('"', "")}", sans-serif`;
  return 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
}
function applyAppearance() {
  const style = document.documentElement.style;
  style.setProperty("--font-scale", appearance.fontScale);
  style.setProperty("--panel-opacity", appearance.panelOpacity);
  style.setProperty("--app-font", fontStack());
  applyColors(document.documentElement, appearance);
  document.documentElement.style.colorScheme = appearance.theme;
  document.body.dataset.theme = appearance.theme;
  document.body.dataset.density = appearance.density;
  document.body.dataset.glass = appearance.glass ? "on" : "off";
  document.body.dataset.motion = appearance.motion ? "on" : "off";
  document.querySelectorAll(".workspace[data-id]").forEach((card) => {
    const workspace = state?.workspaces.find(
      (item) => item.id === card.dataset.id,
    );
    if (workspace)
      applyColors(card, panelAppearance(appearance, workspace.appearance));
  });
}
function toast(message, error = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.className = "toast"), error ? 7000 : 3500);
}

function button(text, className, action, label = text) {
  const node = el("button", className, text);
  node.type = "button";
  node.setAttribute("aria-label", label);
  node.onclick = () => withBusy(node, action);
  return node;
}

async function withBusy(control, action) {
  if (control.disabled || control.dataset.busy === "true") return;
  const text = control.textContent;
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  control.dataset.busy = "true";
  try {
    await action();
  } catch (error) {
    toast(serverText(error.message), true);
  } finally {
    if (control.isConnected) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
      delete control.dataset.busy;
      control.textContent = control.dataset.i18n
        ? t(control.dataset.i18n)
        : text;
      if (control.id === "savePreset") feedback();
    }
  }
}

function bindAction(id, action) {
  const control = $(id);
  control.onclick = () => withBusy(control, action);
}

function editingNow() {
  return Boolean(
    activeOverlay ||
      Date.now() - lastInteractionAt < 600 ||
      window.getSelection()?.isCollapsed === false ||
      document.activeElement?.matches(
        "input:not(#searchPanels), select, textarea",
      ),
  );
}

async function refresh(silent = true) {
  if (pollBusy || pendingMutations) return false;
  pollBusy = true;
  const epoch = mutation;
  try {
    const next = await api("/api/state");
    if (epoch !== mutation) return false;
    state = next;
    connected = true;
    if (!silent || !editingNow()) render();
    else renderHeader();
    return true;
  } catch (error) {
    connected = false;
    if (!editingNow()) render();
    else renderHeader();
    if (!silent) toast(serverText(error.message), true);
    return false;
  } finally {
    pollBusy = false;
  }
}

function mutate(path, method = "POST", body = {}) {
  mutation += 1;
  pendingMutations += 1;
  const operation = mutationQueue
    .catch(() => {})
    .then(async () => {
      state = await api(path, { method, body: JSON.stringify(body) });
      connected = true;
      render();
      return state;
    });
  mutationQueue = operation;
  return operation.finally(() => (pendingMutations -= 1));
}

function targetById(id) {
  return state?.settings.targets.find((target) => target.id === id);
}
function optionList(select, active = "") {
  select.replaceChildren(
    ...state.presets.map((preset) => {
      const option = el("option", "", preset.name);
      option.value = preset.id;
      option.selected = preset.id === active;
      return option;
    }),
  );
}

function renderHeader() {
  const node = $("#containerStatus");
  $("#connectionBanner").classList.toggle("hidden", connected);
  if (!state) {
    node.textContent = connected ? t("Connecting…") : t("Offline");
    return;
  }
  $("#version").textContent = `v${state.version}`;
  const targets = state.targets || [],
    ready = targets.filter((target) => target.running).length;
  node.textContent = connected
    ? t("{ready}/{total} environments available", {
        ready: ready,
        total: targets.length,
      })
    : t("Server disconnected");
  node.className = `container-status${connected && ready === targets.length ? " running" : ""}`;
  node.title = targets
    .map((target) => `${target.running ? "●" : "○"} ${target.name}`)
    .join("\n");
  const statuses = state.workspaces.flatMap((workspace) =>
    Object.values(workspace.steps),
  );
  $("#panelCount").textContent = state.workspaces.length;
  $("#activeCount").textContent = statuses.filter(isActive).length;
  $("#failedCount").textContent = statuses.filter(
    (step) => step.status === "failed",
  ).length;
}

function render() {
  renderHeader();
  if (!state) return;
  const presetsKey = JSON.stringify(state.presets);
  if (presetsKey !== renderedPresets) {
    optionList($("#newPreset"), $("#newPreset").value || state.presets[0]?.id);
    renderedPresets = presetsKey;
  }
  $("#createButton").disabled = !state.presets.length || !connected;
  const visible = state.workspaces.filter((workspace) =>
    matchesWorkspace(workspace, searchQuery, statusFilter),
  );
  const list = $("#workspaceList");
  const current = new Map(
    [...list.children].map((card) => [card.dataset.id, card]),
  );
  const visibleIds = new Set(visible.map((workspace) => workspace.id));
  for (const card of current.values())
    if (!visibleIds.has(card.dataset.id)) card.remove();
  for (const [index, workspace] of visible.entries()) {
    const previous = current.get(workspace.id);
    const signature = JSON.stringify([
      workspace,
      state.settings.targets,
      connected,
      getLanguage(),
    ]);
    if (previous?.dataset.signature === signature) {
      if (list.children[index] !== previous)
        list.insertBefore(previous, list.children[index] || null);
      continue;
    }
    const next = renderWorkspace(workspace);
    next.dataset.id = workspace.id;
    next.dataset.signature = signature;
    if (previous?.isConnected) previous.replaceWith(next);
    if (list.children[index] !== next)
      list.insertBefore(next, list.children[index] || null);
  }
  $("#emptyState").classList.toggle("hidden", state.workspaces.length > 0);
  $("#filterEmpty").classList.toggle(
    "hidden",
    !state.workspaces.length || visible.length > 0,
  );
  $("#filterCount").textContent = t("{shown} of {total}", {
    shown: visible.length,
    total: state.workspaces.length,
  });
  $("#emptyTitle").textContent = state.presets.length
    ? t("No panels yet")
    : t("No scenarios yet");
  $("#emptyHint").textContent = state.presets.length
    ? t(
        "Add a panel and choose a scenario. Each panel has its own path and variables.",
      )
    : t("Create a scenario with commands, then add a panel to run it.");
  $("#emptyAction").textContent = state.presets.length
    ? t("＋ Add panel")
    : t("＋ Create scenario");
  document.querySelectorAll("[data-filter]").forEach((control) => {
    const selected = control.dataset.filter === statusFilter;
    control.classList.toggle("active", selected);
    control.setAttribute("aria-pressed", selected);
  });
}

function renderWorkspace(workspace) {
  const path = (suffix) => workspacePath(workspace.id, suffix);
  const specs = workspace.preset.steps;
  const selected = specs.filter(
    (spec) => workspace.steps[spec.id]?.selected !== false,
  );
  const active = specs.filter((spec) => isActive(workspace.steps[spec.id]));
  const runnable = selected.filter(
    (spec) =>
      !isActive(workspace.steps[spec.id]) &&
      !workspace.steps[spec.id]?.external,
  );
  const card = el("article", "workspace");
  applyColors(card, panelAppearance(appearance, workspace.appearance));
  card.dataset.customAppearance = Object.keys(workspace.appearance || {}).length
    ? "true"
    : "false";
  const head = el("div", "workspace-head");
  const title = el("div", "workspace-title");
  const eyebrow = el("div", "workspace-eyebrow", workspace.preset.name);
  title.append(eyebrow, el("h2", "", workspace.name));
  const meta = el("div", "workspace-meta");
  if (workspace.preset.description)
    meta.append(el("span", "", workspace.preset.description));
  if (workspace.bag_path) meta.append(el("code", "", workspace.bag_path));
  title.append(meta);
  const controls = el("div", "workspace-controls");
  const edit = button(t("Edit scenario"), "button secondary small", () =>
    openDrawer(workspace.preset),
  );
  const parameters = button(t("Parameters"), "button secondary small", () =>
    openVariables(workspace),
  );
  const remove = button(
    "×",
    "icon-button",
    async () => {
      if (
        confirm(
          t("Delete panel “{name}”? The scenario will be kept.", {
            name: workspace.name,
          }),
        )
      )
        await mutate(path(""), "DELETE");
    },
    t("Delete panel {name}", { name: workspace.name }),
  );
  remove.disabled = active.length > 0;
  remove.title = active.length
    ? t("Stop this panel’s commands first")
    : t("Delete panel");
  controls.append(edit, parameters, remove);
  head.append(title, controls);
  card.append(head);
  const bar = el("div", "workspace-bar");
  bar.append(
    el(
      "span",
      "selection-label",
      t("Selected {selected} of {total}", {
        selected: selected.length,
        total: specs.length,
      }),
    ),
  );
  const choose = button(
    selected.length === specs.length ? t("Deselect all") : t("Select all"),
    "text-button",
    () =>
      mutate(path("/selection"), "POST", {
        selected: selected.length !== specs.length,
      }),
  );
  const actions = el("div", "workspace-run-actions");
  const run = button(t("▶ Run selected"), "button primary", () =>
    mutate(path("/run")),
  );
  run.disabled = !runnable.length || !connected;
  run.title = t(
    "Commands start in order, without waiting for the previous command to finish",
  );
  const stop = button(t("■ Stop commands"), "button danger", () =>
    mutate(path("/stop")),
  );
  stop.disabled = !active.length || !connected;
  stop.title = t("Stops only commands started by this panel");
  actions.append(run, stop);
  bar.append(choose, actions);
  card.append(bar);
  const steps = el("div", "steps");
  specs.forEach((spec, index) =>
    steps.append(
      renderStep(workspace, spec, workspace.steps[spec.id] || {}, index),
    ),
  );
  card.append(steps);
  return card;
}

function disclosure(key, label, content, open = false) {
  const details = el("details", "disclosure");
  details.open = detailState.has(key) ? detailState.get(key) : open;
  details.append(el("summary", "", label), content);
  details.ontoggle = () => detailState.set(key, details.open);
  return details;
}

function renderStep(workspace, spec, status, index) {
  const key = `${workspace.id}/${spec.id}`;
  const path = (suffix) =>
    workspacePath(
      workspace.id,
      `/steps/${encodeURIComponent(spec.id)}${suffix}`,
    );
  const card = el("div", `step ${status.status || "idle"}`);
  const head = el("div", "step-head");
  const title = el("label", "step-title");
  const check = el("input", "check");
  check.type = "checkbox";
  check.checked = status.selected !== false;
  check.onchange = () =>
    withBusy(check, async () => {
      try {
        await mutate(path("/select"), "POST", { selected: check.checked });
      } catch (error) {
        check.checked = !check.checked;
        throw error;
      }
    });
  title.append(check, el("span", "", spec.title));
  const badge = el(
    "span",
    `status-badge ${status.status || "idle"}`,
    statusLabel(status),
  );
  head.append(title, badge);
  card.append(head);
  const target = targetById(spec.target_id);
  card.append(
    el(
      "div",
      "mode",
      `${String(index + 1).padStart(2, "0")} · ${target?.name || t("Environment unavailable")} · ${spec.terminal ? t("Terminal") : t("Background")}`,
    ),
  );
  card.append(
    el("p", "status-text", serverText(status.message) || t("Ready to run")),
  );
  const code = el("pre", "command-code", spec.command);
  card.append(disclosure(`${key}/command`, t("Command"), code));
  if (status.log_tail) {
    const output = el("div", "output-content");
    output.append(el("pre", "log", status.log_tail));
    output.append(
      button(t("Copy output"), "text-button", async () => {
        await navigator.clipboard.writeText(status.log_tail);
        toast(t("Output copied"));
      }),
    );
    card.append(
      disclosure(`${key}/log`, t("Output"), output, status.status === "failed"),
    );
  }
  let label = t("▶ Run"),
    endpoint = "run",
    disabled = !connected;
  if (status.status === "starting") {
    label = t("Starting…");
    disabled = true;
  } else if (status.external) {
    label = t("External process");
    disabled = true;
  } else if (status.owned || status.terminal_open) {
    label = status.owned ? t("■ Stop") : t("× Close terminal");
    endpoint = "stop";
  } else if (status.status === "failed") label = t("↻ Retry");
  const control = button(
    label,
    `button step-action ${endpoint === "stop" ? "danger" : "secondary"}`,
    () => mutate(path(`/${endpoint}`)),
  );
  control.disabled = disabled;
  card.append(control);
  return card;
}

function showOverlay(selector) {
  if (activeOverlay) return;
  returnFocus = document.activeElement;
  activeOverlay = $(selector);
  activeOverlay.classList.remove("hidden");
  activeOverlay.setAttribute("aria-hidden", "false");
  activeOverlay.inert = false;
  document.querySelector("main").inert = true;
  document.querySelector(".topbar").inert = true;
  document.body.style.overflow = "hidden";
  activeOverlay.querySelector("input, textarea, button")?.focus();
}

function hideOverlay() {
  if (!activeOverlay) return;
  activeOverlay.classList.add("hidden");
  activeOverlay.classList.remove("open");
  activeOverlay.setAttribute("aria-hidden", "true");
  activeOverlay.inert = true;
  activeOverlay = null;
  document.querySelector("main").inert = false;
  document.querySelector(".topbar").inert = false;
  document.body.style.overflow = "";
  $("#drawerBackdrop").classList.add("hidden");
  if (returnFocus?.isConnected) returnFocus.focus();
  render();
}
function newStep(index = 1) {
  return {
    id: createId("command"),
    title: t("Command {number}", { number: index }),
    command: "",
    target_id:
      state.settings.targets.find((target) => target.type === "local")?.id ||
      "local",
    terminal: true,
    selected: true,
    process_match: "",
  };
}
function newDraft() {
  return { id: "", name: "", description: "", workdir: "", steps: [newStep()] };
}
function isDirty() {
  return draft && JSON.stringify(draft) !== savedDraft;
}
function allowDiscard() {
  return !isDirty() || confirm(t("Discard unsaved changes to this scenario?"));
}
function choosePreset(preset) {
  if (!allowDiscard()) return;
  draft = structuredClone(preset);
  savedDraft = JSON.stringify(draft);
  fillPreset();
}
function fillPreset() {
  $("#presetName").value = draft.name;
  $("#presetDescription").value = draft.description;
  $("#presetProject").value = draft.workdir;
  renderPresetTabs();
  renderStepEditor();
  feedback();
  $("#deletePreset").classList.toggle("hidden", !draft.id);
}
function syncDraftFields() {
  draft.name = $("#presetName").value;
  draft.description = $("#presetDescription").value;
  draft.workdir = $("#presetProject").value;
  feedback();
}
function feedback(message = "", kind = "") {
  if (!draft || !state) return;
  const node = $("#presetFeedback");
  const validation = validatePreset(draft, state.settings.targets);
  node.textContent =
    message || validation || (isDirty() ? t("Unsaved changes") : t("Saved"));
  node.className = `feedback ${kind || (!isDirty() && !validation ? "ok" : "")}`;
  $("#savePreset").disabled =
    !isDirty() ||
    Boolean(validation) ||
    $("#savePreset").dataset.busy === "true";
}
function renderPresetTabs() {
  const tabs = state.presets.map((preset) => {
    const tab = button(
      preset.name,
      `button secondary small preset-tab${draft.id === preset.id ? " active" : ""}`,
      () => choosePreset(preset),
    );
    tab.setAttribute("aria-pressed", preset.id === draft.id);
    return tab;
  });
  tabs.push(
    button(t("＋ New scenario"), "button secondary small", () => {
      if (!allowDiscard()) return;
      draft = newDraft();
      savedDraft = JSON.stringify(draft);
      fillPreset();
      $("#presetName").focus();
    }),
  );
  $("#presetTabs").replaceChildren(...tabs);
}
function selectField(label, values, current, change) {
  const field = el("div", "field");
  const text = el("label", "", label);
  const select = el("select");
  select.id = createId("field");
  text.htmlFor = select.id;
  for (const [value, name] of values) {
    const option = el("option", "", name);
    option.value = value;
    option.selected = String(current) === String(value);
    select.append(option);
  }
  select.onchange = () => change(select.value);
  field.append(text, select);
  return field;
}
function moveStep(index, offset) {
  const destination = index + offset;
  if (destination < 0 || destination >= draft.steps.length) return;
  [draft.steps[index], draft.steps[destination]] = [
    draft.steps[destination],
    draft.steps[index],
  ];
  renderStepEditor();
  feedback();
}
function renderStepEditor() {
  $("#stepEditor").replaceChildren(
    ...draft.steps.map((step, index) => {
      const card = el("div", "edit-step");
      const head = el("div", "edit-step-head");
      const title = el("input");
      title.value = step.title;
      title.placeholder = t("Command name");
      title.setAttribute(
        "aria-label",
        t("Command {number} name", { number: index + 1 }),
      );
      title.oninput = () => {
        step.title = title.value;
        feedback();
      };
      const tools = el("div", "step-editor-tools");
      const up = button(
        "↑",
        "icon-button small",
        () => moveStep(index, -1),
        t("Move command up"),
      );
      const down = button(
        "↓",
        "icon-button small",
        () => moveStep(index, 1),
        t("Move command down"),
      );
      up.disabled = index === 0;
      down.disabled = index === draft.steps.length - 1;
      tools.append(
        up,
        down,
        button(
          "⧉",
          "icon-button small",
          () => {
            draft.steps.splice(index + 1, 0, {
              ...structuredClone(step),
              id: createId("command"),
              title: t("{name} — copy", { name: step.title }),
            });
            renderStepEditor();
            feedback();
          },
          t("Duplicate command"),
        ),
        button(
          "×",
          "icon-button small",
          () => {
            draft.steps.splice(index, 1);
            renderStepEditor();
            feedback();
          },
          t("Delete command"),
        ),
      );
      head.append(el("span", "step-number", String(index + 1)), title, tools);
      const command = el("textarea");
      command.value = step.command;
      command.placeholder = t("e.g. python3 app.py --port {{port}}");
      command.spellcheck = false;
      command.setAttribute(
        "aria-label",
        t("Command {number} text", { number: index + 1 }),
      );
      command.oninput = () => {
        step.command = command.value;
        feedback();
      };
      const grid = el("div", "edit-step-grid");
      grid.append(
        selectField(
          t("Run environment"),
          state.settings.targets.map((target) => [
            target.id,
            `${target.name} · ${target.type === "docker" ? "Docker" : t("local")}`,
          ]),
          step.target_id,
          (value) => {
            step.target_id = value;
            feedback();
          },
        ),
      );
      grid.append(
        selectField(
          t("Run mode"),
          [
            ["true", t("Terminal · interactive")],
            ["false", t("Background · no input")],
          ],
          step.terminal,
          (value) => {
            step.terminal = value === "true";
            feedback();
          },
        ),
      );
      const advanced = el("div");
      advanced.append(
        fieldInput(
          t("External process pattern · regex (Docker)"),
          step.process_match,
          (value) => {
            step.process_match = value;
            feedback();
          },
          t("Optional"),
        ),
      );
      card.append(
        head,
        command,
        grid,
        disclosure(
          `editor/${step.id}/advanced`,
          t("Advanced"),
          advanced,
          Boolean(step.process_match),
        ),
      );
      return card;
    }),
  );
}
function openDrawer(preset = null) {
  if (!state || activeOverlay) return;
  draft = structuredClone(preset || state.presets[0] || newDraft());
  savedDraft = JSON.stringify(draft);
  fillPreset();
  showOverlay("#presetDrawer");
  $("#presetDrawer").classList.add("open");
  $("#drawerBackdrop").classList.remove("hidden");
  $("#presetName").focus();
}
function closeDrawer() {
  if (allowDiscard()) hideOverlay();
}
async function savePreset() {
  syncDraftFields();
  const error = validatePreset(draft, state.settings.targets);
  if (error) throw new Error(error);
  const response = await mutate("/api/presets", "POST", draft);
  const saved =
    response.presets.find((item) => item.id === draft.id) ||
    response.presets.find((item) => item.name === draft.name);
  draft = structuredClone(saved);
  savedDraft = JSON.stringify(draft);
  fillPreset();
  toast(t("Scenario saved"));
}
const colorLabels = {
  accent: ["Accent", "Buttons, selection and focus"],
  background: ["Page background", "The area behind panels"],
  surface: ["Panel background", "Panels, dialogs and command cards"],
  text: ["Text", "Headings, commands and output"],
  info: ["Information", "Paths and run environments"],
  success: ["Running status", "Active command badges and borders"],
  warning: ["Warning", "Starting and external processes"],
  danger: ["Error", "Failed commands and stop actions"],
};
function colorControl(key, values, changed, inherited = false) {
  const field = el("div", "color-field");
  const id = createId("color");
  const label = el("label", "", t(colorLabels[key][0]));
  label.htmlFor = id;
  const hint = el(
    "small",
    "",
    t(
      inherited && key === "surface"
        ? "This panel and its command cards"
        : colorLabels[key][1],
    ),
  );
  const row = el("div", "color-input-row");
  const swatch = el("input", "color-swatch");
  swatch.type = "color";
  swatch.id = id;
  const hex = el("input", "color-hex");
  hex.setAttribute(
    "aria-label",
    t("{name} · HEX", { name: t(colorLabels[key][0]) }),
  );
  hex.maxLength = 7;
  hex.spellcheck = false;
  const value = values[key] || appearance[key];
  swatch.value = value;
  hex.value = value.toUpperCase();
  const update = (value) => {
    if (!isColor(value)) {
      hex.setAttribute("aria-invalid", "true");
      return;
    }
    hex.removeAttribute("aria-invalid");
    values[key] = value;
    swatch.value = value;
    hex.value = value.toUpperCase();
    changed();
  };
  swatch.oninput = () => update(swatch.value);
  hex.oninput = () => update(hex.value);
  hex.onblur = () => {
    hex.value = swatch.value.toUpperCase();
    hex.removeAttribute("aria-invalid");
  };
  row.append(swatch, hex);
  field.append(label, hint, row);
  if (inherited) {
    const toggle = el("input");
    toggle.type = "checkbox";
    toggle.checked = Boolean(values[key]);
    const inherit = el("label", "check-row");
    inherit.append(toggle, el("span", "", t("Custom color")));
    swatch.disabled = hex.disabled = !toggle.checked;
    toggle.onchange = () => {
      if (toggle.checked) values[key] = swatch.value;
      else delete values[key];
      swatch.disabled = hex.disabled = !toggle.checked;
      changed();
    };
    field.append(inherit);
  }
  return field;
}
function renderColorControls() {
  $("#appearanceColors").replaceChildren(
    ...colorKeys.map((key) =>
      colorControl(key, appearance, updateAppearancePreview),
    ),
  );
}
function preview(container, values, title) {
  applyColors(container, values);
  container.style.setProperty("--panel-opacity", values.panelOpacity);
  container.style.setProperty("--font-scale", values.fontScale);
  container.style.setProperty("--app-font", fontStack());
  container.style.fontSize = `${14 * values.fontScale}px`;
  container.dataset.density = values.density;
  container.replaceChildren();
  const head = el("div", "preview-head");
  head.append(
    el("strong", "", title),
    el("span", "status-badge running", t("Running")),
  );
  const actions = el("div", "preview-actions");
  actions.append(
    el("span", "button primary", t("▶ Run")),
    el("span", "button danger", t("■ Stop")),
  );
  const statuses = el("div", "preview-statuses");
  statuses.append(
    el("span", "mode", t("Information")),
    el("span", "status-badge starting", t("Starting")),
    el("span", "status-badge failed", t("Failed")),
  );
  const output = el("pre", "log", "$ python3 app.py\nReady on localhost:8000");
  const panel = el("div", "preview-panel");
  panel.style.padding = values.density === "compact" ? "12px" : "18px";
  panel.append(
    head,
    el("p", "status-text", t("Background command · output below")),
    actions,
    statuses,
    output,
  );
  container.dataset.glass = values.glass ? "on" : "off";
  container.append(panel);
}
function updateAppearancePreview() {
  updateAppearanceLabels();
  preview($("#appearancePreview"), appearance, t("Theme preview"));
  $("#appearanceContrast").textContent =
    contrast(appearance.text, appearance.surface) < 4.5 ||
    contrast(appearance.text, appearance.background) < 4.5
      ? t("Low text contrast. Choose a lighter or darker text color.")
      : "";
  $("#appearanceCustomFont").disabled = appearance.fontFamily !== "custom";
}
function populateAppearance() {
  renderColorControls();
  for (const [id, key] of Object.entries({
    appearanceFont: "fontFamily",
    appearanceCustomFont: "customFont",
    appearanceFontScale: "fontScale",
    appearanceOpacity: "panelOpacity",
    appearanceLayout: "layout",
  }))
    $("#" + id).value = appearance[key];
  $("#appearanceGlass").checked = appearance.glass;
  $("#appearanceMotion").checked = appearance.motion;
  updateAppearancePreview();
}
function updateAppearanceLabels() {
  $("#fontScaleValue").textContent =
    `${Math.round(appearance.fontScale * 100)}%`;
  $("#opacityValue").textContent =
    `${Math.round(appearance.panelOpacity * 100)}%`;
  for (const [attribute, key] of [
    ["theme", "theme"],
    ["density", "density"],
  ])
    document
      .querySelectorAll(`[data-${attribute}-choice]`)
      .forEach((control) => {
        const selected =
          control.dataset[attribute + "Choice"] === appearance[key];
        control.classList.toggle("active", selected);
        control.setAttribute("aria-pressed", selected);
      });
}
function showSettingsSection(section) {
  document
    .querySelectorAll("[data-settings-section]")
    .forEach((node) =>
      node.classList.toggle("hidden", node.dataset.settingsSection !== section),
    );
  document.querySelectorAll("[data-settings-choice]").forEach((control) => {
    const selected = control.dataset.settingsChoice === section;
    control.classList.toggle("active", selected);
    control.setAttribute("aria-pressed", selected);
  });
}
function openSettings() {
  if (!state || activeOverlay) return;
  settingsDraft = structuredClone(state.settings);
  appearanceBeforeSettings = structuredClone(appearance);
  populateAppearance();
  showSettingsSection("appearance");
  renderTargets();
  showOverlay("#settingsModal");
}
function fieldInput(label, value, oninput, placeholder = "") {
  const field = el("div", "field");
  const text = el("label", "", label);
  field.append(text);
  const input = el("input");
  input.id = createId("field");
  text.htmlFor = input.id;
  input.value = value || "";
  input.placeholder = placeholder;
  input.oninput = () => oninput(input.value);
  field.append(input);
  return field;
}
function renderTargets() {
  $("#targetEditor").replaceChildren(
    ...settingsDraft.targets.map((target, index) => {
      const card = el("div", "target-card"),
        head = el("div", "target-head"),
        title = el(
          "div",
          "target-title",
          target.name || t("Environment {number}", { number: index + 1 }),
        ),
        type = el("select", "target-type");
      type.setAttribute(
        "aria-label",
        t("Environment type for {name}", { name: target.name }),
      );
      [
        ["local", t("Local computer")],
        ["docker", t("Docker container")],
      ].forEach(([value, label]) => {
        const option = el("option", "", label);
        option.value = value;
        option.selected = target.type === value;
        type.append(option);
      });
      type.onchange = () => {
        target.type = type.value;
        if (target.type === "docker") {
          target.container_name ||= target.id;
          target.host_workdir ||= "~";
          target.workdir = target.workdir === "~" ? "/" : target.workdir || "/";
        }
        renderTargets();
      };
      const remove = el("button", "icon-button", "×");
      remove.title = t("Delete environment");
      remove.disabled = settingsDraft.targets.length === 1;
      remove.onclick = () => {
        settingsDraft.targets.splice(index, 1);
        renderTargets();
      };
      head.append(title, type, remove);
      card.append(head);
      const fields = el("div", "target-fields");
      fields.append(
        fieldInput(t("Name"), target.name, (value) => {
          target.name = value;
          title.textContent =
            value || t("Environment {number}", { number: index + 1 });
        }),
        fieldInput(
          t("ID"),
          target.id,
          (value) => (target.id = value),
          t("e.g. build-server"),
        ),
        fieldInput(
          target.type === "docker"
            ? t("Container working directory")
            : t("Working directory"),
          target.workdir,
          (value) => (target.workdir = value),
          target.type === "docker" ? "/workspace" : "~/projects/tool",
        ),
        fieldInput(
          t("Setup command"),
          target.source_command,
          (value) => (target.source_command = value),
          t("e.g. source install/setup.bash"),
        ),
      );
      if (target.type === "docker")
        fields.append(
          fieldInput(
            t("Container name"),
            target.container_name,
            (value) => (target.container_name = value),
          ),
          fieldInput(
            t("Host project directory"),
            target.host_workdir,
            (value) => (target.host_workdir = value),
            "~/projects/example",
          ),
          fieldInput(
            t("Container startup command"),
            target.start_command,
            (value) => (target.start_command = value),
            t("e.g. docker start my-container"),
          ),
        );
      card.append(fields);
      return card;
    }),
  );
}
function openVariables(workspace) {
  variablesWorkspaceId = workspace.id;
  $("#workspaceName").value = workspace.name;
  $("#workspacePreset").replaceChildren();
  optionList($("#workspacePreset"), workspace.preset_id);
  $("#workspaceBagPath").value = workspace.bag_path || "";
  variablesDraft = Object.entries(workspace.variables || {}).map(
    ([key, value]) => ({ key, value }),
  );
  panelAppearanceDraft = structuredClone(workspace.appearance || {});
  renderPanelAppearance();
  renderVariables();
  showOverlay("#variablesModal");
}
function renderPanelAppearance() {
  $("#panelColors").replaceChildren(
    ...["accent", "surface", "text"].map((key) =>
      colorControl(key, panelAppearanceDraft, updatePanelPreview, true),
    ),
  );
  $("#panelLayout").value = panelAppearanceDraft.layout || "inherit";
  updatePanelPreview();
}
function updatePanelPreview() {
  const values = panelAppearance(appearance, panelAppearanceDraft);
  preview(
    $("#panelAppearancePreview"),
    values,
    $("#workspaceName").value || t("Panel preview"),
  );
  $("#panelContrast").textContent =
    contrast(values.text, values.surface) < 4.5
      ? t("Low text contrast. Choose a lighter or darker text color.")
      : "";
}
function renderVariables() {
  $("#variablesEditor").replaceChildren(
    ...variablesDraft.map((item, index) => {
      const row = el("div", "variable-row"),
        key = el("input"),
        value = el("input"),
        remove = el("button", "icon-button", "×");
      key.setAttribute(
        "aria-label",
        t("Variable {number} name", { number: index + 1 }),
      );
      value.setAttribute(
        "aria-label",
        t("Variable {number} value", { number: index + 1 }),
      );
      remove.setAttribute(
        "aria-label",
        t("Delete variable {number}", { number: index + 1 }),
      );
      key.placeholder = t("name");
      key.value = item.key;
      key.oninput = () => (item.key = key.value);
      value.placeholder = t("value");
      value.value = item.value;
      value.oninput = () => (item.value = value.value);
      remove.onclick = () => {
        variablesDraft.splice(index, 1);
        renderVariables();
      };
      row.append(key, value, remove);
      return row;
    }),
  );
}

function closeSettings() {
  if (appearanceBeforeSettings) appearance = appearanceBeforeSettings;
  applyAppearance();
  hideOverlay();
}
function closeCurrentOverlay() {
  if (activeOverlay?.id === "presetDrawer") closeDrawer();
  else if (activeOverlay?.id === "settingsModal") closeSettings();
  else hideOverlay();
}

// Each mutation has one error boundary and one busy state; transport errors
// never leave an unhandled promise or let a second click submit the same form.
document.addEventListener(
  "pointerdown",
  () => (lastInteractionAt = Date.now()),
  true,
);
document.addEventListener(
  "input",
  () => (lastInteractionAt = Date.now()),
  true,
);
$("#searchPanels").oninput = (event) => {
  searchQuery = event.target.value;
  render();
};
document.querySelectorAll("[data-filter]").forEach(
  (control) =>
    (control.onclick = () => {
      statusFilter = control.dataset.filter;
      render();
    }),
);
$("#clearFilters").onclick = () => {
  searchQuery = "";
  statusFilter = "all";
  $("#searchPanels").value = "";
  render();
};
$("#createButton").onclick = () => {
  $("#createPanel").classList.remove("hidden");
  $("#newName").focus();
};
$("#cancelCreate").onclick = () => $("#createPanel").classList.add("hidden");
$("#emptyAction").onclick = () =>
  state?.presets.length ? $("#createButton").click() : openDrawer();
bindAction("#confirmCreate", async () => {
  const name = $("#newName").value.trim();
  if (!name) {
    $("#newName").focus();
    throw new Error(t("Enter a panel name"));
  }
  await mutate("/api/workspaces", "POST", {
    name,
    bag_path: $("#newBag").value.trim(),
    preset_id: $("#newPreset").value,
  });
  $("#newName").value = "";
  $("#newBag").value = "";
  $("#createPanel").classList.add("hidden");
  toast(t("Panel created"));
});
bindAction("#refreshButton", async () => {
  if (await refresh(false)) toast(t("Status refreshed"));
});
bindAction("#retryConnection", () => refresh(false));
$("#presetsButton").onclick = () => openDrawer();
$("#drawerBackdrop").onclick = closeDrawer;
document
  .querySelectorAll(".close-drawer")
  .forEach((node) => (node.onclick = closeDrawer));
["#presetName", "#presetDescription", "#presetProject"].forEach(
  (id) => ($(id).oninput = syncDraftFields),
);
bindAction("#addStep", () => {
  draft.steps.push(newStep(draft.steps.length + 1));
  renderStepEditor();
  feedback();
});
bindAction("#savePreset", savePreset);
bindAction("#duplicatePreset", () => {
  syncDraftFields();
  draft = {
    ...structuredClone(draft),
    id: "",
    name: copyName(
      draft.name || t("Scenario"),
      state.presets.map((preset) => preset.name),
    ),
  };
  draft.steps = draft.steps.map((step) => ({
    ...step,
    id: createId("command"),
  }));
  savedDraft = "";
  fillPreset();
  $("#presetName").focus();
});
bindAction("#deletePreset", async () => {
  if (
    !draft.id ||
    !confirm(t("Delete scenario “{name}”?", { name: draft.name }))
  )
    return;
  await mutate(`/api/presets/${encodeURIComponent(draft.id)}`, "DELETE");
  draft = structuredClone(state.presets[0] || newDraft());
  savedDraft = JSON.stringify(draft);
  fillPreset();
});
$("#exportPreset").onclick = () => {
  syncDraftFields();
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" }),
  );
  const anchor = el("a");
  anchor.href = url;
  anchor.download = `${draft.id || "scenario"}.json`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("#importPreset").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (!allowDiscard()) return;
    if (file.size > 1024 * 1024)
      throw new Error(t("Scenario files must be smaller than 1 MB"));
    const imported = importPreset(
      JSON.parse(await file.text()),
      state.settings.targets,
      () => createId("command"),
    );
    draft = imported;
    savedDraft = "";
    fillPreset();
    toast(t("Imported as a new scenario. Review the commands and save."));
  } catch (error) {
    toast(
      error instanceof SyntaxError ? t("Invalid JSON file") : error.message,
      true,
    );
  } finally {
    event.target.value = "";
  }
};
$("#settingsButton").onclick = openSettings;
document
  .querySelectorAll(".close-settings")
  .forEach((node) => (node.onclick = closeSettings));
bindAction("#addTarget", () => {
  settingsDraft.targets.push({
    id: createId("target"),
    name: t("New environment"),
    type: "local",
    workdir: "~",
    source_command: "",
  });
  renderTargets();
});
bindAction("#saveSettings", async () => {
  const ids = settingsDraft.targets.map((target) => target.id.trim());
  if (ids.some((id) => !id) || new Set(ids).size !== ids.length)
    throw new Error(t("Environment IDs must be nonempty and unique"));
  if (JSON.stringify(settingsDraft) !== JSON.stringify(state.settings))
    await mutate("/api/settings", "POST", settingsDraft);
  localStorage.setItem(
    "scenario-runner-appearance-v1",
    JSON.stringify(appearance),
  );
  appearanceBeforeSettings = null;
  applyAppearance();
  hideOverlay();
  toast(t("Settings saved"));
});
document.querySelectorAll("[data-theme-choice]").forEach(
  (control) =>
    (control.onclick = () => {
      appearance.theme = control.dataset.themeChoice;
      Object.assign(appearance, palettes[appearance.theme]);
      populateAppearance();
    }),
);
document.querySelectorAll("[data-density-choice]").forEach(
  (control) =>
    (control.onclick = () => {
      appearance.density = control.dataset.densityChoice;
      updateAppearancePreview();
    }),
);
for (const [id, key] of Object.entries({
  appearanceLayout: "layout",
  appearanceFont: "fontFamily",
  appearanceCustomFont: "customFont",
})) {
  $(`#${id}`).oninput = (event) => {
    appearance[key] = event.target.value;
    updateAppearancePreview();
  };
}
for (const [id, key] of Object.entries({
  appearanceFontScale: "fontScale",
  appearanceOpacity: "panelOpacity",
})) {
  $(`#${id}`).oninput = (event) => {
    appearance[key] = Number(event.target.value);
    updateAppearancePreview();
  };
}
$("#appearanceGlass").onchange = (event) => {
  appearance.glass = event.target.checked;
  updateAppearancePreview();
};
$("#appearanceMotion").onchange = (event) => {
  appearance.motion = event.target.checked;
  updateAppearancePreview();
};
$("#resetAppearance").onclick = () => {
  appearance = normalizeAppearance(null);
  populateAppearance();
};
document
  .querySelectorAll("[data-settings-choice]")
  .forEach(
    (control) =>
      (control.onclick = () =>
        showSettingsSection(control.dataset.settingsChoice)),
  );
$("#panelLayout").onchange = (event) => {
  if (event.target.value === "inherit") delete panelAppearanceDraft.layout;
  else panelAppearanceDraft.layout = event.target.value;
  updatePanelPreview();
};
$("#resetPanelAppearance").onclick = () => {
  panelAppearanceDraft = {};
  renderPanelAppearance();
};
$("#workspaceName").addEventListener("input", updatePanelPreview);
document
  .querySelectorAll(".close-variables")
  .forEach((node) => (node.onclick = hideOverlay));
bindAction("#addVariable", () => {
  variablesDraft.push({ key: "", value: "" });
  renderVariables();
});
bindAction("#saveVariables", async () => {
  const name = $("#workspaceName").value.trim();
  if (!name) throw new Error(t("Enter a panel name"));
  const variables = variablesFromRows(variablesDraft);
  const workspace = state.workspaces.find(
    (item) => item.id === variablesWorkspaceId,
  );
  const body = {
    name,
    variables,
    appearance: panelAppearanceDraft,
    bag_path: $("#workspaceBagPath").value.trim(),
  };
  if ($("#workspacePreset").value !== workspace.preset_id)
    body.preset_id = $("#workspacePreset").value;
  await mutate(workspacePath(variablesWorkspaceId, "/update"), "POST", body);
  hideOverlay();
  toast(t("Parameters saved"));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && activeOverlay) {
    event.preventDefault();
    closeCurrentOverlay();
  }
  if (
    (event.metaKey || event.ctrlKey) &&
    event.key.toLowerCase() === "s" &&
    activeOverlay?.id === "presetDrawer"
  ) {
    event.preventDefault();
    $("#savePreset").click();
  }
  if (event.key === "Tab" && activeOverlay) {
    const controls = [
      ...activeOverlay.querySelectorAll(
        'button:not(:disabled), input, select, textarea, [tabindex="0"]',
      ),
    ].filter((node) => node.getClientRects().length);
    const first = controls[0],
      last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
});
window.addEventListener("beforeunload", (event) => {
  if (activeOverlay?.id === "presetDrawer" && isDirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh(true);
});
// Associate static labels without repeating IDs in every template.
document.querySelectorAll(".field").forEach((field) => {
  const label = field.querySelector("label"),
    control = field.querySelector("input,select,textarea");
  if (label && control?.id) label.htmlFor = control.id;
});
applyAppearance();
translateDocument();
$("#languageSelect").value = getLanguage();
$("#languageSelect").onchange = (event) => {
  setLanguage(event.target.value);
  translateDocument();
  render();
  if (activeOverlay?.id === "presetDrawer") {
    renderPresetTabs();
    renderStepEditor();
    feedback();
  } else if (activeOverlay?.id === "settingsModal") {
    renderTargets();
    populateAppearance();
  } else if (activeOverlay?.id === "variablesModal") {
    renderVariables();
    renderPanelAppearance();
  }
  $("#toast").className = "toast";
  try {
    localStorage.setItem("scenario-runner-language", getLanguage());
  } catch {
    toast(t("Could not save the language preference."), true);
  }
};
refresh(false);
setInterval(() => {
  if (!document.hidden) refresh(true);
}, 2000);
