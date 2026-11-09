// Re-encode only an authorized takeover response. No frames or credentials are retained.
const http = require('node:http');
const { performance } = require('node:perf_hooks');
const sharp = require('/run/lead-home/bin/web/node_modules/sharp');
sharp.cache(false);
sharp.concurrency(2);
const uuid = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
const validId = new RegExp('^'+uuid+'$');
const compactPath = new RegExp('^/browser-control/' + uuid + '$');
const screenshotPath = new RegExp('^/api/agents/' + uuid + '/browser/screenshot$');
function supportsAvif(ua) {
  const v = /(?:Chrome|Chromium)\/(\d+)/.exec(ua);
  if (v) return Number(v[1]) >= 85;
  const f = /Firefox\/(\d+)/.exec(ua);
  if (f) return Number(f[1]) >= 93;
  const s = /Version\/(\d+)/.exec(ua);
  return !!s && Number(s[1]) >= 17 && ua.includes('Safari/');
}
let encoding = 0;
const stats = {authorizedImages:0, encodedImages:0, sourceBytes:0, outputBytes:0};
process.on('SIGUSR2',()=>console.log('FRAME_TOTALS',JSON.stringify(stats)));
const server = http.createServer(async (req, res) => {
  const start = performance.now();
  try {
    const u = new URL(req.url, 'http://127.0.0.1');
    const compact = compactPath.test(u.pathname);
    if (u.origin !== 'http://127.0.0.1' || !(compact || screenshotPath.test(u.pathname)) ||
        !['POST','GET'].includes(req.method) || (req.method === 'POST' && !compact)) {
      res.writeHead(404);res.end();return;
    }
    const headers = {};
    for (const k of ['cookie','origin','content-type','if-none-match','x-lead-request','x-lead-workspace','x-lead-frame-tag','user-agent'])
      if (req.headers[k]) headers[k]=req.headers[k];
    let input;
    if (req.method==='POST') {
      const parts=[];let size=0;
      for await (const part of req) {
        size+=part.length;
        if (size>65536) {res.writeHead(413);res.end();return;}
        parts.push(part);
      }
      input=Buffer.concat(parts);
    }
    const upstream = await fetch('http://127.0.0.1:3100'+u.pathname+u.search,
      {method:req.method,headers,body:input,redirect:'error',signal:AbortSignal.timeout(120000)});
    const out={'cache-control':'private, no-store','vary':'User-Agent'};
    for (const k of ['content-type','etag','x-lead-frame-tag','x-lead-frame-width','x-lead-frame-height'])
      if(upstream.headers.has(k)) out[k]=upstream.headers.get(k);
    let body=Buffer.from(await upstream.arrayBuffer());
    const upstreamMs=performance.now()-start;
    const mime=out['content-type']||'';
    const sourceBytes=body.length;
    if (!res.destroyed && upstream.status===200 && (compact || u.searchParams.get('format')==='jpeg') &&
        /image\/(jpeg|webp)/.test(mime) && body.length>1024 && body.length<4*1024*1024 &&
        validId.test(u.searchParams.get('takeover')||'') && req.headers['sec-fetch-dest']==='empty' &&
        req.headers['sec-fetch-site']==='same-origin' && encoding<4 && supportsAvif(req.headers['user-agent']||'')) {
      encoding++;
      try {
        const image=sharp(body,{limitInputPixels:4000000});
        const meta=await image.metadata();
        const reduced=await image.resize({width:compact?704:960,withoutEnlargement:true}).avif({quality:compact?34:39,effort:1,chromaSubsampling:'4:2:0'}).toBuffer();
        if(reduced.length<body.length*0.9) {
          out['x-lead-frame-width'] ||= String(meta.width);
          out['x-lead-frame-height'] ||= String(meta.height);
          out['content-type']='image/avif';body=reduced;
        }
      } catch { /* Send the authorized original when a codec cannot handle it. */ }
      finally { encoding--; }
    }
    if(!res.destroyed) {
      if (upstream.status===200 && mime.startsWith('image/')) {
        stats.authorizedImages++;stats.sourceBytes+=sourceBytes;stats.outputBytes+=body.length;
        if(out['content-type']==='image/avif') stats.encodedImages++;
      }
      out['server-timing']='origin;dur='+upstreamMs.toFixed(1)+', encode;dur='+(performance.now()-start-upstreamMs).toFixed(1);
      if (![204,304].includes(upstream.status)) out['content-length']=body.length;
      res.writeHead(upstream.status,out);res.end(body);
    }
  } catch {
    if(!res.headersSent && !res.destroyed) res.writeHead(502,{'cache-control':'no-store'});
    res.end();
  }
});
server.listen(18736,'127.0.0.1');
