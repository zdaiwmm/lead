import { resolve } from "node:path";
export interface SharedRoots { home: string; state: string; codexHome: string; workspace: string; }
const cliKeys = new Set(["PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
  "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"]);
export const mcpKeys = new Set(["PATH", "LANG", "LC_ALL", "TZ", "LEAD_REQUEST_BRIDGE", "LEAD_REQUEST_TICKET", "LEAD_EXECUTOR_REQUEST"]);
export function sharedCliEnvironment(source: NodeJS.ProcessEnv) {
  return Object.fromEntries(Object.entries(source).filter(([key, value]) => cliKeys.has(key) && value !== undefined));
}
export function sharedNativeArgs(roots: SharedRoots): string[] {
  const paths = [...new Set([roots.home, roots.state, roots.codexHome, "/var/lib/lead-codex-node", "/run/lead-codex-node", "/run/credentials"])];
  if (paths.some(path => !path.startsWith("/") || resolve(path) !== path || path === "/") ||
      !roots.workspace.startsWith("/") || resolve(roots.workspace) !== roots.workspace) throw new Error("invalid_shared_roots");
  const filesystem = paths.map(path => JSON.stringify(path) + '="none"').concat('\":workspace_roots\"="write"').join(",");
  return ["-c", `permissions.lead_shared={extends=":read-only",filesystem={${filesystem}},network={enabled=true}}`,
    "-c", 'default_permissions="lead_shared"', "-c", 'approval_policy="never"', "-c",
    `projects={${JSON.stringify(roots.workspace)}={trust_level="untrusted"}}`, "-c",
    `shell_environment_policy={inherit="none",ignore_default_excludes=false,set={PATH="/opt/lead-codex/node/bin:/usr/local/bin:/usr/bin:/bin",HOME=${JSON.stringify(roots.workspace)},LANG="C.UTF-8"}}`];
}
export function sharedDenial(method: string): Record<string, unknown> | undefined {
  if (method === "item/tool/requestUserInput") return undefined;
  if (method.includes("permissions/")) return { permissions: {}, scope: "turn" };
  if (method.includes("elicitation")) return { action: "decline" };
  return { decision: "decline" };
}
