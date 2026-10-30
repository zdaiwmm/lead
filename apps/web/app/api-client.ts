type Row = Record<string, any>;
let workspace: string | null = null;
let access: Row | null = null;
export function getAccess() {
  return access;
}
export function setWorkspace(value: string | null) {
  workspace = value;
}
export function getWorkspace() {
  return workspace;
}
export function workspaceHeaders(): Record<string, string> {
  return workspace ? { "X-Lead-Workspace": workspace } : {};
}
export async function api(path: string, body?: Row) {
  const scope =
    path === "/auth/me" || path === "/session" ? {} : workspaceHeaders();
  const response = await fetch("/api" + path, {
    method: body ? "POST" : "GET",
    headers: {
      ...scope,
      ...(body
        ? { "Content-Type": "application/json", "X-Lead-Request": "1" }
        : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const failure = await response
      .json()
      .catch(() => ({ detail: "连接暂时不可用" }));
    if (failure.code === "workspace_changed") window.location.reload();
    if (response.status === 401)
      window.dispatchEvent(
        new CustomEvent("lead:session-expired", {
          detail: failure.login_url || "/api/auth/login",
        }),
      );
    throw Error(
      typeof failure.detail === "string" ? failure.detail : "操作未完成",
    );
  }
  const data = await response.json();
  if (path === "/auth/me") access = data;
  return data;
}
