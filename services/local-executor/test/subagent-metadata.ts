/** Read only the exact native threads created by the isolated child-agent proof. */
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { AppServer } from "../src/rpc.ts";
import { nativeArgs } from "../src/runner.ts";

if (!process.env.LEAD_CONSUMER_STATE)
  throw new Error("explicit_disposable_consumer_fixture_required");
const folder = resolve(process.env.LEAD_CONSUMER_STATE),
  result = JSON.parse(readFileSync(join(folder, "subagent-result.json"), "utf8"));
assert.equal(result.job?.state, "completed");
const request = result.requests.find((row: any) => row.request_key.startsWith("consumer-subagent:"));
assert.equal(request.state, "completed");
const spawns = result.events.filter((event: any) => event.request_id === request.id &&
  event.method === "item/completed" && event.params.item?.type === "subAgentActivity" &&
  event.params.item?.kind === "started");
assert.equal(spawns.length, 1, "exactly one native child must actually start");
const spawn = spawns[0].params.item,
  childId = spawn.agentThreadId;
assert.equal(spawns[0].params.threadId, request.thread_id);
assert.ok(result.job.output.child_thread_id.includes(childId) ||
  result.job.output.child_thread_id.includes(spawn.agentPath));
assert.equal(spawn.model, "gpt-6.1-sol");
assert.equal(spawn.reasoningEffort, "medium");
assert.equal(result.events.filter((event: any) => event.request_id === request.id &&
  event.method === "item/completed" && event.params.item?.type === "subAgentActivity" &&
  event.params.item?.kind === "completed" && event.params.item?.agentThreadId === childId).length, 1);
const rpc = new AppServer(process.env.LEAD_CODEX_BINARY || "codex", nativeArgs(false));
const metadata: any[] = [];
try {
  await rpc.open();
  for (const id of [request.thread_id, childId]) {
    const { thread } = await rpc.request("thread/read", { threadId: id, includeTurns: true });
    assert.equal(thread.id, id);
    assert.equal(thread.modelProvider, "openai");
    assert.equal(thread.model, "gpt-6.1-sol");
    assert.equal(thread.reasoningEffort, "medium");
    if (id === childId) assert.equal(thread.parentThreadId, request.thread_id);
    assert.ok(typeof thread.path === "string" && thread.path.includes(id));
    const records = readFileSync(thread.path, "utf8").split("\n").filter(Boolean).map(line => JSON.parse(line));
    const contexts = records.filter(record => record.type === "turn_context").map(record => ({
      turn_id: record.payload.turn_id,
      model: record.payload.model,
      effort: record.payload.effort,
    }));
    assert.ok(contexts.length > 0, "actual per-turn metadata must exist");
    for (const context of contexts) {
      assert.equal(context.model, "gpt-6.1-sol");
      assert.equal(context.effort, "medium");
    }
    const tokenSnapshots = records.filter(record => record.type === "event_msg" &&
      record.payload?.type === "token_count").map(record => record.payload.info).filter(Boolean);
    const itemTypes = thread.turns.flatMap((turn: any) => turn.items.map((item: any) => item.type));
    assert.ok(!itemTypes.includes("webSearch"), "the proof must not browse");
    const commands = thread.turns.flatMap((turn: any) => turn.items
      .filter((item: any) => item.type === "commandExecution")
      .map((item: any) => ({ command: item.command, status: item.status, exit_code: item.exitCode })));
    if (id === childId) {
      assert.equal(commands.length, 1);
      assert.match(commands[0].command, /^\/bin\/(?:zsh|bash) -lc 'cat native-subagent-fixture\.txt'$/);
      assert.equal(commands[0].status, "completed");
      assert.equal(commands[0].exit_code, 0);
    } else assert.deepEqual(commands, [], "the parent must delegate the read");
    metadata.push({
      id, parent_thread_id: thread.parentThreadId, model: thread.model,
      reasoning_effort: thread.reasoningEffort, model_provider: thread.modelProvider,
      contexts, item_types: itemTypes, commands, latest_native_token_snapshot: tokenSnapshots.at(-1),
    });
  }
} finally {
  await rpc.close();
}
const proof = {
  source_sha: process.env.LEAD_CONSUMER_SOURCE_SHA,
  request_id: request.id,
  backend_primary_thread_id: request.thread_id,
  backend_primary_turn_id: request.turn_id,
  spawn,
  threads: metadata,
  monetary_billing: "unknown; per-thread native snapshots are not a monetary receipt",
};
writeFileSync(join(folder, "subagent-native-metadata.json"), JSON.stringify(proof, null, 2), { mode: 0o600 });
console.log(JSON.stringify(proof));
