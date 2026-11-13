/** Real native steering/interruption of a read-only fixture task; opt-in only. */
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { randomUUID } from "node:crypto";
import { Store } from "../src/store.ts";
import { capabilities } from "../src/capabilities.ts";
import { Daemon } from "../src/daemon.ts";
import { validateUrl } from "../src/transport.ts";
const url = validateUrl(process.env.LEAD_INTEGRATION_URL || ""),
  secret = process.env.LEAD_INTEGRATION_FIXTURE_TOKEN;
if (!secret || !process.env.LEAD_INTEGRATION_STATE)
  throw new Error("explicit_disposable_fixture_required");
const folder = resolve(process.env.LEAD_INTEGRATION_STATE);
async function fixture(path: string, body?: unknown) {
  const response = await fetch(url + "/fixture/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json", "x-fixture-token": secret! },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error("fixture_http_" + response.status);
  return (await response.json()) as any;
}
const store = new Store(join(folder, "connector")),
  caps = await capabilities();
assert.equal(caps.ready, true);
const daemon = new Daemon(store, undefined, undefined, caps),
  running = daemon.run();
const summary: any = {
  source_sha: process.env.LEAD_INTEGRATION_SOURCE_SHA,
  model: "gpt-6.1-sol",
  effort: "medium",
  started_at: new Date().toISOString(),
};
async function until(id: string, predicate: (state: any) => boolean) {
  const deadline = Date.now() + 600_000;
  while (Date.now() < deadline) {
    const state = await fixture("request/" + id);
    if (predicate(state)) return state;
    if (
      ["failed", "execution_unknown", "completed", "interrupted"].includes(
        state.request.state,
      )
    )
      throw new Error(
        "original_request_stopped:" + JSON.stringify(state.request),
      );
    if (state.request.state === "waiting_input")
      throw new Error("manual_input_required_original_request_retained:" + id);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("observation_timeout_original_request_retained:" + id);
}
try {
  const request = await fixture("submit", {
    session_key: "control-" + randomUUID(),
    request_key: "control-" + randomUUID(),
    payload: {
      input:
        'This is an integration test of interruption. Use the terminal to run exactly: python3 -c "import time; time.sleep(30)". This only waits and writes nothing. After it finishes, return {"finished":true}. Do not read or modify files, browse, or contact any service.',
      instructions:
        "Use the native terminal for the exact harmless wait command. Remain available for user steering and interruption.",
      tools: [],
      web_search: "disabled",
      output_schema: {
        type: "object",
        properties: { finished: { type: "boolean" } },
        required: ["finished"],
        additionalProperties: false,
      },
    },
  });
  summary.request_id = request.id;
  console.log(
    JSON.stringify({ phase: "control_submitted", request_id: request.id }),
  );
  await until(request.id, (r) =>
    r.events.some(
      (e: any) =>
        e.method === "item/started" &&
        e.params.item?.type === "commandExecution",
    ),
  );
  const steer = await fixture("request/" + request.id + "/command", {
    kind: "steer",
    payload: {
      input:
        "Update the pending answer to include the fact that the user added this requirement. Keep the current wait command; do not start a replacement command.",
    },
  });
  await until(request.id, (r) =>
    r.events.some(
      (e: any) =>
        e.method === "command/ack" && e.params.command_id === steer.id,
    ),
  );
  const interrupt = await fixture("request/" + request.id + "/command", {
    kind: "interrupt",
    payload: {},
  });
  const result = await until(
    request.id,
    (r) => r.request.state === "interrupted",
  );
  assert.ok(
    result.events.some(
      (e: any) =>
        e.method === "turn/completed" &&
        e.params.turn?.status === "interrupted",
    ),
  );
  assert.ok(!result.events.some((e: any) => e.method === "control/unknown"));
  summary.ok = true;
  summary.state = result.request.state;
  summary.thread_id = result.request.thread_id;
  summary.turn_id = result.request.turn_id;
  summary.usage = result.request.usage;
  summary.steer_command = steer.id;
  summary.interrupt_command = interrupt.id;
  summary.completed_at = new Date().toISOString();
  writeFileSync(
    join(folder, "control-result.json"),
    JSON.stringify(result, null, 2),
    { mode: 0o600 },
  );
  console.log(JSON.stringify({ phase: "steered_and_interrupted", ...summary }));
} catch (error) {
  summary.ok = false;
  summary.error = String(error);
  throw error;
} finally {
  await daemon.close();
  await running.catch(() => {});
  writeFileSync(
    join(folder, "control-summary.json"),
    JSON.stringify(summary, null, 2),
    { mode: 0o600 },
  );
}
