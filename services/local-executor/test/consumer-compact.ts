/** Opt-in official native compaction of this fixture's completed website thread. */
import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { AppServer } from "../src/rpc.ts";
import { EFFORT, MODEL, nativeArgs } from "../src/runner.ts";
import { Store } from "../src/store.ts";

if (!process.env.LEAD_CONSUMER_STATE)
  throw new Error("explicit_disposable_consumer_fixture_required");
const folder = resolve(process.env.LEAD_CONSUMER_STATE),
  website = JSON.parse(readFileSync(join(folder, "website-result.json"), "utf8"));
assert.ok(!existsSync(join(folder, "compact-result.json")), "original compaction intent already exists; inspect it before any new operation");
assert.equal(website.job?.state, "completed");
const session = "consumer-website:" + website.agent_id,
  store = new Store(join(folder, "connector")),
  thread = store.thread(session);
assert.ok(thread);
assert.equal(
  website.requests.find((row: any) => row.session_key === session)?.thread_id,
  thread,
);
const events: { method: string; params: any }[] = [];
const evidence: any = {
  source_sha: process.env.LEAD_CONSUMER_SOURCE_SHA,
  thread_id: thread,
  method: "thread/compact/start",
  state: "preparing",
  events,
};
function save() {
  writeFileSync(join(folder, "compact-result.json"), JSON.stringify(evidence, null, 2), {
    mode: 0o600,
  });
}
const rpc = new AppServer(
  process.env.LEAD_CODEX_BINARY || "codex",
  nativeArgs(false),
  store.workspace(session),
);
let compactTurn: string | undefined;
let compacted = false;
let completed = false;
let failure: Error | undefined;
rpc.onNotification((method, params) => {
  if (method === "connection/closed" && evidence.state !== "completed") {
    failure = new Error("native_compaction_connection_closed; outcome may be unknown");
    return;
  }
  if (!/^(thread\/|turn\/|item\/|error$)/.test(method)) return;
  events.push({ method, params });
  if (method === "error") failure = new Error(JSON.stringify(params));
  if (params.threadId === thread) {
    if (
      method === "thread/compacted" ||
      (method === "item/completed" && params.item?.type === "contextCompaction")
    ) {
      compacted = true;
      compactTurn = params.turnId;
    }
    if (method === "turn/completed") {
      if (params.turn?.status !== "completed")
        failure = new Error("native_compaction_turn_" + params.turn?.status);
      if (!compactTurn || params.turn?.id === compactTurn) completed = true;
    }
  }
  save();
});
rpc.onRequest((id, method, params) => {
  evidence.server_request = { id, method, params };
  failure = new Error("native_compaction_requires_user_response");
  save();
});
try {
  await rpc.open();
  await rpc.request("thread/resume", {
    threadId: thread,
    cwd: store.workspace(session),
    model: MODEL,
    config: { model_reasoning_effort: EFFORT, web_search: "disabled" },
    excludeTurns: true,
  });
  evidence.state = "starting";
  save();
  // Persist the intent before this single call; never replay an ambiguous ACK.
  evidence.ack = await rpc.request("thread/compact/start", { threadId: thread });
  evidence.state = "running";
  save();
  const deadline = Date.now() + 900_000;
  while (!(compacted && completed) && !failure && Date.now() < deadline)
    await new Promise((resolve) => setTimeout(resolve, 1000));
  if (failure) throw failure;
  assert.ok(compacted && completed, "observation timeout; original compaction retained");
  const snapshot = await rpc.request("thread/read", { threadId: thread, includeTurns: true });
  evidence.turns = snapshot.thread.turns;
  evidence.state = "completed";
  evidence.compaction_turn_id = compactTurn;
  save();
  console.log(JSON.stringify({
    state: evidence.state,
    thread_id: thread,
    compaction_turn_id: compactTurn,
    compact_items: events.filter((event) =>
      event.method === "item/completed" && event.params.item?.type === "contextCompaction",
    ).length,
    usage_snapshots: events.filter((event) => event.method === "thread/tokenUsage/updated")
      .map((event) => event.params.tokenUsage),
  }));
} catch (error) {
  evidence.state = "failed_or_unknown";
  evidence.error = String(error);
  save();
  throw error;
} finally {
  await rpc.close();
}
