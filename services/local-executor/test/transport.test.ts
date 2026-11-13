import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { LeadClient, validateUrl } from "../src/transport.ts";
import { Daemon } from "../src/daemon.ts";
import { Store } from "../src/store.ts";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
test("connector rejects nonlocal HTTP, credential URLs and redirects", async () => {
  assert.throws(() => validateUrl("http://example.com"), /https/);
  assert.throws(() => validateUrl("https://user:secret@example.com"), /https/);
  const server = createServer((req, res) => {
    res.writeHead(302, { location: "http://localhost:1/stolen" });
    res.end();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const address = server.address() as any;
  try {
    await assert.rejects(
      new LeadClient({
        url: "http://127.0.0.1:" + address.port,
        device_id: "d",
        token: "private",
        epoch: 1,
        name: "test",
      }).call("poll", {}),
    );
  } finally {
    server.close();
  }
});
test("poll fences daemon instance and sends heartbeats while a request runs", async (t) => {
  const home = mkdtempSync(join(tmpdir(), "lead-daemon-test-"));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const store = new Store(home);
  store.bind({
    url: "http://localhost",
    device_id: "d",
    token: "private",
    epoch: 1,
    name: "test",
  });
  const polls: any[] = [],
    events: any[] = [];
  let finish!: () => void;
  const runner = {
    command: () =>
      new Promise<void>((r) => {
        finish = r;
      }),
    close: async () => {
      finish?.();
    },
  };
  const client = {
    call: async (path: string, payload: any) => {
      if (path === "poll") {
        polls.push(payload);
        return {
          epoch: 2,
          commands:
            polls.length === 1
              ? [{ id: "c", request_id: "r", kind: "run", payload: {} }]
              : [],
        };
      }
      events.push(payload);
      return { accepted: payload.events?.map((e: any) => e.id) || [] };
    },
  };
  const daemon = new Daemon(store, runner as any, client as any);
  await daemon.tick();
  await daemon.tick();
  assert.equal(polls.length, 2);
  assert.equal(polls[0].instance_id, polls[1].instance_id);
  assert.equal(store.data.binding?.epoch, 2);
  finish();
  await daemon.close();
});
