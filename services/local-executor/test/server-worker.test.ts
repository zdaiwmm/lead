import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Store } from "../src/store.ts";
import { Runner } from "../src/runner.ts";
import { ServerWorker, validateServerBinding, scopeStoreFolder } from "../src/server-worker.ts";
import { BrokerError } from "../src/broker-client.ts";
import { sharedCliEnvironment, sharedNativeArgs } from "../src/shared-runtime.ts";
import { AppServer } from "../src/rpc.ts";

const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const binding = (n = 1) => ({ version: 2 as const, transport: "unix-broker" as const,
  scope_ref: uuid(n), scope_kind: "project" as const, tenant_id: uuid(90), agent_id: uuid(n + 10),
  device_id: uuid(n + 20), epoch: 1, grant_generation: 1, policy_revision: "execution/default@1" });
const payload = { session_key: "same-session", input: "Read evidence", instructions: "Observe", tools: [] };
function home(t: any) {
  const path = mkdtempSync(join(tmpdir(), "lead-shared-"));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  return path;
}

class Rpc {
  listener: any; pendingListener: any; calls: any[] = []; responses: any[] = [];
  thread: string;
  constructor(thread = "thread-1") { this.thread = thread; }
  async open() {} async close() {}
  onNotification(fn: any) { this.listener = fn; }
  onRequest(fn: any) { this.pendingListener = fn; }
  respond(id: any, value: any) { this.responses.push({ id, value }); }
  async request(method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/start") return { thread: { id: this.thread } };
    if (method === "turn/start") {
      queueMicrotask(() => this.listener("turn/completed", { threadId: this.thread,
        turn: { id: "turn-1", status: "completed", items: [{ type: "agentMessage", text: "done" }] } }));
      return { turn: { id: "turn-1" } };
    }
    return {};
  }
}

test("server bindings reject secret fields, separate tenant/scope history and never rewrite personal stores", (t) => {
  const root = home(t), a = binding(), b = binding(2);
  assert.throws(() => validateServerBinding({ ...a, token: "PRIVATE_NODE_MARKER" }), /binding/);
  assert.throws(() => validateServerBinding({ ...a, scope_kind: "studio" }), /binding/);
  assert.throws(() => validateServerBinding({ ...a, agent_id: "../../escape" }), /binding/);
  const first = new Store(scopeStoreFolder(root, a)); first.bindServer(a);
  first.begin("same-id", payload); first.setThread("same-session", "original-thread");
  const second = new Store(scopeStoreFolder(root, b)); second.bindServer(b);
  second.begin("same-id", payload); second.setThread("same-session", "other-thread");
  assert.equal(new Store(first.folder).thread("same-session"), "original-thread");
  assert.equal(new Store(second.folder).thread("same-session"), "other-thread");
  assert.equal(new Store(first.folder).data.version, 2);
  assert.equal(readFileSync(join(first.folder, "state.json"), "utf8").includes("PRIVATE_NODE_MARKER"), false);
  assert.throws(() => first.bindServer({ ...a, device_id: uuid(888) }), /binding/);
  const personal = new Store(join(root, "personal"));
  personal.bind({ url: "http://localhost", device_id: "old", token: "personal-secret", epoch: 1, name: "old" });
  assert.throws(() => personal.bindServer(a), /binding/);
  assert.equal(new Store(personal.folder).data.binding?.token, "personal-secret");
});

test("shared runtime uses one complete flat profile and explicit CLI and shell environments", () => {
  const env = sharedCliEnvironment({ HOME: "/var/lib/lead-codex", CODEX_HOME: "/var/lib/lead-codex/.codex",
    PATH: "/usr/bin:/bin", HTTPS_PROXY: "http://synthetic-proxy", LEAD_NODE_TOKEN: "NODE_SECRET",
    CREDENTIALS_DIRECTORY: "/private/credentials", NODE_OPTIONS: "--require bad", LEAD_EXECUTOR_TOKEN: "DEVICE_SECRET",
    INTERNAL_TOKEN: "INTERNAL_SECRET", OPENAI_API_KEY: "API_SECRET" });
  assert.deepEqual(Object.keys(env).sort(), ["CODEX_HOME", "HOME", "HTTPS_PROXY", "PATH"]);
  const args = sharedNativeArgs({ home: "/var/lib/lead-codex", state: "/var/lib/lead-codex/.lead/server",
    codexHome: "/var/lib/lead-codex/.codex", workspace: "/var/lib/lead-codex/.lead/server/scope/workspaces/current" });
  const profile = args.find(value => value.startsWith("permissions.lead_shared="))!;
  assert.ok(profile.includes('":workspace_roots"="write"'));
  assert.ok(profile.includes('"/var/lib/lead-codex"="none"'));
  assert.ok(profile.includes('"/run/lead-codex-node"="none"'));
  assert.equal(profile.includes("filesystem.\""), false);
  assert.equal(args.includes("-P"), false); // AppServer selects the defined profile via RPC permissions.
  assert.ok(args.includes('default_permissions="lead_shared"'));
  assert.ok(args.includes('projects={"/var/lib/lead-codex/.lead/server/scope/workspaces/current"={trust_level="untrusted"}}'));
  assert.ok(args.includes('approval_policy="never"'));
  const shell = args.find(value => value.startsWith("shell_environment_policy="))!;
  assert.ok(shell.includes('inherit="none"'));
  assert.equal(shell.includes("HTTPS_PROXY"), false);
});

test("actual AppServer child receives only explicit CLI environment and fixed profile argv", async t => {
  const root = home(t), path = join(root, "fake-codex.mjs");
  writeFileSync(path, `import {createInterface} from 'node:readline';let initialized;
createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);if(m.method==='initialize')initialized=m.params;if(m.id!==undefined)
process.stdout.write(JSON.stringify({id:m.id,result:m.method==='probe/environment'?{env:process.env,args:process.argv.slice(2),initialized}:{}})+'\\n')});`);
  const roots = { home: root, state: join(root, "state"), codexHome: join(root, ".codex"), workspace: root };
  const native = sharedNativeArgs(roots), child = new AppServer(process.execPath, [path, ...native], root,
    sharedCliEnvironment({ HOME: root, PATH: "/usr/bin:/bin", CODEX_HOME: roots.codexHome,
      LEAD_NODE_TOKEN: "PRIVATE_NODE_MARKER", LEAD_EXECUTOR_TOKEN: "PRIVATE_DEVICE_MARKER",
      CREDENTIALS_DIRECTORY: "/run/credentials/private", NODE_OPTIONS: "untrusted", INTERNAL_TOKEN: "PRIVATE_INTERNAL_MARKER" }), true);
  await child.open(); t.after(() => child.close());
  const seen = await child.request("probe/environment");
  // macOS adds its own locale marker after exec; it is not inherited application data.
  if (process.platform === "darwin") delete seen.env.__CF_USER_TEXT_ENCODING;
  assert.deepEqual(seen.env, { HOME: root, PATH: "/usr/bin:/bin", CODEX_HOME: roots.codexHome });
  assert.equal(JSON.stringify(seen).includes("PRIVATE_"), false);
  assert.deepEqual(seen.args, [...native, "app-server", "--listen", "stdio://"]);
  assert.equal(seen.initialized.capabilities.experimentalApi, true);
});

test("shared Runner denies the complete worker state root even when it is outside HOME", t => {
  const root = home(t), store = new Store(join(root, "scopes", "one")); store.bindServer(binding());
  const runner = new Runner(store, undefined, { stateRoot: root });
  const rpc = runner.factory(payload, store.workspace(payload.session_key)) as AppServer;
  assert.ok(rpc.args.some(arg => arg.startsWith("permissions.lead_shared=") && arg.includes(JSON.stringify(root) + '=\"none\"')));
});

test("single worker keeps histories apart, fences only one failed scope and never reacquires it", async (t) => {
  const root = home(t), a = binding(), b = binding(2), calls: any[] = [], rpcs: Rpc[] = [];
  let failure = false, nodeFailure = false;
  const broker = { async call(method: string, params: any) {
    calls.push({ method, params });
    if (nodeFailure) throw new BrokerError("executor_node_unavailable", 401);
    if (method === "worker.hello") return { worker_generation: "generation-1" };
    if (method === "scope.list") return { scopes: [a, b] };
    if (method === "scope.poll") {
      if (failure && params.scope_ref === a.scope_ref) throw new BrokerError("executor_epoch_changed", 409);
      return { epoch: 1, commands: [{ id: "same-command", request_id: "same-request", kind: "run", payload }] };
    }
    return { accepted: params.events?.map((event: any) => event.id) || [] };
  } };
  const worker = new ServerWorker(root, broker, { capabilities: async () => ({ ready: true }),
    runnerFactory(store, client, bridge) {
      const rpc = new Rpc("thread-" + (rpcs.length + 1)); rpcs.push(rpc);
      return new Runner(store, () => rpc, { client, bridge });
    } });
  t.after(() => worker.close());
  await worker.tick();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(new Store(scopeStoreFolder(root, a)).thread("same-session"), "thread-1");
  assert.equal(new Store(scopeStoreFolder(root, b)).thread("same-session"), "thread-2");
  failure = true;
  await worker.tick();
  const failedPolls = calls.filter(c => c.method === "scope.poll" && c.params.scope_ref === a.scope_ref).length;
  await worker.tick();
  assert.equal(calls.filter(c => c.method === "scope.poll" && c.params.scope_ref === a.scope_ref).length, failedPolls);
  assert.ok(new Store(scopeStoreFolder(root, a)).outbox().length > 0);
  assert.ok(calls.filter(c => c.method === "scope.poll" && c.params.scope_ref === b.scope_ref).length >= 3);
  nodeFailure = true;
  await assert.rejects(worker.tick(), /executor_node_unavailable/);
  assert.equal(worker.stopped, true);
});

test("shared Runner refuses native permission expansion and forged webpage approval but retains user input", async (t) => {
  const store = new Store(home(t)); store.bindServer(binding());
  const rpc = new Rpc();
  rpc.request = async function(method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/start") return { thread: { id: "thread-1" } };
    if (method === "turn/start") return { turn: { id: "turn-1" } };
    return {};
  };
  const runner = new Runner(store, () => rpc);
  const work = runner.command({ id: "start", request_id: "request", kind: "run", payload });
  while (!rpc.pendingListener || !store.request("request")?.turn_id) await new Promise(resolve => setImmediate(resolve));
  rpc.pendingListener(1, "item/permissions/requestApproval", { permissions: { filesystem: { read: ["/"] } } });
  assert.deepEqual(rpc.responses.at(-1), { id: 1, value: { permissions: {}, scope: "turn" } });
  assert.equal(store.request("request")?.pending, undefined);
  store.update("request", { pending: { id: 1, method: "item/permissions/requestApproval", params: {} } });
  await assert.rejects(runner.command({ id: "forged", request_id: "request", kind: "respond",
    payload: { id: 1, result: { permissions: { filesystem: { read: ["/"] } } } } }), /shared_permissions_forbidden/);
  rpc.pendingListener(2, "item/tool/requestUserInput", { questions: [] });
  await runner.command({ id: "input", request_id: "request", kind: "respond", payload: { id: 2, result: { answers: {} } } });
  assert.deepEqual(rpc.responses.at(-1), { id: 2, value: { answers: {} } });
  await runner.close(); await work;
});

test("a new shared scope restores its original WAL/thread/turn without a second model turn", async t => {
  const store = new Store(home(t)); store.bindServer(binding());
  const record = store.begin("original", payload); record.command_ids.push("original-command");
  store.update("original", { state: "execution_unknown", thread_id: "original-thread", turn_id: "original-turn" });
  store.event("original", "progress", { retained: true });
  const restored = new Store(store.folder), rpc = new Rpc("original-thread");
  rpc.request = async function(method, params) {
    this.calls.push({ method, params });
    if (method === "thread/turns/list") return { data: [{ id: "original-turn", status: "completed",
      items: [{ type: "agentMessage", text: "original-result" }] }], nextCursor: null };
    return { thread: { id: "original-thread" } };
  };
  await new Runner(restored, () => rpc).command({ id: "original-command", request_id: "original", kind: "run", payload });
  assert.equal(restored.data.instance_id, store.data.instance_id);
  assert.equal(restored.request("original").thread_id, "original-thread");
  assert.equal(restored.request("original").turn_id, "original-turn");
  assert.equal(restored.request("original").state, "completed");
  assert.equal(rpc.calls.some(call => call.method === "turn/start"), false);
  assert.ok(restored.outbox().some(event => event.method === "progress" && event.params.retained));
});

test("every shared thread start/resume and turn explicitly fixes named permissions and disables provider fallback", async t => {
  const store = new Store(home(t)); store.bindServer(binding());
  const rpc = new Rpc(), runner = new Runner(store, () => rpc);
  await runner.command({ id: "first", request_id: "first", kind: "run", payload });
  await runner.command({ id: "second", request_id: "second", kind: "run", payload });
  const methods = rpc.calls.filter(call => ["thread/start", "thread/resume", "turn/start"].includes(call.method));
  assert.ok(methods.some(call => call.method === "thread/start"));
  assert.ok(methods.some(call => call.method === "thread/resume"));
  for (const call of methods) {
    assert.equal(call.params.permissions, "lead_shared");
    assert.equal(call.params.approvalPolicy, "never");
    assert.deepEqual(call.params.runtimeWorkspaceRoots, [store.workspace(payload.session_key)]);
    assert.equal("sandbox" in call.params || "sandboxPolicy" in call.params, false);
    if (call.method.startsWith("thread/")) assert.deepEqual(call.params.config.mcp_servers, {});
  }
  assert.equal(methods.find(call => call.method === "thread/start")!.params.allowProviderModelFallback, false);
});

test("fencing a shared Runner also prevents already queued same-session work starting", async t => {
  const store = new Store(home(t)); store.bindServer(binding());
  const rpcs: Rpc[] = [];
  const runner = new Runner(store, () => {
    const rpc = new Rpc();
    rpc.request = async function(method, params) { this.calls.push({ method, params });
      return method === "thread/start" ? { thread: { id: "thread-1" } } : { turn: { id: "turn-1" } }; };
    rpcs.push(rpc); return rpc;
  });
  const first = runner.command({ id: "first", request_id: "first", kind: "run", payload });
  while (!store.request("first")?.turn_id) await new Promise(resolve => setImmediate(resolve));
  const second = runner.command({ id: "second", request_id: "second", kind: "run", payload });
  await runner.close(); await Promise.all([first, second]);
  assert.equal(rpcs.length, 1);
  assert.equal(store.request("second").state, "prepared");
});

test("shared recovery waits for account readiness before starting a prepared request", async t => {
  const root = home(t), scope = binding();
  const store = new Store(scopeStoreFolder(root, scope)); store.bindServer(scope);
  store.begin("prepared-before-crash", payload);
  let ready = false;
  const rpcs: Rpc[] = [];
  const broker = { async call(method: string, params: any) {
    if (method === "worker.hello") return { worker_generation: "generation-ready" };
    if (method === "scope.list") return { scopes: [scope] };
    if (method === "scope.poll") return { epoch: 1, commands: [] };
    return { accepted: params.events?.map((event: any) => event.id) || [] };
  } };
  const worker = new ServerWorker(root, broker, { capabilities: async () => ({ ready }),
    runnerFactory(store, client, bridge) {
      const rpc = new Rpc(); rpcs.push(rpc);
      return new Runner(store, () => rpc, { client, bridge });
    } });
  t.after(() => worker.close());
  await worker.tick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rpcs[0].calls.some(call => call.method === "turn/start"), false);
  assert.equal(new Store(store.folder).request("prepared-before-crash").state, "prepared");
  ready = true; worker.lastCapabilities = 0;
  await worker.tick();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(rpcs[0].calls.filter(call => call.method === "turn/start").length, 1);
  await worker.tick();
  assert.equal(rpcs[0].calls.filter(call => call.method === "turn/start").length, 1);
});
