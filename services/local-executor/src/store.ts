import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import type {
  Binding,
  Dict,
  Event,
  RequestRecord,
  RunPayload,
  ServerBinding,
} from "./types.ts";
import { bindingIdentity, validateServerBinding } from "./server-binding.ts";
const digest = (value: string) =>
  createHash("sha256").update(value).digest("hex");
interface State {
  version: number;
  binding?: Binding;
  server_binding?: ServerBinding;
  instance_id?: string;
  requests: Record<string, RequestRecord>;
  sessions: Record<string, string>;
  events: Event[];
  controls: string[];
}
export class Store {
  folder: string;
  data: State;
  constructor(folder: string) {
    this.folder = resolve(folder);
    mkdirSync(this.folder, { recursive: true, mode: 0o700 });
    const file = join(this.folder, "state.json");
    this.data = existsSync(file)
      ? JSON.parse(readFileSync(file, "utf8"))
      : { version: 1, requests: {}, sessions: {}, events: [], controls: [] };
    if (![1, 2].includes(this.data.version)) throw new Error("unsupported_state_version");
    if (this.data.version === 2) {
      if (this.data.binding) throw new Error("invalid_server_binding");
      this.data.server_binding = validateServerBinding(this.data.server_binding);
      if (typeof this.data.instance_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(this.data.instance_id))
        throw new Error("invalid_server_instance");
    }
  }
  save() {
    const temp = join(this.folder, `.state-${process.pid}.tmp`);
    const fd = openSync(temp, "w", 0o600);
    try {
      writeFileSync(fd, JSON.stringify(this.data));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(temp, join(this.folder, "state.json"));
    const dir = openSync(this.folder, "r");
    try {
      fsyncSync(dir);
    } finally {
      closeSync(dir);
    }
  }
  bind(binding: Binding) {
    if (this.data.version !== 1) throw new Error("invalid_personal_binding");
    this.data.binding = binding;
    this.save();
  }
  bindServer(value: ServerBinding) {
    const binding = validateServerBinding(value);
    if (this.data.binding || this.data.version === 1 && (Object.keys(this.data.requests).length ||
        Object.keys(this.data.sessions).length || this.data.events.length || this.data.controls.length) ||
        this.data.server_binding && (bindingIdentity(this.data.server_binding) !== bindingIdentity(binding) ||
          this.data.server_binding.epoch !== binding.epoch))
      throw new Error("server_binding_conflict");
    this.data.version = 2;
    this.data.server_binding = binding;
    this.data.instance_id ||= randomUUID();
    this.save();
  }
  get shared() { return this.data.version === 2; }
  runtimeBinding() { return this.data.server_binding || this.data.binding; }
  request(id: string) {
    return this.data.requests[id];
  }
  begin(id: string, payload: RunPayload) {
    const hash = digest(JSON.stringify(payload)),
      old = this.request(id);
    if (old) {
      if (old.hash !== hash) throw new Error("request_identity_conflict");
      return old;
    }
    this.data.requests[id] = {
      id,
      hash,
      payload,
      state: "prepared",
      command_ids: [],
    };
    this.save();
    return this.request(id)!;
  }
  update(id: string, patch: Partial<RequestRecord>) {
    Object.assign(this.request(id)!, patch);
    this.save();
  }
  event(request_id: string, method: string, params: Dict) {
    const event = { id: randomUUID(), request_id, method, params };
    this.data.events.push(event);
    this.save();
    return event;
  }
  outbox() {
    return this.data.events;
  }
  ack(ids: string[]) {
    const accepted = new Set(ids);
    this.data.events = this.data.events.filter((e) => !accepted.has(e.id));
    this.save();
  }
  workspace(session: string) {
    const dir = join(this.folder, "workspaces", digest(session));
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    return dir;
  }
  thread(session: string) {
    return this.data.sessions[session];
  }
  setThread(session: string, id: string) {
    this.data.sessions[session] = id;
    this.save();
  }
  descriptor(id: string) {
    const path = join(this.folder, "requests", digest(id) + ".json");
    mkdirSync(join(this.folder, "requests"), { recursive: true, mode: 0o700 });
    writeFileSync(
      path,
      JSON.stringify({
        request_id: id,
        tools: this.request(id)!.payload.tools || [],
      }),
      { mode: 0o600 },
    );
    return path;
  }
}
