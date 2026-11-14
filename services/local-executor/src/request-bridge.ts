import { randomBytes, randomUUID } from "node:crypto";
import { chmodSync, unlinkSync } from "node:fs";
import { createConnection, createServer, type Socket } from "node:net";
import { BrokerError, FRAME_LIMIT } from "./broker-client.ts";
import type { Store } from "./store.ts";
import type { Dict, ExecutorClient, ToolSpec } from "./types.ts";
interface Ticket {
  store: Store; request: string; client: ExecutorClient; tools: ToolSpec[];
  generation: string; expires: number; active: boolean; sockets: Set<Socket>;
}
export interface RequestLease { env: Record<string, string>; close(): void; }
const terminal = new Set(["completed", "failed", "interrupted", "execution_unknown", "canceled"]);
export class RequestBridge {
  path: string; generation: string; now: () => number;
  tickets = new Map<string, Ticket>();
  sockets = new Set<Socket>();
  server = createServer(socket => this.accept(socket));
  listening = false;
  constructor(path: string, generation: string, now = () => performance.now()) { this.path = path; this.generation = generation; this.now = now; }
  async start() {
    await new Promise<void>((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(this.path, () => { this.server.off("error", reject); resolve(); });
    });
    this.listening = true; chmodSync(this.path, 0o600);
  }
  issue(store: Store, request: string, client: ExecutorClient): RequestLease {
    if (!this.listening || !store.shared || !store.request(request)) throw new BrokerError("request_bridge_scope_invalid");
    const token = randomBytes(32).toString("base64url");
    const ticket: Ticket = { store, request, client, tools: structuredClone(store.request(request).payload.tools || []),
      generation: this.generation, expires: this.now() + 90_000, active: true, sockets: new Set() };
    this.tickets.set(token, ticket);
    return { env: { LEAD_REQUEST_BRIDGE: this.path, LEAD_REQUEST_TICKET: token, LEAD_EXECUTOR_REQUEST: request },
      close: () => { ticket.active = false; this.tickets.delete(token); for (const socket of ticket.sockets) socket.destroy(); } };
  }
  private accept(socket: Socket) {
    this.sockets.add(socket); let buffer = "", ticket: Ticket | undefined;
    socket.setEncoding("utf8");
    // Only the initial handshake expires. Long model turns retain an authenticated
    // channel; every tool call still checks its original live request and generation.
    const timer = setTimeout(() => socket.destroy(), 90_000); timer.unref();
    socket.on("error", () => {});
    socket.on("close", () => { clearTimeout(timer); this.sockets.delete(socket); ticket?.sockets.delete(socket); });
    socket.on("data", chunk => {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > FRAME_LIMIT) { socket.destroy(); return; }
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n"), line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        let frame: Dict;
        try { frame = JSON.parse(line); if (frame.v !== 1 || typeof frame.id !== "string" || !frame.params || typeof frame.params !== "object") throw new Error(); }
        catch { socket.destroy(); return; }
        const reply = (result?: Dict, code?: string) => {
          if (!socket.destroyed) socket.write(JSON.stringify({ v: 1, id: frame.id,
            ...(code ? { error: { code } } : { result }) }) + "\n");
        };
        if (!ticket) {
          const p = frame.params;
          const candidate = typeof p.ticket === "string" ? this.tickets.get(p.ticket) : undefined;
          if (frame.method !== "request.hello" || Object.keys(p).sort().join() !== "request_id,ticket" ||
              !candidate || candidate.expires <= this.now() || !candidate.active || candidate.request !== p.request_id ||
              candidate.generation !== this.generation) { reply(undefined, "request_bridge_unauthorized"); socket.end(); continue; }
          ticket = candidate; ticket.sockets.add(socket); clearTimeout(timer);
          reply({ tools: ticket.tools }); continue;
        }
        const current = ticket, record = current.store.request(current.request), p = frame.params;
        if (frame.method !== "request.tools" || Object.keys(p).sort().join() !== "arguments,name" ||
            !current.active || current.generation !== this.generation || !record || terminal.has(record.state) || record.cancel_requested ||
            typeof p.name !== "string" || !current.tools.some(tool => tool.name === p.name) ||
            !p.arguments || typeof p.arguments !== "object" || Array.isArray(p.arguments)) {
          reply(undefined, "request_bridge_forbidden"); continue;
        }
        void current.client.call("tools", { request_id: current.request, name: p.name, arguments: p.arguments })
          .then(result => reply(result))
          .catch(error => reply(undefined, error instanceof BrokerError ? error.code : "request_bridge_tool_failed"));
      }
    });
  }
  async close() {
    for (const ticket of this.tickets.values()) ticket.active = false;
    this.tickets.clear(); for (const socket of this.sockets) socket.destroy();
    if (this.listening) {
      await new Promise<void>(resolve => this.server.close(() => resolve()));
      this.listening = false;
      try { unlinkSync(this.path); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    }
  }
}

export class RequestBridgeClient {
  socket?: Socket; path: string; ticket: string; request: string;
  pending = new Map<string, { resolve(value: Dict): void; reject(error: Error): void; timer: NodeJS.Timeout }>();
  buffer = ""; tools: ToolSpec[] = [];
  constructor(path: string, ticket: string, request: string) { this.path = path; this.ticket = ticket; this.request = request; }
  async open() {
    if (!this.path.startsWith("/") || !this.ticket || !this.request) throw new BrokerError("request_bridge_scope_invalid");
    const socket = this.socket = createConnection(this.path); socket.setEncoding("utf8");
    socket.on("error", () => this.fail()); socket.on("close", () => this.fail());
    socket.on("data", chunk => {
      this.buffer += chunk;
      if (Buffer.byteLength(this.buffer) > FRAME_LIMIT) { this.close(); return; }
      while (this.buffer.includes("\n")) {
        const end = this.buffer.indexOf("\n"), line = this.buffer.slice(0, end); this.buffer = this.buffer.slice(end + 1);
        try {
          const message = JSON.parse(line), pending = this.pending.get(message.id);
          if (message.v !== 1 || !pending) throw new Error();
          this.pending.delete(message.id); clearTimeout(pending.timer);
          if (message.error) pending.reject(new BrokerError(message.error.code));
          else if (!message.result || typeof message.result !== "object") pending.reject(new BrokerError("request_bridge_invalid_response"));
          else pending.resolve(message.result);
        } catch { this.close(); return; }
      }
    });
    const result = await this.call("request.hello", { ticket: this.ticket, request_id: this.request });
    if (!Array.isArray(result.tools)) { this.close(); throw new BrokerError("request_bridge_invalid_response"); }
    this.tools = result.tools;
  }
  call(method: string, params: Dict): Promise<Dict> {
    if (!this.socket || this.socket.destroyed) return Promise.reject(new BrokerError("request_bridge_disconnected"));
    const id = randomUUID(), frame = JSON.stringify({ v: 1, id, method, params }) + "\n";
    if (Buffer.byteLength(frame) > FRAME_LIMIT) return Promise.reject(new BrokerError("request_bridge_frame_too_large"));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new BrokerError("request_bridge_timeout")); }, 45_000);
      this.pending.set(id, { resolve, reject, timer }); this.socket!.write(frame);
    });
  }
  private fail() { for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(new BrokerError("request_bridge_disconnected")); } this.pending.clear(); }
  close() { this.socket?.destroy(); this.fail(); }
}
