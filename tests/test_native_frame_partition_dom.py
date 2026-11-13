"""The pinned Chromium partition, not profile emptiness, isolates public frames."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',reason='Explicit real Chromium DOM verification required')


@pytest.mark.asyncio
async def test_public_opaque_frame_cannot_read_or_change_default_profile_sentinels(tmp_path):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass

        def do_GET(self):
            requests.append({'path':self.path,'host':self.headers['Host'],'cookie':bool(self.headers.get('Cookie'))})
            if self.path=='/parent':
                body='<iframe credentialless sandbox src="http://127.0.0.1:'+str(self.server.server_port)+'/public"></iframe>'
            elif self.path=='/public':
                body='<p id=public>Public static frame</p><script>window.executed=true;document.body.append(localStorage.getItem("old-local")||"")</script>'
            else:
                body='<p>Ordinary profile storage</p>'
            raw=('<!doctype html>'+body).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    asset=Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR','/missing'))/'chromium/chrome-linux64/chrome'
    options={'headless':True,'args':['--no-sandbox']}
    if asset.is_file():
        options['executable_path']=str(asset)
    try:
        async with async_playwright() as playwright:
            context=await playwright.chromium.launch_persistent_context(tmp_path/'profile',**options)
            await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith((
                'http://127.0.0.1:'+str(server.server_port),'http://localhost:'+str(server.server_port))) else route.abort())
            try:
                first=await context.new_page()
                await first.goto('http://127.0.0.1:'+str(server.server_port)+'/seed')
                await first.evaluate('''async()=>{localStorage.setItem('old-local','synthetic-private-local-sentinel');
                    await new Promise((resolve,reject)=>{const request=indexedDB.open('old-profile-db',1);
                        request.onupgradeneeded=()=>request.result.createObjectStore('data');
                        request.onerror=()=>reject(Error('seed_failed'));request.onsuccess=()=>{
                            const db=request.result,tx=db.transaction('data','readwrite');
                            tx.objectStore('data').put('synthetic-private-idb-sentinel','key');
                            tx.oncomplete=()=>{db.close();resolve()};};});}''')
                await first.close()
                parent=await context.new_page()
                await parent.goto('http://localhost:'+str(server.server_port)+'/parent')
                child=next(frame for frame in parent.frames if frame.parent_frame)
                await child.locator('#public').wait_for()
                proof=await child.evaluate('''async()=>({credentialless:window.credentialless,origin:origin,
                    executed:window.executed===true,text:document.body.innerText,
                    local:(()=>{try{localStorage.length;return false}catch(e){return e.name==='SecurityError'}})(),
                    idb:await (async()=>{try{await indexedDB.databases();return false}catch(e){return e.name==='SecurityError'}})()})''')
                assert proof=={'credentialless':True,'origin':'null','executed':False,'text':'Public static frame','local':True,'idb':True}
                session=await context.new_cdp_session(child)
                try:
                    frame=(await session.send('Page.getFrameTree'))['frameTree']['frame']
                    with pytest.raises(Exception,match='opaque origin and its storage key cannot be serialized'):
                        await session.send('Storage.getStorageKeyForFrame',{'frameId':frame['id']})
                finally:
                    await session.detach()
                original=await context.new_page()
                await original.goto('http://127.0.0.1:'+str(server.server_port)+'/seed')
                kept=await original.evaluate('''async()=>({local:localStorage.getItem('old-local'),idb:await new Promise((resolve,reject)=>{
                    const request=indexedDB.open('old-profile-db',1);request.onerror=()=>reject(Error('read_failed'));
                    request.onsuccess=()=>{const db=request.result,read=db.transaction('data').objectStore('data').get('key');
                        read.onsuccess=()=>{db.close();resolve(read.result)};};})})''')
                assert kept=={'local':'synthetic-private-local-sentinel','idb':'synthetic-private-idb-sentinel'}
                assert [row for row in requests if row['path']=='/public']==[
                    {'path':'/public','host':'127.0.0.1:'+str(server.server_port),'cookie':False}]
            finally:
                await context.close()
    finally:
        server.shutdown()
        server.server_close()
