"""Only an actual navigation-destroyed renderer permits bounded fresh scope reads."""
import asyncio
import copy
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from lead_app.browser_runtime import PersistentRuntime
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

NAVIGATION_ERROR = 'Execution context was destroyed, most likely because of a navigation'


def scope_runtime():
    scope = {'target_id':'original-root','task_id':'original-task','generation':1,
        'account_id':'original-account','origins':['https://board.fixture']}
    page = SimpleNamespace(frames=[object(),object()])
    runtime = PersistentRuntime.__new__(PersistentRuntime)
    runtime.pages = {scope['target_id']:{'scope':scope,'page':page}}
    runtime.gate = SimpleNamespace(targets={'session':{'targetId':scope['target_id'],'type':'page'}},
        owners={'session':scope['target_id']},documents={scope['target_id']:['root','loader','https://board.fixture/home']})
    runtime.forward_token = lambda: 'live-original-token'
    return runtime, scope


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['transient_navigation','permission_changed','source_changed','protocol_error','never_stable'])
async def test_scope_retries_only_confirmed_navigation_and_rechecks_authority(monkeypatch, condition):
    from lead_app import native_frame_roles

    runtime, scope = scope_runtime()
    calls, authority = [], []
    def current(value):
        assert value is scope
        authority.append(True)
        if condition == 'permission_changed' and calls:
            raise ValueError('frame_source_expired')
    runtime.current_scope = current
    async def observation(current_runtime, current_scope, page):
        assert current_runtime is runtime and current_scope is scope
        calls.append(True)
        if condition == 'source_changed':
            raise ValueError('frame_source_changed')
        if condition == 'protocol_error':
            raise PlaywrightError('Unrelated protocol failure')
        if len(calls) == 1 or condition == 'never_stable':
            raise PlaywrightError(NAVIGATION_ERROR)
        return list(scope['origins'])
    monkeypatch.setattr(native_frame_roles, 'observation', observation)
    if condition == 'transient_navigation':
        result = await runtime.scope_view(scope['target_id'])
        assert result['origins'] == scope['origins'] and len(calls) == 2 and len(authority) >= 2
    elif condition == 'permission_changed':
        with pytest.raises(ValueError, match='frame_source_expired'):
            await runtime.scope_view(scope['target_id'])
        assert len(calls) == 1 and len(authority) >= 2
    elif condition == 'never_stable':
        with pytest.raises(PlaywrightError, match='Execution context was destroyed'):
            await runtime.scope_view(scope['target_id'])
        assert len(calls) == 3
    else:
        with pytest.raises((ValueError, PlaywrightError), match='frame_source_changed|Unrelated protocol failure'):
            await runtime.scope_view(scope['target_id'])
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS') != '1', reason='Explicit isolated Chromium navigation test required')
async def test_real_paused_post_navigation_exports_only_a_new_stable_document(monkeypatch):
    from lead_app import native_frame_roles

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def html(self, body):
            raw=body.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            self.html('<p>Child</p>' if self.path=='/child' else '<form method=post action=/submit><button>Submit</button></form><iframe src=/child></iframe>')
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.html('<main id=fresh>New document</main><iframe src=/child></iframe>')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            asset=Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR','/missing'))/'chromium/chrome-linux64/chrome'
            options={'headless':True,'args':['--no-sandbox','--disable-background-networking']}
            if asset.is_file():
                options['executable_path']=str(asset)
            browser=await playwright.chromium.launch(**options)
            context=await browser.new_context()
            page=await context.new_page()
            root=await browser.new_browser_cdp_session()
            held, paused=[],asyncio.Event()
            async def request(event):
                if event['request']['method']=='POST':
                    held.append(event)
                    paused.set()
                else:
                    await root.send('Fetch.continueRequest',{'requestId':event['requestId']})
            root.on('Fetch.requestPaused',request)
            await root.send('Fetch.enable',{'patterns':[{'urlPattern':'*','requestStage':'Request'}]})
            origin='http://127.0.0.1:'+str(server.server_port)
            await page.goto(origin+'/home')
            cdp=await context.new_cdp_session(page)
            await cdp.send('Page.enable')
            target=(await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
            frame=(await cdp.send('Page.getFrameTree'))['frameTree']['frame']
            scope={'target_id':target,'task_id':'original-task','generation':1,
                'account_id':'synthetic-account','origins':[origin]}
            runtime=PersistentRuntime.__new__(PersistentRuntime)
            runtime.pages={target:{'scope':scope,'page':page}}
            runtime.gate=SimpleNamespace(documents={target:[frame['id'],frame['loaderId'],frame['url']]},
                targets={'session':{'targetId':target,'type':'page'}},owners={'session':target})
            runtime.forward_token=lambda:'live-original-token'
            checked=[]
            runtime.current_scope=lambda value:checked.append(True)
            def navigated(event):
                value=event['frame']
                if not value.get('parentId'):
                    runtime.gate.documents[target]=[value['id'],value['loaderId'],value['url']]
            cdp.on('Page.frameNavigated',navigated)
            documents=[]
            async def observe(current,lease,observed_page):
                # Real renderer/document read, with no foreign-origin grant.
                facts,document=await native_frame_roles.root_facts(current,lease,observed_page)
                documents.append(document)
                assert facts['parent_url']==observed_page.url
                return list(lease['origins'])
            monkeypatch.setattr(native_frame_roles,'observation',observe)
            old=copy.deepcopy(runtime.gate.documents[target])
            click=asyncio.create_task(page.locator('button').click(timeout=5000))
            await asyncio.wait_for(paused.wait(),2)
            view=asyncio.create_task(runtime.scope_view(target))
            await asyncio.sleep(.2)
            assert not view.done()
            await root.send('Fetch.continueRequest',{'requestId':held[0]['requestId']})
            try:
                await click
                result=await asyncio.wait_for(view,3)
                assert result['origins']==[origin]
                assert documents and documents[-1]==runtime.gate.documents[target] and documents[-1]!=old
                assert page.url==origin+'/submit' and len(checked)>=2
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
