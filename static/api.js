import { t, serverText } from "./i18n.js";
/** JSON transport. A timeout never implies that a command was not started. */
export function workspacePath(id, suffix = "") {
  return `/api/workspaces/${encodeURIComponent(id)}${suffix}`;
}

export async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    options.method ? 60000 : 10000,
  );
  try {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
      signal: controller.signal,
    });
    const body = await response.json();
    if (!response.ok) {
      const error = Array.isArray(body.failures)
        ? t("Some commands could not be stopped.") +
          " " +
          body.failures
            .map(
              (failure) => `${failure.title}: ${serverText(failure.message)}`,
            )
            .join("; ")
        : serverText(body.error) ||
          t("HTTP error {status}", { status: response.status });
      throw new Error(error);
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError")
      throw new Error(
        t("The server timed out. Refresh status before running again."),
      );
    if (error instanceof TypeError)
      throw new Error(
        t(
          "Cannot connect to the server. Check that Scenario Runner is running.",
        ),
      );
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
