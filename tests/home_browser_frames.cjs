// Run inside the disposable CI network namespace with the release's locked web dependencies.
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const sharp = require('/run/lead-home/bin/web/node_modules/sharp');

const bundle = path.resolve(process.argv[2]);
const aid = '11111111-1111-4111-8111-111111111111';
const session = '22222222-2222-4222-8222-222222222222';
const base = 'http://127.0.0.1:18083';
const screenshot = base + '/api/agents/' + aid + '/browser/screenshot?format=jpeg&takeover=' + session;
const control = base + '/browser-control/' + aid + '?takeover=' + session;
const headers = {
  cookie: 'fixture=allowed', 'user-agent': 'Chrome/140', 'sec-fetch-site': 'same-origin',
  'sec-fetch-dest': 'empty', 'x-lead-workspace': 'fixture', origin: 'https://shui.click',
  'x-lead-request': '1', 'content-type': 'application/json',
};

async function waitFor(url) {
  for (let attempt = 0; attempt < 60; attempt++) {
    try { await (await fetch(url)).arrayBuffer(); return; } catch { await sleep(100); }
  }
  throw Error('Test listener did not start: ' + url);
}

async function main() {
  const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800">' +
    '<defs><pattern id="p" width="32" height="32" patternUnits="userSpaceOnUse">' +
    '<rect width="32" height="32" fill="#388"/><circle cx="10" cy="10" r="6" fill="#fff"/>' +
    '</pattern></defs><rect width="100%" height="100%" fill="url(#p)"/></svg>');
  const jpeg = await sharp(svg).jpeg().toBuffer();
  const inputs = [];
  const origin = http.createServer(async (req, res) => {
    if (req.headers.cookie !== headers.cookie) { res.writeHead(401).end(); return; }
    if (req.headers['x-lead-workspace'] !== headers['x-lead-workspace']) { res.writeHead(403).end(); return; }
    if (req.method === 'POST') {
      if (req.headers.origin !== headers.origin || req.headers['x-lead-request'] !== '1') {
        res.writeHead(403).end(); return;
      }
      const parts = [];
      for await (const part of req) parts.push(part);
      inputs.push(JSON.parse(Buffer.concat(parts)));
    }
    if (req.headers['if-none-match'] === '"frame"') { res.writeHead(304).end(); return; }
    res.writeHead(200, { 'content-type': 'image/jpeg', etag: '"frame"',
      'x-lead-frame-width': '1280', 'x-lead-frame-height': '800' }).end(jpeg);
  });
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'lead-frames-'));
  let encoder, nginx;
  try {
    await new Promise(resolve => origin.listen(3100, '127.0.0.1', resolve));
    encoder = spawn(process.execPath, [path.join(bundle, 'frame-live.cjs')], { stdio: 'inherit' });
    await waitFor('http://127.0.0.1:18736/');
    const conf = path.join(scratch, 'nginx.conf');
    fs.writeFileSync(conf, `daemon off; master_process off; pid ${scratch}/pid;
      error_log stderr crit; events {} http { access_log off;
      client_body_temp_path ${scratch}/body; proxy_temp_path ${scratch}/proxy;
      fastcgi_temp_path ${scratch}/fastcgi; uwsgi_temp_path ${scratch}/uwsgi; scgi_temp_path ${scratch}/scgi;
      include ${bundle}/upstream.conf;
      server { listen 127.0.0.1:18083; include ${bundle}/location.conf;
      location / { return 404; } } }`);
    execFileSync('nginx', ['-t', '-p', scratch, '-c', conf]);
    nginx = spawn('nginx', ['-p', scratch, '-c', conf], { stdio: 'inherit' });
    await waitFor(base);
    assert.equal((await fetch(screenshot)).status, 401);
    assert.equal((await fetch(screenshot, { headers: { ...headers, 'x-lead-workspace': 'denied' } })).status, 403);
    for (const [url, width] of [[screenshot, 960], [control, 704]]) {
      const request = { takeover: session, kind: 'click', x: 21, y: 34 };
      const res = await fetch(url, url === control ? { method: 'POST', headers, body: JSON.stringify(request) } : { headers });
      assert.equal(res.status, 200);
      assert.equal(res.headers.get('content-type'), 'image/avif');
      assert.equal(res.headers.get('x-lead-frame-width'), '1280');
      assert.equal(res.headers.get('x-lead-frame-height'), '800');
      assert.equal(res.headers.get('cache-control'), 'private, no-store');
      const data = Buffer.from(await res.arrayBuffer());
      assert.ok(data.length < jpeg.length * 0.9);
      assert.equal((await sharp(data).metadata()).width, width);
      if (url === control) assert.deepEqual(inputs, [request]);
    }
    const legacy = await fetch(screenshot, { headers: { ...headers, 'user-agent': 'Legacy' } });
    assert.deepEqual(Buffer.from(await legacy.arrayBuffer()), jpeg);
    assert.equal((await fetch(screenshot, { headers: { ...headers, 'if-none-match': '"frame"' } })).status, 304);
    const stopped = once(encoder, 'exit');
    encoder.kill(); await stopped;
    const fallback = await fetch(screenshot, { headers });
    assert.deepEqual(Buffer.from(await fallback.arrayBuffer()), jpeg);
    console.log('Real nginx routing, authenticated AVIF, original coordinates, single input, 304 and origin fallback passed.');
  } finally {
    if (encoder?.exitCode == null && !encoder?.signalCode) encoder?.kill();
    if (nginx && nginx.exitCode == null && !nginx.signalCode) { const stopped = once(nginx, 'exit'); nginx.kill(); await stopped; }
    origin.closeAllConnections(); origin.close();
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
