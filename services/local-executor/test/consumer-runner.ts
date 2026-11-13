/** Opt-in original-product consumer verification; no social actions. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { Store } from "../src/store.ts";
import { capabilities } from "../src/capabilities.ts";
import { Daemon } from "../src/daemon.ts";
import { post, validateUrl } from "../src/transport.ts";
const url = validateUrl(process.env.LEAD_CONSUMER_URL || ""),
  secret = process.env.LEAD_CONSUMER_TOKEN;
const phase = process.argv[2];
if (
  !secret ||
  !process.env.LEAD_CONSUMER_STATE ||
  !["website", "decision", "content", "recall"].includes(phase)
)
  throw new Error("explicit_disposable_consumer_fixture_required");
const folder = resolve(process.env.LEAD_CONSUMER_STATE);
mkdirSync(folder, { recursive: true, mode: 0o700 });
async function fixture(path: string, body?: unknown) {
  const r = await fetch(url + "/fixture/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json", "x-fixture-token": secret! },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!r.ok)
    throw new Error("fixture_http_" + r.status + ":" + (await r.text()));
  return (await r.json()) as any;
}
const caps = await capabilities();
assert.equal(caps.ready, true);
const store = new Store(join(folder, "connector"));
if (!store.data.binding) {
  const invite = await fixture("pairing", {}),
    binding = await post(url, "/executor/v1/pair", {
      code: invite.code,
      name: "Actual product consumer verification",
      platform: process.platform,
      capabilities: caps,
    });
  store.bind({
    url,
    token: binding.token,
    device_id: binding.device_id,
    epoch: binding.epoch,
    name: "consumer-verification",
  });
}
const daemon = new Daemon(store, undefined, undefined, caps),
  running = daemon.run();
try {
  await fixture("start/" + phase, {});
  console.log(
    JSON.stringify({
      phase,
      state: "started",
      source_sha: process.env.LEAD_CONSUMER_SOURCE_SHA,
    }),
  );
  const deadline = Date.now() + 900_000;
  while (Date.now() < deadline) {
    const current = await fixture("status/" + phase);
    writeFileSync(
      join(folder, phase + "-result.json"),
      JSON.stringify(current, null, 2),
      { mode: 0o600 },
    );
    if (current.job?.state === "completed") {
      assert.equal(current.billing.reservations, 0);
      assert.equal(current.billing.usage_events, 0);
      if (phase === "recall") {
        const recall = current.requests.find((row: any) =>
          row.request_key.startsWith("consumer-recall:"),
        );
        const website = current.requests.find((row: any) =>
          row.request_key.startsWith("consumer-website:"),
        );
        assert.ok(recall && website);
        assert.equal(recall.thread_id, website.thread_id);
        const nonConversationItems = current.events.filter((event: any) =>
          event.request_id === recall.id && event.method === "item/started" &&
          !["userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"].includes(event.params.item?.type),
        );
        assert.deepEqual(nonConversationItems, [], "recall must use the compacted conversation without browsing or tools");
      }
      const usageByThread = new Map<string, any>();
      for (const row of current.requests)
        if (row.thread_id && row.usage)
          usageByThread.set(row.thread_id, row.usage.total);
      console.log(
        JSON.stringify({
          phase,
          state: "completed",
          output: current.job.output,
          request_count: current.requests.length,
          billing: current.billing,
          usage_by_thread: Object.fromEntries(usageByThread),
        }),
      );
      break;
    }
    if (
      current.job?.state === "failed" ||
      current.requests.some((row: any) =>
        ["failed", "execution_unknown"].includes(row.state),
      )
    )
      throw new Error(
        "original_consumer_stopped:" +
          JSON.stringify({
            job: current.job,
            requests: current.requests,
            native_errors: current.events.filter((event: any) =>
              event.method === "error" ||
              (event.method === "turn/completed" && event.params?.turn?.error),
            ),
          }),
      );
    await new Promise((r) => setTimeout(r, 1000));
  }
  const final = await fixture("status/" + phase);
  assert.equal(
    final.job.state,
    "completed",
    "observation timeout; original requests retained",
  );
} finally {
  await daemon.close();
  await running.catch(() => {});
}
