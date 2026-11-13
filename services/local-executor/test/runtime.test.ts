import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Store } from "../src/store.ts";
import { Runner } from "../src/runner.ts";

class FakeRpc {
  calls: any[] = [];
  listener: any;
  pendingListener: any;
  async open() {}
  async close() {}
  onNotification(fn: any) {
    this.listener = fn;
  }
  onRequest(fn: any) {
    this.pendingListener = fn;
  }
  async request(method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/start") return { thread: { id: "thread-1" } };
    if (method === "thread/resume")
      return { thread: { id: "thread-1", status: { type: "idle" } } };
    if (method === "thread/turns/list") return { data: [], nextCursor: null };
    if (method === "turn/start") {
      setTimeout(
        () =>
          this.listener("item/completed", {
            threadId: "thread-1",
            turnId: "turn-1",
            item: { type: "agentMessage", text: '{"ok":true}' },
          }),
        1,
      );
      setTimeout(
        () =>
          this.listener("turn/completed", {
            threadId: "thread-1",
            turn: { id: "turn-1", status: "completed", items: [] },
          }),
        2,
      );
      return { turn: { id: "turn-1" } };
    }
    return {};
  }
  respond() {}
}
const payload = {
  session_key: "same-conversation",
  input: "Research this product",
  instructions: "Use actual evidence",
  output_schema: {
    type: "object",
    properties: { ok: { type: "boolean" } },
    required: ["ok"],
    additionalProperties: false,
  },
  model: "gpt-6.1-sol",
  effort: "medium",
  tools: [],
};
function setup(t: any) {
  const folder = mkdtempSync(join(tmpdir(), "lead-connector-test-"));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  return new Store(folder);
}
test("interrupting a queued request never starts its model turn", async (t) => {
  const store = setup(t), rpc = new FakeRpc();
  const runner = new Runner(store, () => rpc as any);
  const work = runner.command({ id: "queued", request_id: "cancel-before-start", kind: "run", payload });
  await runner.command({ id: "cancel", request_id: "cancel-before-start", kind: "interrupt", payload: {} });
  await work;
  assert.equal(store.request("cancel-before-start")?.state, "interrupted");
  assert.equal(rpc.calls.filter(c => c.method === "turn/start").length, 0);
});
test("an offline interrupt survives restart and cancels only the reconciled original turn", async (t) => {
  const store = setup(t), rpc = new FakeRpc();
  store.begin("offline-cancel", payload);
  store.update("offline-cancel", { thread_id: "thread-1", turn_id: "original-turn", state: "running" });
  await new Runner(store).command({ id: "cancel-offline", request_id: "offline-cancel", kind: "interrupt", payload: {} });
  rpc.request = async function(method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/turns/list") return { data: [{ id: "original-turn", status: "inProgress", items: [] }], nextCursor: null };
    if (method === "turn/interrupt") this.listener("turn/completed", {
      threadId: "thread-1", turn: { id: "original-turn", status: "interrupted", items: [] },
    });
    return {};
  };
  const restored = new Store(store.folder), runner = new Runner(restored, () => rpc as any);
  const work = runner.command({ id: "resume", request_id: "offline-cancel", kind: "run", payload });
  await Promise.race([work, new Promise(resolve => setTimeout(resolve, 100))]);
  await runner.close();
  await work;
  const interrupts = rpc.calls.filter(c => c.method === "turn/interrupt");
  assert.equal(interrupts.length, 1);
  assert.equal(interrupts[0].params.turnId, "original-turn");
  assert.equal(rpc.calls.filter(c => c.method === "turn/start").length, 0);
  assert.equal(restored.request("offline-cancel")?.state, "interrupted");
});
test("run persists original thread, explicit Sol medium and parsed result; replay does not run twice", async (t) => {
  const store = setup(t),
    rpc = new FakeRpc();
  const runner = new Runner(store, () => rpc as any);
  await runner.command({ id: "c1", request_id: "r1", kind: "run", payload });
  assert.equal(rpc.calls.filter((c) => c.method === "turn/start").length, 1);
  assert.equal(
    rpc.calls.find((c) => c.method === "turn/start").params.effort,
    "medium",
  );
  assert.equal(
    rpc.calls.find((c) => c.method === "turn/start").params.model,
    "gpt-6.1-sol",
  );
  assert.equal(store.request("r1")?.thread_id, "thread-1");
  assert.deepEqual(store.request("r1")?.output, { ok: true });
  await runner.command({ id: "c1", request_id: "r1", kind: "run", payload });
  assert.equal(rpc.calls.filter((c) => c.method === "turn/start").length, 1);
});
test("lost start acknowledgment is recorded unknown, never a fresh turn", async (t) => {
  const store = setup(t),
    rpc = new FakeRpc();
  rpc.request = async function (method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/start") return { thread: { id: "thread-1" } };
    if (method === "turn/start") throw new Error("pipe closed");
    return { data: [], nextCursor: null };
  };
  await new Runner(store, () => rpc as any).command({
    id: "c2",
    request_id: "r2",
    kind: "run",
    payload,
  });
  assert.equal(store.request("r2")?.state, "execution_unknown");
  await new Runner(new Store(store.folder), () => rpc as any).command({
    id: "c2",
    request_id: "r2",
    kind: "run",
    payload,
  });
  assert.equal(rpc.calls.filter((c) => c.method === "turn/start").length, 1);
});
test("durable events are replayed until acknowledged and request identity conflict rejected", (t) => {
  const store = setup(t);
  store.begin("r", payload);
  store.event("r", "progress", { value: 1 });
  const second = new Store(store.folder);
  assert.equal(second.outbox().length, 1);
  assert.throws(
    () => second.begin("r", { ...payload, input: "different" }),
    /identity_conflict/,
  );
  second.ack(second.outbox().map((e) => e.id));
  assert.equal(new Store(store.folder).outbox().length, 0);
});
test("session key cannot control workspace path", (t) => {
  const store = setup(t);
  assert.ok(
    store
      .workspace("../../outside")
      .startsWith(join(store.folder, "workspaces")),
  );
});
test("restart reconciles an unknown original turn by marker without starting another turn", async (t) => {
  const store = setup(t);
  const record = store.begin("lost-ack", payload);
  store.update(record.id, {
    thread_id: "thread-1",
    state: "execution_unknown",
  });
  const rpc = new FakeRpc();
  rpc.request = async function (method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/turns/list")
      return {
        data: [
          {
            id: "original-turn",
            status: "completed",
            items: [
              {
                type: "userMessage",
                content: [{ text: "[lead_request:lost-ack]" }],
              },
              { type: "agentMessage", text: '{"ok":true}' },
            ],
          },
        ],
        nextCursor: null,
      };
    return { thread: { id: "thread-1" } };
  };
  await new Runner(store, () => rpc as any).command({
    id: "original-command",
    request_id: "lost-ack",
    kind: "run",
    payload,
  });
  assert.equal(store.request("lost-ack")?.state, "completed");
  assert.equal(store.request("lost-ack")?.turn_id, "original-turn");
  assert.equal(rpc.calls.filter((c) => c.method === "turn/start").length, 0);
});
test("approval requests wait for matching user response and steering names the current turn", async (t) => {
  const store = setup(t),
    rpc = new FakeRpc(),
    responses: any[] = [];
  rpc.respond = (id: any, value: any) => {
    responses.push({ id, value });
  };
  rpc.request = async function (method: string, params: any) {
    this.calls.push({ method, params });
    if (method === "thread/start") return { thread: { id: "thread-1" } };
    if (method === "turn/start") {
      setTimeout(
        () =>
          this.pendingListener(31, "item/commandExecution/requestApproval", {
            threadId: "thread-1",
            turnId: "turn-1",
          }),
        1,
      );
      return { turn: { id: "turn-1" } };
    }
    return {};
  };
  const runner = new Runner(store, () => rpc as any),
    run = runner.command({
      id: "c",
      request_id: "approval",
      kind: "run",
      payload,
    });
  while (!store.request("approval")?.pending)
    await new Promise((r) => setImmediate(r));
  assert.equal(responses.length, 0);
  assert.equal(store.request("approval")?.state, "waiting_user");
  await runner.command({
    id: "steer",
    request_id: "approval",
    kind: "steer",
    payload: { input: "Use latest instructions" },
  });
  assert.equal(
    rpc.calls.find((c) => c.method === "turn/steer").params.expectedTurnId,
    "turn-1",
  );
  await runner.command({
    id: "respond",
    request_id: "approval",
    kind: "respond",
    payload: { id: 31, result: { decision: "decline" } },
  });
  assert.deepEqual(responses, [{ id: 31, value: { decision: "decline" } }]);
  rpc.listener("turn/completed", {
    threadId: "thread-1",
    turn: { id: "turn-1", status: "interrupted", items: [] },
  });
  await run;
  assert.equal(store.request("approval")?.state, "interrupted");
});
test("request-level disabled search is preserved when loading the native thread", async (t) => {
  const store = setup(t),
    rpc = new FakeRpc();
  await new Runner(store, () => rpc as any).command({
    id: "search-off",
    request_id: "search-off",
    kind: "run",
    payload: { ...payload, web_search: "disabled" },
  });
  assert.equal(
    rpc.calls.find((c) => c.method === "thread/start").params.config.web_search,
    "disabled",
  );
});
