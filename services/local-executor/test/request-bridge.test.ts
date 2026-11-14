import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:net";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { Store } from "../src/store.ts";
import { RequestBridge, RequestBridgeClient } from "../src/request-bridge.ts";
import { BrokerError, ScopeClient, UnixRpc } from "../src/broker-client.ts";
import { Daemon } from "../src/daemon.ts";

function setup(t: any) {
  const root = mkdtempSync(join(tmpdir(), "lead-rb-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const store = new Store(join(root, "s"));
  store.bindServer({ version: 2, transport: "unix-broker", scope_ref: randomUUID(), scope_kind: "studio",
    tenant_id: randomUUID(), studio_id: randomUUID(), epoch: 4, grant_generation: 2, policy_revision: "execution/default@1" });
  store.begin("request-1", { session_key: "studio-session", input: "read", instructions: "read",
    tools: [{ name: "lead_public_read", description: "read source", inputSchema: { type: "object" } }] });
  return { root, store };
}

test("Unix broker client fixes scope/fences, accepts native frames and redacts remote error text", async t => {
  const { root, store } = setup(t), frames: any[] = [];
  const socket = createServer(peer => {
    peer.setEncoding("utf8"); let buffer = "";
    peer.on("data", chunk => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const frame = JSON.parse(buffer.split("\n")[0]); frames.push(frame);
      peer.end(JSON.stringify({ v: 1, id: frame.id, ...(frame.method === "request.tools" ?
        { error: { code: "SECRET https://private.invalid/token", status: 409, message: "PRIVATE_NODE_MARKER" } } : { result: { epoch: 4, commands: [] } }) }) + "\n");
    });
  });
  const path = join(root, "b.sock"); await new Promise<void>(resolve => socket.listen(path, resolve));
  t.after(() => socket.close());
  const client = new ScopeClient(store.data.server_binding!, new UnixRpc(path), "pid:invocation");
  await client.call("poll", { instance_id: store.data.instance_id, capabilities: { ready: true }, scope_ref: "attacker", epoch: 999 });
  assert.deepEqual(frames[0].params, { instance_id: store.data.instance_id, worker_generation: "pid:invocation",
    scope_ref: store.data.server_binding!.scope_ref, grant_generation: 2, epoch: 4 });
  await assert.rejects(client.call("tools", { request_id: "request-1", name: "lead_public_read", arguments: {} }), error =>
    error instanceof BrokerError && error.status === 409 && !String(error).includes("SECRET") && !String(error).includes("PRIVATE_NODE_MARKER"));
  await assert.rejects(client.call("register", {}), /method_forbidden/);
});

test("request ticket cannot select another request/tool/scope and expiry applies only to handshake", async t => {
  const { root, store } = setup(t); let now = 1_000; const calls: any[] = [];
  const bridge = new RequestBridge(join(root, "r.sock"), "generation", () => now);
  await bridge.start(); t.after(() => bridge.close());
  const lease = bridge.issue(store, "request-1", { async call(method, body) {
    calls.push({ method, body }); return { content: [{ type: "text", text: "observed" }] };
  } });
  const env = lease.env;
  const client = new RequestBridgeClient(env.LEAD_REQUEST_BRIDGE, env.LEAD_REQUEST_TICKET, "request-1");
  await client.open(); t.after(() => client.close());
  assert.equal(client.tools[0].name, "lead_public_read");
  now += 100_000;
  await client.call("request.tools", { name: "lead_public_read", arguments: { url: "https://example.invalid" } });
  assert.equal(calls[0].body.request_id, "request-1");
  await assert.rejects(client.call("scope.list", {}), /forbidden/);
  await assert.rejects(client.call("request.tools", { request_id: "other", name: "lead_public_read", arguments: {} }), /forbidden/);
  await assert.rejects(client.call("request.tools", { name: "browser_input", arguments: {} }), /forbidden/);
  const expired = new RequestBridgeClient(env.LEAD_REQUEST_BRIDGE, env.LEAD_REQUEST_TICKET, "request-1");
  await assert.rejects(expired.open(), /unauthorized/); expired.close();
  const other = bridge.issue(store, "request-1", { async call() { throw new Error("not reachable"); } });
  const wrong = new RequestBridgeClient(other.env.LEAD_REQUEST_BRIDGE, other.env.LEAD_REQUEST_TICKET, "foreign-request");
  await assert.rejects(wrong.open(), /unauthorized/); wrong.close();
  assert.equal(readFileSync(join(store.folder, "state.json"), "utf8").includes(env.LEAD_REQUEST_TICKET), false);
  bridge.generation = "replacement";
  await assert.rejects(client.call("request.tools", { name: "lead_public_read", arguments: {} }), /forbidden/);
  lease.close(); other.close();
});

test("official MCP server-mode uses the request bridge without reading a bearer Store", async t => {
  const { root, store } = setup(t), calls: any[] = [];
  const bridge = new RequestBridge(join(root, "r.sock"), "generation");
  await bridge.start(); t.after(() => bridge.close());
  const lease = bridge.issue(store, "request-1", { async call(method, body) {
    calls.push({ method, body }); return { content: [{ type: "image", mimeType: "image/png", data: "aW1hZ2U=" }] };
  } });
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [fileURLToPath(new URL("../src/mcp.ts", import.meta.url))], env: lease.env, stderr: "pipe" });
  const client = new Client({ name: "bridge-test", version: "1" });
  await client.connect(transport); t.after(() => client.close());
  assert.equal((await client.listTools()).tools.length, 1);
  const result: any = await client.callTool({ name: "lead_public_read", arguments: {} });
  assert.equal(result.content[0].type, "image"); assert.equal(calls[0].body.request_id, "request-1");
  await assert.rejects(client.callTool({ name: "undeclared", arguments: {} }));
  assert.equal(calls.length, 1);
  await client.close(); lease.close();
});

test("shared instance survives restart, directory cannot replace its fence and poll cannot raise its epoch", async t => {
  const { store } = setup(t), before = store.data.server_binding!;
  store.event("request-1", "progress", {});
  const restored = new Store(store.folder);
  assert.equal(restored.data.instance_id, store.data.instance_id);
  assert.throws(() => restored.bindServer({ ...before, epoch: 5 }), /binding_conflict/);
  assert.equal(new Store(store.folder).data.server_binding!.epoch, 4);
  const sent: any[] = [];
  const daemon = new Daemon(restored, { async command() {}, async close() {} } as any,
    { async call(method, body) { sent.push({ method, body }); return { epoch: 5, commands: [] }; } });
  await assert.rejects(daemon.tick(), /epoch_changed/);
  assert.equal(sent.length, 1); assert.equal(sent[0].body.instance_id, store.data.instance_id);
  assert.equal(restored.data.server_binding!.epoch, 4); assert.equal(restored.outbox().length, 1);
});

test("an in-flight poll cannot dispatch after its scope has been fenced", async t => {
  const { store } = setup(t); let resolve!: (value: any) => void, count = 0;
  const daemon = new Daemon(store, { async command() { count++; }, async close() {} } as any,
    { async call() { return await new Promise<any>(done => { resolve = done; }); } });
  const tick = daemon.tick();
  await daemon.close();
  resolve({ epoch: 4, commands: [{ id: "late", request_id: "request-1", kind: "run", payload: {} }] });
  await tick; assert.equal(count, 0);
});
