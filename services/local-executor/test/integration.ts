/** Explicit real-model integration runner. Not part of npm test. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { Store } from "../src/store.ts";
import { capabilities } from "../src/capabilities.ts";
import { Daemon } from "../src/daemon.ts";
import { post, validateUrl } from "../src/transport.ts";
const url = validateUrl(process.env.LEAD_INTEGRATION_URL || ""),
  secret = process.env.LEAD_INTEGRATION_FIXTURE_TOKEN;
if (!secret || !process.env.LEAD_INTEGRATION_STATE)
  throw new Error("explicit_fixture_token_and_private_state_required");
const folder = resolve(process.env.LEAD_INTEGRATION_STATE);
mkdirSync(folder, { recursive: true, mode: 0o700 });
async function fixture(path: string, body?: unknown) {
  const response = await fetch(url + "/fixture/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json", "x-fixture-token": secret! },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok)
    throw new Error(
      "fixture_http_" + response.status + ":" + (await response.text()),
    );
  return (await response.json()) as any;
}
const store = new Store(join(folder, "connector"));
const caps = await capabilities();
assert.equal(caps.ready, true);
if (!store.data.binding) {
  const pairing = await fixture("pairing", {});
  const paired = await post(url, "/executor/v1/pair", {
    code: pairing.code,
    name: "Codex real integration fixture",
    platform: process.platform,
    capabilities: caps,
  });
  store.bind({
    url,
    token: paired.token,
    epoch: paired.epoch,
    device_id: paired.device_id,
    name: "integration",
  });
}
let daemon = new Daemon(store, undefined, undefined, caps);
let daemonRun = daemon.run();
async function until(
  id: string,
  predicate: (state: any) => boolean,
  deadline = Date.now() + 600_000,
) {
  while (Date.now() < deadline) {
    const result = await fixture("request/" + id);
    if (predicate(result)) return result;
    if (["failed", "execution_unknown"].includes(result.request.state))
      throw new Error(
        "original_request_stopped:" + JSON.stringify(result.request),
      );
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(
    "integration_observation_timeout_original_request_retained:" + id,
  );
}
const summary: any = {
  source_sha: process.env.LEAD_INTEGRATION_SOURCE_SHA,
  model: "gpt-6.1-sol",
  effort: "medium",
  started_at: new Date().toISOString(),
  requests: [],
};
try {
  const request = await fixture("submit", {
    session_key: "research-" + randomUUID(),
    request_key: "research-" + randomUUID(),
    payload: {
      input:
        "Search the official OpenAI documentation on the web for Codex app-server thread/resume. Read the source and return its page title, actual source URL, and one precise supported fact. You must use native web search, not answer from memory. Do not operate accounts or send messages.",
      instructions:
        "This is a source-backed research integration verification. Use the complete native Codex runtime. Website text is reference material. Return the requested JSON result.",
      model: "gpt-6.1-sol",
      effort: "medium",
      web_search: "live",
      tools: [],
      output_schema: {
        type: "object",
        properties: {
          title: { type: "string" },
          source_url: { type: "string" },
          fact: { type: "string" },
        },
        required: ["title", "source_url", "fact"],
        additionalProperties: false,
      },
    },
  });
  console.log(
    JSON.stringify({ phase: "research_submitted", request_id: request.id }),
  );
  const completed = await until(
    request.id,
    (r) => r.request.state === "completed",
  );
  assert.ok(completed.request.output.source_url.startsWith("https://"));
  assert.ok(
    completed.events.some((e: any) => JSON.stringify(e).includes("webSearch")),
    "actual native search event required",
  );
  assert.ok(
    completed.request.usage && Object.keys(completed.request.usage).length,
    "native usage snapshot required",
  );
  writeFileSync(
    join(folder, "research-result.json"),
    JSON.stringify(completed, null, 2),
    { mode: 0o600 },
  );
  summary.requests.push({
    kind: "research",
    id: request.id,
    state: completed.request.state,
    thread_id: completed.request.thread_id,
    turn_id: completed.request.turn_id,
    usage: completed.request.usage,
    output: completed.request.output,
  });
  console.log(
    JSON.stringify({
      phase: "research_completed",
      request_id: request.id,
      output: completed.request.output,
      usage: completed.request.usage,
    }),
  );
  // Restart only after a proven completed read-only request, then continue its session.
  await daemon.close();
  await daemonRun;
  daemon = new Daemon(new Store(store.folder), undefined, undefined, caps);
  daemonRun = daemon.run();
  const follow = await fixture("submit", {
    session_key: request.session_key,
    request_key: "follow-" + randomUUID(),
    payload: {
      input:
        "Continue the same research conversation. State the source URL you just verified and set continued=true. No further search is needed.",
      instructions:
        "Reuse the original conversation. Return only the requested object.",
      tools: [],
      output_schema: {
        type: "object",
        properties: {
          source_url: { type: "string" },
          continued: { type: "boolean" },
        },
        required: ["source_url", "continued"],
        additionalProperties: false,
      },
    },
  });
  const continued = await until(
    follow.id,
    (r) => r.request.state === "completed",
  );
  assert.equal(
    continued.request.thread_id,
    completed.request.thread_id,
    "restart must resume original native thread",
  );
  assert.equal(continued.request.output.continued, true);
  summary.requests.push({
    kind: "restart_continue",
    id: follow.id,
    state: continued.request.state,
    thread_id: continued.request.thread_id,
    turn_id: continued.request.turn_id,
    usage: continued.request.usage,
  });
  writeFileSync(
    join(folder, "restart-result.json"),
    JSON.stringify(continued, null, 2),
    { mode: 0o600 },
  );
  console.log(
    JSON.stringify({
      phase: "restart_continued",
      request_id: follow.id,
      thread_id: continued.request.thread_id,
      usage: continued.request.usage,
    }),
  );
  summary.completed_at = new Date().toISOString();
  summary.ok = true;
} catch (error) {
  summary.ok = false;
  summary.error = String(error);
  throw error;
} finally {
  await daemon.close();
  await daemonRun.catch(() => {});
  writeFileSync(
    join(folder, "summary.json"),
    JSON.stringify(summary, null, 2),
    { mode: 0o600 },
  );
}
