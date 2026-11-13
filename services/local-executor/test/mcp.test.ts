import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { Store } from "../src/store.ts";
test("official MCP stdio preserves screenshots and binds every call to its original request", async (t) => {
  const home = mkdtempSync(join(tmpdir(), "lead-mcp-test-")),
    calls: any[] = [];
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const server = createServer(async (req, res) => {
    let data = "";
    for await (const chunk of req) data += chunk;
    calls.push({
      path: req.url,
      auth: req.headers.authorization,
      body: JSON.parse(data),
    });
    res.setHeader("content-type", "application/json");
    res.end(
      JSON.stringify({
        content: [
          { type: "image", mimeType: "image/png", data: "aW1hZ2U=" },
          { type: "text", text: "observed" },
        ],
        structuredContent: { document: "current" },
      }),
    );
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const store = new Store(home),
    address = server.address() as any;
  store.bind({
    url: "http://127.0.0.1:" + address.port,
    device_id: "d",
    epoch: 7,
    token: "private-test-token",
    name: "test",
  });
  store.begin("task-1", {
    session_key: "scope",
    input: "read",
    instructions: "read",
    tools: [
      {
        name: "browser_observe",
        description: "read screenshot",
        inputSchema: {
          type: "object",
          properties: {},
          additionalProperties: false,
        },
      },
    ],
  });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("../src/mcp.ts", import.meta.url))],
    env: { LEAD_EXECUTOR_HOME: home, LEAD_EXECUTOR_REQUEST: "task-1" },
    stderr: "pipe",
  });
  const client = new Client({ name: "test", version: "1" });
  await client.connect(transport);
  t.after(() => client.close());
  assert.equal((await client.listTools()).tools.length, 1);
  const result: any = await client.callTool({
    name: "browser_observe",
    arguments: {},
  });
  assert.equal(result.content[0].type, "image");
  assert.equal(result.content[0].data, "aW1hZ2U=");
  assert.equal(calls[0].body.request_id, "task-1");
  assert.equal(calls[0].body.epoch, 7);
  assert.equal(calls[0].auth, "Bearer private-test-token");
  await assert.rejects(
    client.callTool({ name: "not_declared", arguments: {} }),
  );
  assert.equal(calls.length, 1);
});
