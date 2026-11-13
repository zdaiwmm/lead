import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import type { Dict, RpcLike } from "./types.ts";
export class AppServer implements RpcLike {
  process?: ChildProcessWithoutNullStreams;
  sequence = 0;
  pending = new Map<
    number,
    {
      resolve: (x: any) => void;
      reject: (e: Error) => void;
      timer: NodeJS.Timeout;
    }
  >();
  notification: (m: string, p: Dict) => void = () => {};
  serverRequest: (id: string | number, m: string, p: Dict) => void = () => {};
  binary: string;
  args: string[];
  cwd?: string;
  constructor(binary = "codex", args: string[] = [], cwd?: string) {
    this.binary = binary;
    this.args = args;
    this.cwd = cwd;
  }
  onNotification(fn: typeof this.notification) {
    this.notification = fn;
  }
  onRequest(fn: typeof this.serverRequest) {
    this.serverRequest = fn;
  }
  async open() {
    const env = { ...process.env };
    delete env.LEAD_EXECUTOR_TOKEN;
    delete env.OPENAI_API_KEY;
    this.process = spawn(
      this.binary,
      [...this.args, "app-server", "--listen", "stdio://"],
      { cwd: this.cwd, env, stdio: ["pipe", "pipe", "pipe"] },
    );
    // Stderr is deliberately not forwarded: it can contain private paths or tool data.
    this.process.stderr.resume();
    this.process.on("error", (error) => this.rejectAll(error));
    this.process.on("exit", () => {
      this.rejectAll(new Error("app_server_closed"));
      this.notification("connection/closed", {});
    });
    const lines = createInterface({ input: this.process.stdout });
    lines.on("line", (line) => {
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        this.rejectAll(new Error("app_server_invalid_json"));
        return;
      }
      if (msg.method && msg.id !== undefined) {
        this.serverRequest(msg.id, msg.method, msg.params || {});
        return;
      }
      if (msg.method) {
        this.notification(msg.method, msg.params || {});
        return;
      }
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error)
        entry.reject(new Error("rpc_error:" + JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
    });
    await this.request("initialize", {
      clientInfo: {
        name: "lead_local_executor",
        title: "Lead Local Executor",
        version: "0.1.0",
      },
    });
    this.send({ method: "initialized" });
  }
  send(value: Dict) {
    if (!this.process?.stdin.writable)
      throw new Error("app_server_not_writable");
    this.process.stdin.write(JSON.stringify(value) + "\n");
  }
  request(method: string, params: Dict = {}) {
    const id = ++this.sequence;
    return new Promise<any>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("rpc_ack_timeout:" + method));
      }, 60_000);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.send({ id, method, params });
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error as Error);
      }
    });
  }
  respond(id: string | number, result: any) {
    this.send({ id, result });
  }
  rejectAll(error: Error) {
    for (const item of this.pending.values()) {
      clearTimeout(item.timer);
      item.reject(error);
    }
    this.pending.clear();
  }
  async close() {
    const proc = this.process;
    if (!proc || proc.exitCode !== null) return;
    proc.stdin.end();
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        proc.kill("SIGTERM");
        resolve();
      }, 2000);
      proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}
