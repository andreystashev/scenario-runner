/** Theme values are shared by previews and real panels. No browser state here. */
export const palettes = {
  dark: {
    accent: "#48e29a",
    background: "#091015",
    surface: "#111d26",
    text: "#edf5f4",
    info: "#72b7ff",
    success: "#48e29a",
    warning: "#f0c56d",
    danger: "#ff767e",
  },
  light: {
    accent: "#168f62",
    background: "#edf3f1",
    surface: "#ffffff",
    text: "#13211e",
    info: "#286cba",
    success: "#168f62",
    warning: "#946000",
    danger: "#be3445",
  },
};
export const colorKeys = Object.keys(palettes.dark);
export const appearanceDefaults = {
  theme: "dark",
  ...palettes.dark,
  fontFamily: "system",
  customFont: "",
  fontScale: 1,
  density: "comfortable",
  panelOpacity: 0.96,
  glass: true,
  layout: "grid",
  motion: true,
};
export const isColor = (value) =>
  typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
export function normalizeAppearance(saved) {
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const theme = Object.hasOwn(palettes, saved.theme) ? saved.theme : "dark";
  const result = { ...appearanceDefaults, ...palettes[theme], ...saved, theme };
  for (const key of colorKeys)
    if (!isColor(result[key])) result[key] = palettes[theme][key];
  for (const [key, choices] of Object.entries({
    fontFamily: ["system", "mono", "serif", "custom"],
    density: ["comfortable", "compact"],
    layout: ["grid", "list"],
  }))
    if (!choices.includes(result[key])) result[key] = appearanceDefaults[key];
  result.customFont =
    typeof result.customFont === "string" ? result.customFont : "";
  for (const [key, min, max] of [
    ["fontScale", 0.9, 1.5],
    ["panelOpacity", 0.72, 1],
  ])
    result[key] = Number.isFinite(result[key])
      ? Math.min(max, Math.max(min, result[key]))
      : appearanceDefaults[key];
  for (const key of ["glass", "motion"])
    if (typeof result[key] !== "boolean") result[key] = appearanceDefaults[key];
  return result;
}
const rgb = (hex) =>
  hex
    .slice(1)
    .match(/../g)
    .map((value) => parseInt(value, 16));
export function mix(a, b, weight) {
  return (
    "#" +
    rgb(a)
      .map((value, index) =>
        Math.round(value * weight + rgb(b)[index] * (1 - weight))
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
  );
}
function luminance(hex) {
  const channels = rgb(hex).map((value) => {
    const c = value / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
export function contrast(a, b) {
  const values = [luminance(a), luminance(b)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
export function readableText(background) {
  return contrast("#ffffff", background) > contrast("#111111", background)
    ? "#ffffff"
    : "#111111";
}
export function panelAppearance(global, overrides = {}) {
  const result = { ...global };
  for (const key of ["accent", "surface", "text"])
    if (isColor(overrides[key])) result[key] = overrides[key];
  if (isColor(overrides.surface)) {
    result.background = mix(
      result.surface,
      readableText(result.surface) === "#ffffff" ? "#000000" : "#ffffff",
      0.85,
    );
    if (!isColor(overrides.text) && contrast(result.text, result.surface) < 4.5)
      result.text = readableText(result.surface);
  }
  if (["grid", "list"].includes(overrides.layout))
    result.layout = overrides.layout;
  return result;
}
export function colorTokens(value) {
  return {
    "--bg": value.background,
    "--surface": value.surface,
    "--text": value.text,
    "--accent": value.accent,
    "--info": value.info,
    "--success": value.success,
    "--warning": value.warning,
    "--danger": value.danger,
    "--on-accent": readableText(value.accent),
    "--bg-soft": mix(value.surface, value.background, 0.65),
    "--surface-2": mix(value.text, value.surface, 0.06),
    "--line": mix(value.text, value.surface, 0.16),
    "--line-strong": mix(value.text, value.surface, 0.3),
    "--muted": mix(value.text, value.surface, 0.68),
    "--accent-soft": mix(value.accent, value.surface, 0.16),
    "--log-bg": mix(value.background, value.surface, 0.45),
  };
}
