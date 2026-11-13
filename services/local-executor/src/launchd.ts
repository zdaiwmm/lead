import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
const label = "com.lead.local-executor";
const xml = (s: string) =>
  s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
export function launchAgentPlist(
  state: string,
  node: string,
  cli: string,
  path: string,
) {
  const environment = {
    PATH: path,
    ...(process.env.LEAD_CODEX_BINARY
      ? { LEAD_CODEX_BINARY: process.env.LEAD_CODEX_BINARY }
      : {}),
    ...(process.env.LEAD_CODEX_MEMORIES
      ? { LEAD_CODEX_MEMORIES: process.env.LEAD_CODEX_MEMORIES }
      : {}),
  };
  return `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>Label</key><string>${label}</string><key>ProgramArguments</key><array>${[node, cli, "run", "--state", state].map((v) => "<string>" + xml(v) + "</string>").join("")}</array><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer><key>EnvironmentVariables</key><dict>${Object.entries(
    environment,
  )
    .map(([k, v]) => "<key>" + k + "</key><string>" + xml(v) + "</string>")
    .join(
      "",
    )}</dict><key>StandardOutPath</key><string>${xml(join(state, "service.log"))}</string><key>StandardErrorPath</key><string>${xml(join(state, "service-error.log"))}</string></dict></plist>\n`;
}
export function launchd(
  action: "install" | "uninstall",
  state: string,
  cli: string,
) {
  if (process.platform !== "darwin") throw new Error("launchd_requires_macos");
  const folder = join(homedir(), "Library", "LaunchAgents"),
    plist = join(folder, label + ".plist"),
    domain = "gui/" + process.getuid!();
  spawnSync("/bin/launchctl", ["bootout", domain + "/" + label], {
    stdio: "ignore",
  });
  if (action === "uninstall") {
    rmSync(plist, { force: true });
    return;
  }
  mkdirSync(folder, { recursive: true });
  writeFileSync(
    plist,
    launchAgentPlist(
      state,
      process.execPath,
      cli,
      process.env.PATH || "/usr/bin:/bin",
    ),
    { mode: 0o600 },
  );
  const result = spawnSync("/bin/launchctl", ["bootstrap", domain, plist], {
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error("launchd_install_failed");
}
