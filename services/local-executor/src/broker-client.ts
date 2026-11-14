import { randomUUID } from "node:crypto";
import { createConnection } from "node:net";
import type { BrokerRpc, Dict, ExecutorClient, ServerBinding } from "./types.ts";
export const BROKER_SOCKET = "/run/lead-codex-node/broker.sock";
export const FRAME_LIMIT = 16 * 1024 * 1024;
export class BrokerError extends Error {
  code: string; status: number; retryable: boolean;
  constructor(code: string, status = 0, retryable = false) {
    const safe = /^[a-z][a-z0-9_]{0,79}$/.test(code) ? code : "broker_request_rejected";
    super(safe); this.code = safe; this.status = status; this.retryable = retryable;
  }
}
export function nodeFailure(error: unknown) {
  return error instanceof BrokerError && ["executor_node_unavailable", "executor_node_epoch_changed", "broker_worker_changed"].includes(error.code);
}
export class UnixRpc implements BrokerRpc {
  path: string;
  constructor(path = BROKER_SOCKET) {
    if (!path.startsWith("/") || path.includes("\0")) throw new BrokerError("invalid_unix_socket");
    this.path = path;
  }
  call(method: string, params: Dict): Promise<Dict> {
    const id = randomUUID(), frame = JSON.stringify({ v: 1, id, method, params }) + "\n";
    if (Buffer.byteLength(frame) > FRAME_LIMIT) return Promise.reject(new BrokerError("broker_frame_too_large"));
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.path); let buffer = "", done = false;
      const finish = (error?: Error, value?: Dict) => { if (done) return; done = true; socket.destroy(); error ? reject(error) : resolve(value!); };
      socket.setEncoding("utf8");
      socket.setTimeout(45_000, () => finish(new BrokerError("broker_timeout", 0, true)));
      socket.on("connect", () => socket.write(frame));
      socket.on("error", () => finish(new BrokerError("broker_disconnected", 0, true)));
      socket.on("close", () => finish(new BrokerError("broker_disconnected", 0, true)));
      socket.on("data", chunk => {
        buffer += chunk;
        if (Buffer.byteLength(buffer) > FRAME_LIMIT) return finish(new BrokerError("broker_frame_too_large"));
        const newline = buffer.indexOf("\n"); if (newline < 0) return;
        try {
          const reply = JSON.parse(buffer.slice(0, newline));
          if (reply.v !== 1 || reply.id !== id || typeof reply !== "object") throw new Error();
          if (reply.error) return finish(new BrokerError(reply.error.code, Number(reply.error.status) || 0, reply.error.retryable === true));
          if (!reply.result || typeof reply.result !== "object" || Array.isArray(reply.result)) throw new Error();
          finish(undefined, reply.result);
        } catch { finish(new BrokerError("broker_invalid_response")); }
      });
    });
  }
}
export class ScopeClient implements ExecutorClient {
  binding: ServerBinding; rpc: BrokerRpc; generation: string;
  constructor(binding: ServerBinding, rpc: BrokerRpc, generation: string) { this.binding = binding; this.rpc = rpc; this.generation = generation; }
  call(path: string, payload: Dict) {
    const method = { poll: "scope.poll", events: "scope.events", tools: "request.tools" }[path];
    if (!method) return Promise.reject(new BrokerError("broker_method_forbidden"));
    const body = path === "poll" ? { instance_id: payload.instance_id } : path === "events" ? { events: payload.events } :
      { request_id: payload.request_id, name: payload.name, arguments: payload.arguments };
    return this.rpc.call(method, { ...body, worker_generation: this.generation, scope_ref: this.binding.scope_ref,
      grant_generation: this.binding.grant_generation, epoch: this.binding.epoch });
  }
}
