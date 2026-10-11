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
    if (!response.ok)
      throw new Error(body.error || `Ошибка HTTP ${response.status}`);
    return body;
  } catch (error) {
    if (error.name === "AbortError")
      throw new Error(
        "Сервер не ответил вовремя. Обновите статусы перед повторным запуском.",
      );
    if (error instanceof TypeError)
      throw new Error(
        "Нет связи с сервером. Проверьте, что Scenario Runner запущен.",
      );
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
