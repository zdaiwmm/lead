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
} from "./types.ts";
const digest = (value: string) =>
  createHash("sha256").update(value).digest("hex");
interface State {
  version: number;
  binding?: Binding;
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
    if (this.data.version !== 1) throw new Error("unsupported_state_version");
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
    this.data.binding = binding;
    this.save();
  }
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
