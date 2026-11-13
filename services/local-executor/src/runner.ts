import { fileURLToPath } from "node:url";
import { AppServer } from "./rpc.ts";
import { Store } from "./store.ts";
import type {
  Command,
  Dict,
  RequestRecord,
  RpcLike,
  RunPayload,
} from "./types.ts";
export const MODEL = "gpt-6.1-sol",
  EFFORT = "medium";
const terminal = new Set([
  "completed",
  "failed",
  "interrupted",
  "execution_unknown",
]);
export function nativeArgs(memories = false) {
  const settings: Record<string, unknown> = {
    model: MODEL,
    model_reasoning_effort: EFFORT,
    web_search: "live",
    "agents.enabled": true,
    "agents.default_subagent_model": MODEL,
    "agents.default_subagent_reasoning_effort": EFFORT,
  };
  if (memories)
    Object.assign(settings, {
      "features.memories": true,
      "memories.extract_model": MODEL,
      "memories.consolidation_model": MODEL,
    });
  return Object.entries(settings).flatMap(([key, value]) => [
    "-c",
    key + "=" + JSON.stringify(value),
  ]);
}
export class Runner {
  active = new Map<string, { rpc: RpcLike; finish: () => void }>();
  sessions = new Map<string, Promise<void>>();
  store: Store;
  factory: (payload: RunPayload, cwd: string) => RpcLike;
  constructor(
    store: Store,
    factory: (payload: RunPayload, cwd: string) => RpcLike = (payload, cwd) =>
      new AppServer(
        process.env.LEAD_CODEX_BINARY || "codex",
        nativeArgs(process.env.LEAD_CODEX_MEMORIES === "1"),
        cwd,
      ),
  ) {
    this.store = store;
    this.factory = factory;
  }
  async command(command: Command) {
    if (command.kind !== "run") return this.control(command);
    const payload = command.payload as RunPayload;
    if (
      typeof payload.session_key !== "string" ||
      typeof payload.input !== "string" ||
      typeof payload.instructions !== "string"
    )
      throw new Error("invalid_run_payload");
    if (
      (payload.model && payload.model !== MODEL) ||
      (payload.effort && payload.effort !== EFFORT)
    )
      throw new Error("unsupported_model_configuration");
    const record = this.store.begin(command.request_id, payload);
    if (!record.command_ids.includes(command.id)) {
      record.command_ids.push(command.id);
      this.store.save();
      this.store.event(record.id, "command/ack", { command_id: command.id });
    }
    if (
      (terminal.has(record.state) && record.state !== "execution_unknown") ||
      this.active.has(record.id)
    )
      return;
    const previous =
      this.sessions.get(payload.session_key) || Promise.resolve();
    const task = previous.catch(() => {}).then(() => this.run(record));
    this.sessions.set(payload.session_key, task);
    try {
      await task;
    } finally {
      if (this.sessions.get(payload.session_key) === task)
        this.sessions.delete(payload.session_key);
    }
  }
  async run(record: RequestRecord) {
    if (
      (terminal.has(record.state) && record.state !== "execution_unknown") ||
      this.active.has(record.id)
    )
      return;
    const reconcileOnly = record.state === "execution_unknown";
    if (reconcileOnly && !record.thread_id) return;
    const payload = record.payload,
      cwd = this.store.workspace(payload.session_key),
      rpc = this.factory(payload, cwd);
    let finish!: () => void;
    const done = new Promise<void>((resolve) => {
      finish = resolve;
    });
    this.active.set(record.id, { rpc, finish });
    rpc.onNotification((method, params) => {
      if (method === "connection/closed") {
        if (!terminal.has(record.state)) {
          this.store.update(record.id, { state: "execution_unknown" });
          this.store.event(record.id, "result", {
            state: "execution_unknown",
            thread_id: record.thread_id,
            turn_id: record.turn_id,
            error: "app_server_closed",
          });
        }
        finish();
        return;
      }
      // Authentication and account notifications remain local; never upload credentials.
      if (!/^(thread\/|turn\/|item\/|error$)/.test(method)) return;
      this.store.event(record.id, method, params);
      if (method === "turn/started" && params.threadId === record.thread_id) {
        this.store.update(record.id, {
          turn_id: params.turn?.id,
          state: "running",
        });
      }
      if (
        method === "item/completed" &&
        params.threadId === record.thread_id &&
        params.item?.type === "agentMessage" &&
        (!record.turn_id || params.turnId === record.turn_id)
      )
        this.store.update(record.id, { last_message: params.item.text });
      if (
        method === "turn/completed" &&
        params.threadId === record.thread_id &&
        (!record.turn_id || params.turn.id === record.turn_id)
      ) {
        const turn = params.turn;
        this.complete(record, turn.status, turn.id, turn.items);
        finish();
      }
    });
    rpc.onRequest((id, method, params) => {
      this.store.update(record.id, {
        pending: { id, method, params },
        state: "waiting_user",
      });
      this.store.event(record.id, "server/request", { id, method, params });
    });
    try {
      await rpc.open();
      const config: Dict = {
        model_reasoning_effort: EFFORT,
        web_search: payload.web_search || "live",
      };
      if (payload.tools?.length)
        config.mcp_servers = {
          lead: {
            command: process.execPath,
            args: [
              fileURLToPath(
                new URL(
                  import.meta.url.endsWith(".ts") ? "./mcp.ts" : "./mcp.js",
                  import.meta.url,
                ),
              ),
            ],
            env: {
              LEAD_EXECUTOR_HOME: this.store.folder,
              LEAD_EXECUTOR_REQUEST: record.id,
            },
            required: true,
          },
        };
      let thread = record.thread_id || this.store.thread(payload.session_key);
      if (thread) {
        await rpc.request("thread/resume", {
          threadId: thread,
          cwd,
          model: MODEL,
          config,
          developerInstructions: payload.instructions,
          excludeTurns: true,
        });
        this.store.update(record.id, { thread_id: thread });
        if (
          reconcileOnly ||
          ["starting", "running", "waiting_user", "reconciling"].includes(
            record.state,
          )
        ) {
          await this.reconcile(record, rpc, finish);
          await done;
          return;
        }
      } else {
        const started = await rpc.request("thread/start", {
          model: MODEL,
          cwd,
          config,
          developerInstructions: payload.instructions,
          ephemeral: false,
        });
        thread = started.thread.id;
        this.store.update(record.id, { thread_id: thread });
        this.store.setThread(payload.session_key, thread!);
        this.store.event(record.id, "thread/started", {
          thread: started.thread,
        });
      }
      // Persist BEFORE sending. A lost ACK may mean the turn already ran.
      this.store.update(record.id, { state: "starting" });
      const marker = "[lead_request:" + record.id + "]";
      const result = await rpc.request("turn/start", {
        threadId: thread,
        model: MODEL,
        effort: EFFORT,
        clientUserMessageId: record.id,
        input: [{ type: "text", text: marker + "\n" + payload.input }],
        ...(payload.output_schema
          ? { outputSchema: payload.output_schema }
          : {}),
      });
      if (!terminal.has(record.state))
        this.store.update(record.id, {
          turn_id: result.turn.id,
          state: "running",
        });
      await done;
    } catch (error) {
      if (!terminal.has(record.state)) {
        const crossed = record.state !== "prepared";
        const state = crossed ? "execution_unknown" : "failed";
        this.store.update(record.id, { state });
        this.store.event(record.id, "result", {
          state,
          thread_id: record.thread_id,
          turn_id: record.turn_id,
          error: String(error),
        });
      }
    } finally {
      this.active.delete(record.id);
      await rpc.close();
    }
  }
  complete(
    record: RequestRecord,
    status: string,
    turnId: string,
    items: Dict[] = [],
  ) {
    if (terminal.has(record.state)) return;
    let state =
      status === "completed"
        ? "completed"
        : status === "interrupted"
          ? "interrupted"
          : "failed";
    const text =
      items.filter((i) => i.type === "agentMessage").at(-1)?.text ??
      record.last_message ??
      "";
    let output: unknown = text;
    if (state === "completed" && record.payload.output_schema)
      try {
        output = JSON.parse(text);
      } catch {
        state = "failed";
      }
    this.store.update(record.id, {
      state,
      turn_id: turnId,
      output,
      pending: undefined,
    });
    this.store.event(record.id, "result", {
      state,
      output,
      thread_id: record.thread_id,
      turn_id: turnId,
      ...(status === "completed" && state === "failed"
        ? { error: "invalid_structured_output" }
        : {}),
    });
  }
  async reconcile(record: RequestRecord, rpc: RpcLike, finish: () => void) {
    this.store.update(record.id, { state: "reconciling" });
    let cursor: string | undefined;
    do {
      const page = await rpc.request("thread/turns/list", {
        threadId: record.thread_id,
        itemsView: "full",
        limit: 100,
        ...(cursor ? { cursor } : {}),
      });
      for (const turn of page.data || []) {
        const match =
          turn.id === record.turn_id ||
          JSON.stringify(turn.items || []).includes(
            "[lead_request:" + record.id + "]",
          );
        if (!match) continue;
        this.store.update(record.id, { turn_id: turn.id });
        if (turn.status === "inProgress") {
          this.store.update(record.id, { state: "running" });
          return;
        }
        this.complete(record, turn.status, turn.id, turn.items);
        finish();
        return;
      }
      cursor = page.nextCursor || undefined;
    } while (cursor);
    this.store.update(record.id, { state: "execution_unknown" });
    this.store.event(record.id, "result", {
      state: "execution_unknown",
      thread_id: record.thread_id,
      turn_id: record.turn_id,
      error: "original_turn_not_proven",
    });
    finish();
  }
  async control(command: Command) {
    if (this.store.data.controls.includes(command.id)) return;
    const record = this.store.request(command.request_id),
      active = this.active.get(command.request_id);
    if (!record || !active) throw new Error("request_not_active");
    // Control messages have no established RPC idempotency either: persist before send.
    this.store.data.controls.push(command.id);
    this.store.save();
    this.store.event(record.id, "command/ack", { command_id: command.id });
    try {
      if (command.kind === "steer")
        await active.rpc.request("turn/steer", {
          threadId: record.thread_id,
          expectedTurnId: record.turn_id,
          input: [{ type: "text", text: String(command.payload.input || "") }],
        });
      else if (command.kind === "interrupt")
        await active.rpc.request("turn/interrupt", {
          threadId: record.thread_id,
          turnId: record.turn_id,
        });
      else if (command.kind === "respond") {
        if (
          !record.pending ||
          String(record.pending.id) !== String(command.payload.id)
        )
          throw new Error("pending_request_changed");
        active.rpc.respond(record.pending.id, command.payload.result);
        this.store.update(record.id, { pending: undefined, state: "running" });
      }
    } catch (error) {
      this.store.event(record.id, "control/unknown", {
        command_id: command.id,
        error: String(error),
      });
    }
  }
  async close() {
    for (const { rpc, finish } of this.active.values()) {
      finish();
      await rpc.close();
    }
  }
}
