import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  t,
  setLanguage,
  getLanguage,
  serverText,
  russian,
  translateDocument,
} from "../static/i18n.js";
import {
  copyName,
  importPreset,
  statusLabel,
  variablesFromRows,
} from "../static/ui-model.js";
import { request } from "../static/api.js";

afterEach(() => setLanguage("ru"));

test("saved language loads after a reload and unavailable storage falls back to Russian", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  try {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: { getItem: () => "en" },
    });
    const saved = await import("../static/i18n.js?saved-language");
    assert.equal(saved.getLanguage(), "en");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("Storage disabled");
      },
    });
    const unavailable = await import("../static/i18n.js?unavailable-storage");
    assert.equal(unavailable.getLanguage(), "ru");
  } finally {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
    else delete globalThis.localStorage;
  }
});

test("document translation updates labels and accessible hints without touching input values", () => {
  const original = globalThis.document;
  const label = { dataset: { i18n: "Panels" }, textContent: "Панели" };
  const input = {
    value: "Мой сценарий",
    attributes: { "data-i18n-placeholder": "Command name" },
    getAttribute(name) {
      return this.attributes[name];
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
  const root = {
    documentElement: { lang: "ru" },
    querySelectorAll(selector) {
      if (selector === "[data-i18n]") return [label];
      if (selector === "[data-i18n-placeholder]") return [input];
      return [];
    },
  };
  try {
    globalThis.document = root;
    setLanguage("en");
    translateDocument(root);
    assert.equal(root.documentElement.lang, "en");
    assert.equal(label.textContent, "Panels");
    assert.equal(input.attributes.placeholder, "Command name");
    assert.equal(input.value, "Мой сценарий");
    setLanguage("ru");
    translateDocument(root);
    assert.equal(label.textContent, "Панели");
    assert.equal(input.attributes.placeholder, "Название команды");
  } finally {
    if (original) globalThis.document = original;
    else delete globalThis.document;
  }
});

test("language switching preserves placeholder values and literal command variables", () => {
  setLanguage("en");
  assert.equal(getLanguage(), "en");
  assert.equal(
    t("Delete panel {name}", { name: "<script>{{other}}</script>" }),
    "Delete panel <script>{{other}}</script>",
  );
  assert.equal(
    t("Use {{name}} and {name}", { name: "literal" }),
    "Use {{name}} and literal",
  );
  assert.equal(statusLabel({ status: "failed" }), "Failed");
  setLanguage("ru");
  assert.equal(statusLabel({ status: "failed" }), "Ошибка");
  assert.equal(t("Panels"), "Панели");
  assert.throws(() => setLanguage("fr"));
  assert.equal(getLanguage(), "ru");
});

test("server messages are localized without changing paths or technical details", () => {
  setLanguage("en");
  const path = "/tmp/Архив {{name}}/test";
  assert.equal(
    serverText(`Рабочая папка не найдена: ${path}`),
    `Working directory not found: ${path}`,
  );
  assert.equal(
    serverText("Команда завершена, код 2"),
    "Finished with exit code 2.",
  );
  assert.equal(
    serverText(
      "Не удалось закрыть окно Terminal. Повторите остановку. AppleScript -1743",
    ),
    "Could not close the scenario’s Terminal window. AppleScript -1743",
  );
  assert.equal(
    serverText("My command: обычный вывод"),
    "My command: обычный вывод",
  );
  setLanguage("ru");
  assert.equal(serverText("Пресет не найден"), "Сценарий не найден");
});

test("imports retain user names, commands and variable values across languages", () => {
  setLanguage("en");
  const command = 'echo "Привет {{message}}"';
  const imported = importPreset(
    {
      name: "Мой сценарий",
      steps: [{ title: "Моя команда", command, terminal: false }],
    },
    [{ id: "local" }],
    () => "new",
  );
  assert.equal(imported.name, "Мой сценарий");
  assert.equal(imported.steps[0].title, "Моя команда");
  assert.equal(imported.steps[0].command, command);
  assert.equal(
    variablesFromRows([{ key: "message", value: "{{value}} Привет" }]).message,
    "{{value}} Привет",
  );
  assert.equal(copyName("Мой сценарий", []), "Мой сценарий — copy");
});

test("all UI source messages and static translation keys have Russian translations", () => {
  for (const file of ["app.js", "api.js", "ui-model.js"]) {
    const source = readFileSync(
      new URL(`../static/${file}`, import.meta.url),
      "utf8",
    );
    for (const match of source.matchAll(/\bt\(\s*("(?:[^"\\]|\\.)*")/g)) {
      const key = JSON.parse(match[1]);
      assert.ok(Object.hasOwn(russian, key), `${file}: missing ${key}`);
    }
    assert.ok(!/[А-Яа-яЁё]/.test(source), `${file}: untranslated inline copy`);
  }
  const markup = readFileSync(
    new URL("../static/index.html", import.meta.url),
    "utf8",
  );
  for (const match of markup.matchAll(/data-i18n(?:-[a-z-]+)?="([^"]+)"/g)) {
    const key = match[1].replaceAll("&quot;", '"').replaceAll("&amp;", "&");
    assert.ok(Object.hasOwn(russian, key), `index.html: missing ${key}`);
  }
});

test("API errors translate each failed command while retaining user titles", async () => {
  setLanguage("en");
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    json: async () => ({
      error: "Не все команды остановлены. Моя команда: Команда не найдена",
      failures: [{ title: "Моя команда", message: "Команда не найдена" }],
    }),
  });
  try {
    await assert.rejects(
      request("/api/workspaces/panel/stop", { method: "POST" }),
      {
        message:
          "Some commands could not be stopped. Моя команда: Command not found.",
      },
    );
  } finally {
    globalThis.fetch = original;
  }
});
