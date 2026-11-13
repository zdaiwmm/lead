import test from "node:test";
import assert from "node:assert/strict";
import { launchAgentPlist } from "../src/launchd.ts";
import { capabilities } from "../src/capabilities.ts";
test("launchd arguments are XML escaped, include a fixed executable, and do not contain credentials", () => {
  const plist = launchAgentPlist(
    "/private/Lead & Co/state",
    "/opt/Node & Co/node",
    "/opt/Lead/cli.js",
    "/usr/bin:/opt/bin",
  );
  assert.ok(plist.includes("/private/Lead &amp; Co/state"));
  assert.ok(plist.includes("/opt/Node &amp; Co/node"));
  assert.ok(!plist.includes("Bearer"));
});
test("capability detection verifies actual signed in model and medium without starting a turn", async () => {
  const methods: string[] = [];
  const rpc = {
    open: async () => {},
    close: async () => {},
    request: async (method: string) => {
      methods.push(method);
      if (method === "account/read")
        return { account: { type: "chatgpt", email: "private@example.test" } };
      return {
        data: [
          {
            id: "gpt-6.1-sol",
            model: "gpt-6.1-sol",
            hidden: false,
            supportedReasoningEfforts: [{ reasoningEffort: "medium" }],
            inputModalities: ["text", "image"],
          },
        ],
      };
    },
  };
  const result = await capabilities(rpc as any, "codex-cli test");
  assert.equal(result.ready, true);
  assert.equal(result.auth_type, "chatgpt");
  assert.deepEqual(methods, ["account/read", "model/list"]);
  assert.ok(!JSON.stringify(result).includes("private@example.test"));
});
