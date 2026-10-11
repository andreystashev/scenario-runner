import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  appearanceDefaults,
  palettes,
  normalizeAppearance,
  panelAppearance,
  colorTokens,
  contrast,
  readableText,
} from "../static/appearance.js";

test("legacy preferences retain custom colors while new semantic colors and layout get defaults", () => {
  const theme = normalizeAppearance({
    theme: "light",
    accent: "#123456",
    surface: "#ffffee",
    fontScale: 4,
    glass: false,
  });
  assert.equal(theme.accent, "#123456");
  assert.equal(theme.surface, "#ffffee");
  assert.equal(theme.info, palettes.light.info);
  assert.equal(theme.layout, "grid");
  assert.equal(theme.fontScale, 1.5);
  assert.equal(theme.glass, false);
  const invalid = normalizeAppearance({
    theme: "__proto__",
    text: "url(evil)",
    layout: "broken",
    motion: "true",
  });
  assert.equal(invalid.text, appearanceDefaults.text);
  assert.equal(invalid.motion, true);
});
test("panel overrides are isolated, inherit changes and choose readable text for a custom surface", () => {
  const global = normalizeAppearance(null),
    overrides = { accent: "#aabbee", surface: "#ffffff", layout: "list" };
  const panel = panelAppearance(global, overrides),
    other = panelAppearance(global);
  assert.equal(panel.accent, "#aabbee");
  assert.equal(panel.layout, "list");
  assert.ok(contrast(panel.text, panel.surface) >= 4.5);
  assert.equal(other.accent, global.accent);
  assert.equal(global.surface, palettes.dark.surface);
  global.info = "#778899";
  assert.equal(panelAppearance(global, overrides).info, "#778899");
  assert.deepEqual(panelAppearance(global, {}), global);
});
test("every derived shade and primary button text follows chosen colors", () => {
  const a = colorTokens(palettes.dark),
    b = colorTokens(palettes.light);
  for (const token of [
    "--bg-soft",
    "--surface-2",
    "--line",
    "--line-strong",
    "--muted",
    "--accent-soft",
    "--log-bg",
  ])
    assert.notEqual(a[token], b[token]);
  for (const color of ["#ffffff", "#111111", "#48e29a", "#168f62", "#6699ff"])
    assert.ok(contrast(readableText(color), color) >= 4.5);
  assert.equal(
    colorTokens({ ...palettes.dark, danger: "#ff0011" })["--danger"],
    "#ff0011",
  );
});
test("application CSS uses palette roles, with fixed colors only in defaults and theme thumbnails", () => {
  let css = readFileSync(new URL("../static/app.css", import.meta.url), "utf8");
  css = css
    .replace(/:root\s*\{[^}]*\}/s, "")
    .replace(/\.(?:dark|light)-choice[^}]+\}/g, "");
  assert.ok(!/#[\da-f]{3,8}\b/i.test(css), "Unmapped fixed color");
  assert.ok(!/rgba?\((?!0, 0, 0)[\d., ]+\)/.test(css), "Unmapped RGB color");
});
