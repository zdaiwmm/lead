"""Actual scoped SDK/Chrome image path, one synthetic HTTP response, no model API."""
import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import io
import json
import logging
import runpy
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class Pages:
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                color = 'rgb(230,10,180)' if self.path == '/other' else 'rgb(20,160,80)'
                label = 'OTHER TASK PAGE' if self.path == '/other' else 'CURRENT AUTHORIZED PAGE'
                raw = ('<!doctype html><html><body style="margin:0;background:white;font:20px sans-serif">'
                    '<div style="width:500px;height:150px;background:' + color + '">' + label + '</div>'
                    '<label>Observed field<input id=entry name=entry></label></body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


async def probe(args):
    import httpx
    from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
    from lead_app.browser_execution import ScopeViolation, scoped_session_class
    from PIL import Image
    from playwright.async_api import async_playwright

    logging.disable(logging.CRITICAL)
    pages, foreign = Pages(), Pages()
    original_client = httpx.AsyncClient
    captured = []
    with socket.socket() as chooser:
        chooser.bind(('127.0.0.1', 0))
        port = chooser.getsockname()[1]
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=str(args.chrome), headless=True,
                args=['--remote-debugging-port=' + str(port)])
            try:
                context = await browser.new_context(viewport={'width': 800, 'height': 600}, device_scale_factor=1)
                current = await context.new_page()
                await current.goto(pages.origin + '/current')
                other = await context.new_page()
                await other.goto(pages.origin + '/other')
                async def target(page):
                    session = await context.new_cdp_session(page)
                    try:
                        return (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
                    finally:
                        await session.detach()
                current_id, other_id = await target(current), await target(other)
                async with original_client(trust_env=False) as client:
                    endpoint = (await client.get(f'http://127.0.0.1:{port}/json/version')).json()['webSocketDebuggerUrl']
                scope = {'task_id': 'synthetic-owned-task', 'generation': 1, 'target_id': current_id,
                    'targets': {current_id: current_id}, 'origins': [pages.origin]}

                async def manager(request):
                    operation = json.loads(request.content)['operation']
                    if operation == 'scope':
                        return httpx.Response(200, json=dict(scope))
                    assert operation == 'preflight', operation
                    return httpx.Response(200, json={'owner_required': False})

                async def upstream(request):
                    assert str(request.url) == 'https://api.deepseek.com/chat/completions'
                    data = json.loads(request.content)
                    images = [part for message in data['messages'] if message['role'] == 'user'
                        and isinstance(message.get('content'), list)
                        for part in message['content'] if part.get('type') == 'image_url']
                    assert len(images) == 1
                    raw = base64.b64decode(images[0]['image_url']['url'].split(',', 1)[1], validate=True)
                    with Image.open(io.BytesIO(raw)) as image:
                        pixels = image.convert('RGB').tobytes()
                    triples = list(zip(pixels[::3], pixels[1::3], pixels[2::3]))
                    captured.append({'request_model': data['model'], 'user_images': len(images),
                        'owned_page_pixels': triples.count((20, 160, 80)),
                        'other_page_pixels': triples.count((230, 10, 180)),
                        'image_sha256': hashlib.sha256(raw).hexdigest()})
                    tool = data.get('tools', [{'function': {'name': 'AgentOutput'}}])[0]['function']['name']
                    output = {'thinking': 'Observe only this original fixture page.',
                        'evaluation_previous_goal': 'The assigned page was observed.', 'memory': 'Synthetic fixture.',
                        'next_goal': 'Finish without changing the page.', 'current_plan_item': 0,
                        'plan_update': ['Observe the assigned page.'],
                        'action': [{'done': {'text': 'Original fixture observed.', 'success': True}}]}
                    return httpx.Response(200, json={'id': 'synthetic-only', 'object': 'chat.completion',
                        'model': 'deepseek-flash', 'choices': [{'index': 0, 'message': {'role': 'assistant',
                        'content': None, 'tool_calls': [{'id': 'synthetic-call', 'type': 'function',
                        'function': {'name': tool, 'arguments': json.dumps(output)}}]}, 'finish_reason': 'tool_calls'}],
                        'usage': {'prompt_tokens': 1024, 'completion_tokens': 7, 'total_tokens': 1031}})

                namespace = runpy.run_path(str(args.runner), run_name='isolated_vision_probe')
                main = namespace['main']
                globals_ = main.__globals__
                globals_['httpx'] = SimpleNamespace(AsyncClient=lambda **kwargs: original_client(
                    transport=httpx.MockTransport(manager), **kwargs))
                def provider_client(data, timeout, meter):
                    async def account(request):
                        try:
                            await meter.measure_request(request)
                        except ValueError:
                            value = json.loads(request.content)
                            shape = {'model': value.get('model'), 'messages': [
                                {'role': item.get('role'), 'content_type': type(item.get('content')).__name__,
                                 'parts': [{'type': part.get('type'), 'keys': sorted(part),
                                    'image_keys': sorted(part.get('image_url', {})),
                                    'detail': part.get('image_url', {}).get('detail')}
                                    for part in item.get('content', [])] if isinstance(item.get('content'), list) else []}
                                for item in value.get('messages', [])]}
                            (args.output / 'rejected-request-shape.json').write_text(json.dumps(shape, indent=2))
                            raise
                    return original_client(transport=httpx.MockTransport(upstream), event_hooks={
                        'request': [account], 'response': [meter.measure_response]})
                globals_['provider_http_client'] = provider_client
                data = {'native_scope': {**scope, 'cdp_url': endpoint, 'headers': {}},
                    'native_kind': 'connection', 'native_goal': 'Observe the assigned synthetic page and finish. Do not change anything.',
                    'agent_id': '00000000-0000-4000-8000-000000000030', 'run_id': 'synthetic-run',
                    'gateway_endpoint': 'http://manager.fixture', 'gateway_token': 'synthetic-only',
                    'command_path': 'native/command', 'model_socket': None,
                    'model': {'provider': 'deepseek', 'model': 'deepseek-flash',
                        'base_url': 'https://api.deepseek.com', 'key': 'synthetic-only-no-provider'},
                    'max_requests': 8, 'max_total_tokens': 40000, 'max_output_tokens': 1800,
                    'max_steps': 1, 'workdir': str(args.output / 'worker')}
                result = await asyncio.wait_for(main(data), 45)
                assert result['success'], result.get('errors')
                assert len(captured) == 1 and captured[0]['owned_page_pixels'] > 20000
                assert captured[0]['other_page_pixels'] == 0
                assert result['usage']['vision_images_sent'] == 1
                assert result['usage']['request_count'] == result['usage']['entry_count'] == 1
                assert not result['usage']['billing_unknown']

                false_session = scoped_session_class()(cdp_url=endpoint, keep_alive=True,
                    enable_default_extensions=False, permissions=[], accept_downloads=False)
                async def read_scope():
                    return dict(scope)
                await false_session.start_scoped(scope, read_scope, enable_screenshots=False)
                try:
                    raw_captures = []
                    original_capture = ScreenshotWatchdog.on_ScreenshotEvent
                    async def counted_capture(session, event):
                        raw_captures.append(1)
                        return await original_capture(session, event)
                    # Observe the real event path; this wrapper keeps native capture behavior.
                    with patch.object(ScreenshotWatchdog, 'on_ScreenshotEvent', counted_capture):
                        state = await false_session.get_browser_state_summary(include_screenshot=True)
                    assert not raw_captures
                    assert state.screenshot is None
                    cached = false_session._cached_browser_state_summary
                    assert cached is None or cached.screenshot is None
                    try:
                        await false_session.get_or_create_cdp_session(other_id)
                    except ScopeViolation:
                        wrong_page_denied = True
                    else:
                        raise AssertionError('Another task page was accepted.')
                    scope['origins'] = []
                    try:
                        await false_session.get_browser_state_summary(include_screenshot=True)
                    except ScopeViolation:
                        revoked_denied = True
                    else:
                        raise AssertionError('Revoked scope still observed.')
                    scope['origins'] = [pages.origin]
                    await current.evaluate('(url)=>{const n=document.createElement("iframe");n.src=url;document.body.append(n)}', foreign.origin + '/other')
                    await current.wait_for_function('document.querySelector("iframe").contentWindow!==null')
                    await asyncio.sleep(.2)
                    try:
                        await false_session.get_browser_state_summary(include_screenshot=True)
                    except ScopeViolation:
                        foreign_frame_denied = True
                    else:
                        raise AssertionError('An unapproved frame was accepted.')
                finally:
                    false_session._intentional_stop = True
                    await asyncio.wait_for(false_session.stop(), 3)
                assert not current.is_closed() and not other.is_closed()
                return {'sdk_version': importlib.metadata.version('browser-use'), 'python_version': list(sys.version_info[:2]),
                    'actual_model_http_requests': len(captured), 'paid_requests': 0, 'request': captured[0],
                    'worker_usage': result['usage'], 'vision_false_cache_empty': True,
                    'vision_false_raw_capture_count': len(raw_captures),
                    'other_task_denied': wrong_page_denied, 'revoked_scope_denied': revoked_denied,
                    'foreign_frame_denied': foreign_frame_denied, 'original_pages_alive': True}
            finally:
                await browser.close()
    finally:
        pages.close()
        foreign.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('sdk-site', 'worker-site', 'api', 'runner', 'chrome', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.sdk_site.resolve(strict=True)), str(args.worker_site.resolve(strict=True)), str(args.api.resolve(strict=True))]
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = asyncio.run(probe(args))
    result['runner_sha256'] = hashlib.sha256(args.runner.read_bytes()).hexdigest()
    result['scope_sha256'] = hashlib.sha256((args.api / 'lead_app/browser_execution.py').read_bytes()).hexdigest()
    path = args.output / 'proof.json'
    path.write_text(json.dumps(result, indent=2))
    path.chmod(0o600)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
