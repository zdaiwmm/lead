import { randomUUID } from "node:crypto";
import { Store } from "./store.ts";
import { LeadClient } from "./transport.ts";
import { Runner } from "./runner.ts";
import { capabilities as readCapabilities } from "./capabilities.ts";
import type { Command, Dict } from "./types.ts";
export class Daemon {
  store: Store;
  runner: Runner;
  client: LeadClient;
  instanceId = randomUUID();
  tasks = new Map<string, Promise<void>>();
  stopped = false;
  capabilities: Dict;
  constructor(
    store: Store,
    runner = new Runner(store),
    client?: LeadClient,
    capabilities: Dict = {},
  ) {
    if (!store.data.binding) throw new Error("device_not_paired");
    this.store = store;
    this.runner = runner;
    this.client = client || new LeadClient(store.data.binding);
    this.capabilities = capabilities;
  }
  async tick() {
    // Establish this process's epoch before uploading events or running any tools.
    const response = await this.client.call("poll", {
      instance_id: this.instanceId,
      capabilities: this.capabilities,
    });
    if (typeof response.epoch !== "number")
      throw new Error("invalid_device_epoch");
    if (this.store.data.binding!.epoch !== response.epoch) {
      this.store.data.binding!.epoch = response.epoch;
      this.store.save();
    }
    this.client.binding = this.store.data.binding!;
    await this.flush();
    for (const command of response.commands || []) this.dispatch(command);
  }
  dispatch(command: Command) {
    if (this.tasks.has(command.id)) return;
    const task = this.runner
      .command(command)
      .catch((error) => {
        this.store.event(command.request_id, "command/error", {
          command_id: command.id,
          error: String(error),
        });
      })
      .finally(() => this.tasks.delete(command.id));
    this.tasks.set(command.id, task);
  }
  recover() {
    for (const record of Object.values(this.store.data.requests))
      if (!["completed", "failed", "interrupted"].includes(record.state))
        this.dispatch({
          id: record.command_ids[0] || "recover:" + record.id,
          request_id: record.id,
          kind: "run",
          payload: record.payload,
        });
  }
  async flush() {
    // Preserve event ordering and only remove IDs the server explicitly accepted.
    const events = this.store.outbox().slice(0, 100);
    if (!events.length) return;
    const result = await this.client.call("events", { events });
    if (Array.isArray(result.accepted)) this.store.ack(result.accepted);
  }
  async run() {
    let recovered = false,
      lastCapabilityCheck = Date.now(),
      checking = false;
    while (!this.stopped) {
      if (!checking && Date.now() - lastCapabilityCheck > 60_000) {
        checking = true;
        lastCapabilityCheck = Date.now();
        void readCapabilities()
          .then((value) => {
            this.capabilities = value;
          })
          .catch(() => {
            this.capabilities = {
              ready: false,
              auth_type: "unavailable",
              models: [],
              reasoning_efforts: [],
            };
          })
          .finally(() => {
            checking = false;
          });
      }
      try {
        await this.tick();
        if (!recovered) {
          this.recover();
          recovered = true;
        }
      } catch (error) {
        // A stale process is fenced by the server. It must not re-acquire ownership.
        if (
          String(error).includes("lead_http_409") ||
          String(error).includes("lead_http_401")
        ) {
          await this.close();
          throw error;
        }
      }
      if (!this.stopped)
        await new Promise<void>((r) => {
          setTimeout(r, 2000);
        });
    }
  }
  async close() {
    this.stopped = true;
    await this.runner.close();
  }
}
