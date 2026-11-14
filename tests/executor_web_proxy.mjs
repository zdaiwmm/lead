// Real Next HTTP proxy against a synthetic API; no account, model or DB calls.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import http from "node:http";
import { readFile, writeFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

const web = fileURLToPath(new URL("../apps/web/", import.meta.url));
const listen = async (server) => {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
};

test("Next forwards both executor versions with original POST, bearer, body and query", { timeout: 120000 }, async () => {
  const seen = [];
  const api = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    const value = { path: req.url, method: req.method, bearer: req.headers.authorization, body };
    seen.push(value);
    res.writeHead(req.url.includes("/poll") ? 409 : 200, { "content-type": "application/json" });
    res.end(JSON.stringify(value));
  });
  const apiPort = await listen(api);
  const reservation = http.createServer();
  const webPort = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  const generated = ["tsconfig.json", "next-env.d.ts"];
  const originals = await Promise.all(generated.map((name) => readFile(new URL(name, `file://${web}`))));
  const production = process.env.LEAD_PROXY_PRODUCTION === "1";
  const dist = `.next-proxy-test-${process.pid}`;
  let child;
  let log = "";
  const launch = (mode) => {
    const process = spawn(globalThis.process.execPath, ["node_modules/next/dist/bin/next", mode, ...(mode === "build" ? [] : ["--hostname", "127.0.0.1", "--port", String(webPort)])], {
      cwd: web,
      env: { ...globalThis.process.env, NEXT_TELEMETRY_DISABLED: "1", LEAD_WEB_DIST_DIR: dist, LEAD_API_ORIGIN: `http://127.0.0.1:${apiPort}` },
      stdio: ["ignore", "pipe", "pipe"],
    });
    process.stdout.on("data", (part) => { log += part; });
    process.stderr.on("data", (part) => { log += part; });
    return process;
  };
  try {
    if (production) {
      // Rewrites are frozen at build time. Build only our private fixture dist
      // with its synthetic origin; leave CI's distributable .next untouched.
      child = launch("build");
      const [code] = await once(child, "exit");
      assert.equal(code, 0, log);
    }
    child = launch(production ? "start" : "dev");
    const base = `http://127.0.0.1:${webPort}`;
    const deadline = Date.now() + 60000;
    let ready = false;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error(`Next exited: ${log}`);
      try {
        const response = await fetch(base + "/executor/v1/pair", { signal: AbortSignal.timeout(3000) });
        if (response.status === 200) { ready = true; break; }
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    assert.ok(ready, `Next did not become ready: ${log}`);
    const scope = "11111111-1111-4111-8111-111111111111";
    for (const path of ["/executor/v1/pair", "/executor/v2/nodes/register", "/executor/v2/nodes/poll", ...["poll", "events", "tools"].map((name) => `/executor/v2/scopes/${scope}/${name}`)]) {
      const before = seen.length;
      const body = JSON.stringify({ synthetic: path });
      const response = await fetch(base + path + "?probe=preserved", {
        method: "POST", headers: { "content-type": "application/json", authorization: "Bearer synthetic-proxy-test" }, body,
      });
      assert.equal(seen.length, before + 1, "each POST forwards once");
      assert.equal(response.status, path.endsWith("/poll") ? 409 : 200, path);
      assert.deepEqual(await response.json(), { path: path + "?probe=preserved", method: "POST", bearer: "Bearer synthetic-proxy-test", body });
    }
    const before = seen.length;
    const unknown = await fetch(base + "/executor/v3/nodes/poll", { method: "POST" });
    assert.equal(unknown.status, 404);
    assert.equal(seen.length, before, "unknown protocol must not reach the API");
  } finally {
    if (child && child.exitCode === null) { child.kill("SIGTERM"); await once(child, "exit"); }
    await new Promise((resolve) => api.close(resolve));
    await rm(new URL(dist, `file://${web}`), { recursive: true, force: true });
    await Promise.all(generated.map((name, i) => writeFile(new URL(name, `file://${web}`), originals[i])));
  }
});
