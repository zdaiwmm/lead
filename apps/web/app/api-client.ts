type Row = Record<string, any>;
let workspace: string | null = null;
let access: Row | null = null;
export function getAccess() {
  return access;
}
export function setWorkspace(value: string | null) {
  const changed = workspace !== value;
  workspace = value;
  if (changed && typeof window !== "undefined") window.dispatchEvent(new Event("lead:workspace-ready"));
}
export function getWorkspace() {
  return workspace;
}
export function workspaceHeaders(): Record<string, string> {
  return workspace ? { "X-Lead-Workspace": workspace } : {};
}
export async function api(path: string, body?: Row, signal?: AbortSignal) {
  const scope =
    path === "/auth/me" || path === "/session" ? {} : workspaceHeaders();
  const dashboardRead = !body && /^\/agents\/[^/]+\/(plans|operations|analytics\/overview)(?:\?|$)/.test(path);
  const deadline = AbortSignal.timeout(body ? 60000 : 5000);
  const requestSignal = signal ? AbortSignal.any([signal, deadline]) : deadline;
  const request = () => fetch("/api" + path, {
    method: body ? "POST" : "GET",
    headers: {
      ...scope,
      ...(body
        ? { "Content-Type": "application/json", "X-Lead-Request": "1" }
        : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
    signal: requestSignal,
  });
  let response: Response;
  for(let attempt=0;;attempt++){
    try {
      response=await request();
      const unavailable=[502,503,504].includes(response.status) ||
        (response.status===500 && !response.headers.get("content-type")?.includes("application/json"));
      if(!dashboardRead || !unavailable || attempt>=2) break;
      await response.body?.cancel();
    } catch(error) {
      if (deadline.aborted) throw Error("连接超时，正在等待下一次刷新");
      if(!dashboardRead || !(error instanceof TypeError) || signal?.aborted || attempt>=2) throw error;
    }
    await new Promise(resolve=>setTimeout(resolve,(attempt===0?1200:3500)+Math.random()*450));
    requestSignal.throwIfAborted();
    if(scope["X-Lead-Workspace"]!==workspaceHeaders()["X-Lead-Workspace"]) throw Error("工作区已切换");
  }
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


export type ExecutionCadence = {
  state: "ready" | "waiting_time" | "waiting_event" | "receipt_only" | "terminal" | "unverified";
  reason_label: string;
  evidence_source: string;
  scope_label: string;
  next_at: string | null;
  resume_event_label: string | null;
  needs_user: boolean;
  policy_version: number | null;
  compatible: boolean | null;
};

export function enableManagedScope(aid: string, account: Row, controlVersion: number) {
  return api(`/agents/${aid}/accounts/${account.id}/managed-scope`, {
    binding_version: account.binding_version,
    expected_control_version: controlVersion,
  });
}
