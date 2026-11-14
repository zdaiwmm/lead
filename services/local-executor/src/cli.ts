#!/usr/bin/env node
import {
  existsSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
  closeSync,
} from "node:fs";
import { hostname, platform } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Store } from "./store.ts";
import { capabilities } from "./capabilities.ts";
import { Daemon } from "./daemon.ts";
import { post, validateUrl } from "./transport.ts";
import { launchd, profileState } from "./launchd.ts";
import { ServerWorker } from "./server-worker.ts";
import { BROKER_SOCKET, UnixRpc } from "./broker-client.ts";
import { AppServer } from "./rpc.ts";
import { nativeArgs } from "./runner.ts";
import { sharedCliEnvironment, sharedNativeArgs } from "./shared-runtime.ts";
const args = process.argv.slice(2),
  command = args.shift() || "status";
function option(name: string, fallback?: string) {
  const index = args.indexOf("--" + name);
  return index < 0 ? fallback : args[index + 1];
}
function lock(folder: string) {
  const path = join(folder, "daemon.pid");
  if (existsSync(path)) {
    const pid = Number(readFileSync(path, "utf8"));
    try {
      process.kill(pid, 0);
      throw new Error("executor_already_running");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
    }
    unlinkSync(path);
  }
  const fd = openSync(path, "wx", 0o600);
  writeFileSync(fd, String(process.pid));
  closeSync(fd);
  return () => {
    if (existsSync(path) && readFileSync(path, "utf8") === String(process.pid))
      unlinkSync(path);
  };
}
async function main() {
  if (command === "server-run") {
    if (option("socket", BROKER_SOCKET) !== BROKER_SOCKET) throw new Error("invalid_server_socket");
    const folder = resolve(option("state", "/var/lib/lead-codex/.lead/server")!);
    const binary = process.env.LEAD_CODEX_BINARY || "codex", memories = process.env.LEAD_CODEX_MEMORIES;
    const environment = sharedCliEnvironment(process.env);
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, environment, { LEAD_CODEX_BINARY: binary }, memories === "1" ? { LEAD_CODEX_MEMORIES: "1" } : {});
    // Store creates only the server root here; no personal binding is written.
    const { mkdirSync } = await import("node:fs"); mkdirSync(folder, { recursive: true, mode: 0o700 });
    const release = lock(folder);
    const worker = new ServerWorker(folder, new UnixRpc(BROKER_SOCKET), { capabilities: () => capabilities(new AppServer(binary,
      [...nativeArgs(), ...sharedNativeArgs({ home: process.env.HOME || "/var/lib/lead-codex", state: folder,
        codexHome: process.env.CODEX_HOME || "/var/lib/lead-codex/.codex", workspace: folder })], folder, sharedCliEnvironment(process.env), true)) });
    const stop = () => { void worker.close(); };
    process.once("SIGTERM", stop); process.once("SIGINT", stop);
    try { await worker.run(); }
    finally { await worker.close(); release(); }
    return;
  }
  const folder = resolve(
      option(
        "state",
        process.env.LEAD_EXECUTOR_HOME || profileState(option("profile")),
      )!,
    ),
    store = new Store(folder);
  if (command === "pair") {
    const url = validateUrl(option("url") || ""),
      code = option("code");
    if (!code) throw new Error("pair_code_required");
    const name = option("name", hostname())!,
      caps = await capabilities();
    if (!caps.ready)
      throw new Error("sign_in_to_codex_with_chatgpt_and_enable_sol_medium");
    const result = await post(url, "/executor/v1/pair", {
      code,
      name,
      platform: platform(),
      capabilities: caps,
    });
    if (
      typeof result.token !== "string" ||
      typeof result.device_id !== "string" ||
      typeof result.epoch !== "number"
    )
      throw new Error("invalid_pair_result");
    store.bind({
      url,
      device_id: result.device_id,
      token: result.token,
      epoch: result.epoch,
      name,
    });
    if (args.includes("--install")) launchd("install", folder, fileURLToPath(import.meta.url));
    console.log(
      JSON.stringify({ paired: true, name, url, device_id: result.device_id, background: args.includes("--install") }),
    );
    return;
  }
  if (command === "status") {
    const caps = await capabilities();
    console.log(
      JSON.stringify(
        {
          paired: !!store.data.binding,
          url: store.data.binding?.url,
          name: store.data.binding?.name,
          capabilities: caps,
          pending_requests: Object.values(store.data.requests).filter(
            (r) =>
              ![
                "completed",
                "failed",
                "interrupted",
                "execution_unknown",
              ].includes(r.state),
          ).length,
        },
        null,
        2,
      ),
    );
    return;
  }
  if (command === "install" || command === "uninstall") {
    if (command === "install" && !store.data.binding)
      throw new Error("device_not_paired");
    launchd(command, folder, fileURLToPath(import.meta.url));
    console.log(JSON.stringify({ installed: command === "install" }));
    return;
  }
  if (command !== "run")
    throw new Error("use_pair_run_status_install_or_uninstall");
  const release = lock(folder);
  let daemon: Daemon | undefined;
  try {
    let caps;
    try {
      caps = await capabilities();
    } catch {
      caps = {
        ready: false,
        auth_type: "unavailable",
        models: [],
        reasoning_efforts: [],
      };
    }
    daemon = new Daemon(store, undefined, undefined, caps);
    const stop = () => {
      void daemon!.close();
    };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
    await daemon.run();
  } finally {
    await daemon?.close();
    release();
  }
}
main().catch((error) => {
  console.error(command === "server-run" ? "server_worker_stopped" : String(error));
  process.exitCode = 1;
});
