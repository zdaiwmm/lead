import { randomBytes, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { Daemon } from "./daemon.ts";
import { Store } from "./store.ts";
import { Runner } from "./runner.ts";
import { BrokerError, nodeFailure, ScopeClient, UnixRpc } from "./broker-client.ts";
import { bindingIdentity, scopeStoreFolder, validateServerBinding } from "./server-binding.ts";
import { RequestBridge } from "./request-bridge.ts";
import type { BrokerRpc, Dict, ExecutorClient, ServerBinding } from "./types.ts";
export { scopeStoreFolder, validateServerBinding } from "./server-binding.ts";
interface Options {
  capabilities?: () => Promise<Dict>;
  runnerFactory?: (store: Store, client: ExecutorClient, bridge: RequestBridge) => Runner;
}
export class ServerWorker {
  root: string; broker: BrokerRpc; options: Options; instance = randomUUID(); generation?: string;
  scopes = new Map<string, { store: Store; daemon: Daemon; recovered: boolean }>();
  fenced = new Set<string>(); stopped = false; bridge?: RequestBridge;
  lastCapabilities = 0; capabilities: Dict = {};
  constructor(root: string, broker: BrokerRpc = new UnixRpc(), options: Options = {}) {
    this.root = resolve(root); this.broker = broker; this.options = options;
    mkdirSync(this.root, { recursive: true, mode: 0o700 });
    const file = join(this.root, "fenced-scopes.json");
    if (existsSync(file)) {
      const ids = JSON.parse(readFileSync(file, "utf8"));
      if (!Array.isArray(ids) || ids.some(id => typeof id !== "string")) throw new BrokerError("invalid_scope_fences");
      this.fenced = new Set(ids);
    }
  }
  private async fence(ref: string) {
    this.fenced.add(ref);
    const file = join(this.root, "fenced-scopes.json"), temporary = file + ".tmp";
    writeFileSync(temporary, JSON.stringify([...this.fenced]), { mode: 0o600 }); renameSync(temporary, file);
    const entry = this.scopes.get(ref); this.scopes.delete(ref); await entry?.daemon.close();
  }
  private async failure(ref: string, error: unknown) {
    if (nodeFailure(error)) { await this.close(); throw error; }
    if (error instanceof BrokerError && [401, 403, 409].includes(error.status)) await this.fence(ref);
  }
  async tick() {
    if (this.stopped) throw new BrokerError("server_worker_stopped");
    try {
      if (!this.generation || Date.now() - this.lastCapabilities > 60_000) {
        try { this.capabilities = await this.options.capabilities?.() || {}; }
        catch { this.capabilities = { ready: false, auth_type: "unavailable", models: [], reasoning_efforts: [] }; }
        const hello = await this.broker.call("worker.hello", { instance_id: this.instance, capabilities: this.capabilities });
        if (typeof hello.worker_generation !== "string" || !hello.worker_generation ||
            this.generation && this.generation !== hello.worker_generation) throw new BrokerError("broker_worker_changed", 409);
        this.generation = hello.worker_generation; this.lastCapabilities = Date.now();
        if (!this.bridge) {
          this.bridge = new RequestBridge(join(this.root, "b-" + randomBytes(6).toString("hex") + ".sock"), this.generation);
          await this.bridge.start();
        }
      }
      const directory = await this.broker.call("scope.list", { worker_generation: this.generation });
      if (!Array.isArray(directory.scopes)) throw new BrokerError("broker_invalid_directory");
      const bindings = directory.scopes.map((scope: Dict) => validateServerBinding({ ...scope, version: 2, transport: "unix-broker",
        policy_revision: scope.policy_revision || directory.policy_revision }));
      if (new Set(bindings.map((b: ServerBinding) => b.scope_ref)).size !== bindings.length) throw new BrokerError("broker_invalid_directory");
      const present = new Set(bindings.map((b: ServerBinding) => b.scope_ref));
      for (const ref of this.scopes.keys()) if (!present.has(ref)) await this.fence(ref);
      for (const binding of bindings) {
        if (this.fenced.has(binding.scope_ref)) continue;
        const old = this.scopes.get(binding.scope_ref);
        if (old) {
          if (bindingIdentity(old.store.data.server_binding!) !== bindingIdentity(binding) ||
              old.store.data.server_binding!.epoch !== binding.epoch) await this.fence(binding.scope_ref);
          continue;
        }
        let store: Store;
        try { store = new Store(scopeStoreFolder(this.root, binding)); store.bindServer(binding); }
        catch { await this.fence(binding.scope_ref); continue; }
        const scoped = new ScopeClient(store.data.server_binding!, this.broker, this.generation!);
        const client: ExecutorClient = { binding: scoped.binding, call: async (method, payload) => {
          try { return await scoped.call(method, payload); }
          catch (error) { void this.failure(binding.scope_ref, error).catch(() => {}); throw error; }
        } };
        const runner = this.options.runnerFactory?.(store, client, this.bridge!) || new Runner(store, undefined, {
          client, bridge: this.bridge, stateRoot: this.root,
        });
        this.scopes.set(binding.scope_ref, { store, daemon: new Daemon(store, runner, client, this.capabilities), recovered: false });
      }
      await Promise.all([...this.scopes].map(async ([ref, entry]) => {
        try {
          await entry.daemon.tick();
          if (!entry.recovered && this.capabilities.ready === true) {
            entry.daemon.recover(); entry.recovered = true;
          }
        }
        catch (error) { await this.failure(ref, error); }
      }));
      if (this.stopped) throw new BrokerError("executor_node_unavailable", 401);
    } catch (error) {
      if (nodeFailure(error) || error instanceof BrokerError && [401, 403, 409].includes(error.status)) await this.close();
      throw error;
    }
  }
  async run() {
    while (!this.stopped) {
      try { await this.tick(); }
      catch (error) { if (this.stopped || nodeFailure(error)) throw error; }
      if (!this.stopped) await new Promise(resolve => setTimeout(resolve, 2000));
    }
  }
  async close() {
    this.stopped = true;
    await Promise.all([...this.scopes.values()].map(entry => entry.daemon.close())); this.scopes.clear();
    await this.bridge?.close();
  }
}
