"""Real Chromium/Unix CDP boundary proof; run only with a disposable profile.

Usage: python browser_namespace_cdp_probe.py WRAPPER CHROMIUM
Requires the already provisioned httpx/websockets runtime; no platform traffic.
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx
from websockets.asyncio.client import unix_connect


async def main(wrapper, binary):
    with tempfile.TemporaryDirectory(prefix='lead-cdp-boundary-') as folder:
        root = Path(folder)
        for name in ('profile', 'assets', 'control'):
            (root / name).mkdir(mode=0o700)
        proxy = socket.socket(socket.AF_UNIX)
        proxy.bind(str(root / 'forward.sock'))
        proxy.listen(32)
        nonce = os.urandom(24).hex()
        requests, attestations = [], []

        def serve(connection):
            with connection:
                connection.settimeout(5)
                data = b''
                while b'\r\n\r\n' not in data:
                    block = connection.recv(65536)
                    if not block:
                        return
                    data += block
                head, body = data.split(b'\r\n\r\n', 1)
                line = head.split(b'\r\n')[0].decode()
                headers = dict(row.decode().split(': ', 1) for row in head.split(b'\r\n')[1:] if b': ' in row)
                length = int(headers.get('Content-Length', '0'))
                while len(body) < length:
                    body += connection.recv(65536)
                if line == 'POST /__lead/attest HTTP/1.1':
                    assert hmac.compare_digest(headers['X-Lead-Proof'], hmac.new(bytes.fromhex(nonce), body, hashlib.sha256).hexdigest())
                    attestations.append(json.loads(body))
                    code, content = '200 OK', b'ok'
                elif line.startswith('GET http://fixture.test/'):
                    requests.append(line)
                    code, content = '200 OK', b'<html><body>isolated fixture</body></html>'
                else:
                    code, content = '403 Forbidden', b'fixture only'
                with contextlib.suppress(OSError):
                    connection.sendall(f'HTTP/1.1 {code}\r\nContent-Length: {len(content)}\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n'.encode() + content)

        def accept():
            while True:
                try:
                    connection, _ = proxy.accept()
                except OSError:
                    return
                threading.Thread(target=serve, args=(connection,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        with socket.socket() as port_picker:
            port_picker.bind(('127.0.0.1', 0))
            port = port_picker.getsockname()[1]
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'LEAD_REAL_BROWSER': str(Path(binary).resolve()), 'LEAD_BROWSER_PROFILE': str(root/'profile'),
               'LEAD_BROWSER_ASSETS': str(root/'assets'), 'LEAD_BROWSER_SOCKET': str(root/'forward.sock'),
               'LEAD_BROWSER_PROXY_PORT': '31415', 'LEAD_BOUNDARY_NONCE': nonce,
               'LEAD_BROWSER_CONTROL_DIRECTORY': str(root/'control'), 'LEAD_BROWSER_CDP_PORT': str(port)}
        args = ['/usr/bin/python3', wrapper, '--headless', '--no-sandbox', '--disable-gpu',
                '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update',
                '--no-first-run', '--no-default-browser-check', '--disable-quic',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                '--user-data-dir='+str(root/'profile'), '--proxy-server=http://127.0.0.1:31415',
                '--proxy-bypass-list=<-loopback>', '--remote-debugging-address=127.0.0.1',
                '--remote-debugging-port='+str(port), 'about:blank']
        log = open(root/'browser.log', 'w+')
        process = subprocess.Popen(args, env=env, stdout=log, stderr=log)
        control = str(root/'control/cdp.sock')
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=control), timeout=1) as client:
                version = None
                for _ in range(100):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read()[-4000:])
                    try:
                        response = await client.get('http://localhost/json/version')
                        response.raise_for_status()
                        version = response.json()
                        break
                    except (httpx.HTTPError, json.JSONDecodeError):
                        await asyncio.sleep(.1)
                assert version, 'CDP never became ready'
            assert attestations and attestations[0]['only_loopback'] and attestations[0]['direct_udp_blocked']
            assert attestations[0]['netns'] != os.readlink('/proc/self/ns/net')
            assert (root/'control/cdp.sock').stat().st_mode & 0o777 == 0o600
            with socket.socket() as host:
                host.settimeout(.3)
                assert host.connect_ex(('127.0.0.1', port)) != 0, 'CDP leaked onto host TCP'
            uri = version['webSocketDebuggerUrl']
            async with unix_connect(control, uri=uri) as first, unix_connect(control, uri=uri) as second:
                async def rpc(ws, method, params=None, session=None):
                    message = {'id': 1, 'method': method, 'params': params or {}}
                    if session:
                        message['sessionId'] = session
                    await ws.send(json.dumps(message))
                    while True:
                        answer = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        if answer.get('id') == 1:
                            assert 'error' not in answer, answer
                            return answer['result']
                async def page(ws, name):
                    target = (await rpc(ws, 'Target.createTarget', {'url': 'http://fixture.test/'+name}))['targetId']
                    session = (await rpc(ws, 'Target.attachToTarget', {'targetId': target, 'flatten': True}))['sessionId']
                    for _ in range(50):
                        result = await rpc(ws, 'Runtime.evaluate', {'expression': 'document.body && document.body.innerText', 'returnByValue': True}, session)
                        if result['result'].get('value') == 'isolated fixture':
                            return target
                        await asyncio.sleep(.1)
                    raise AssertionError(result)
                targets = await asyncio.gather(page(first, 'one'), page(second, 'two'))
                assert len(set(targets)) == 2 and len(requests) >= 2
                process.kill()
                await asyncio.to_thread(process.wait, 5)
                for ws in (first, second):
                    try:
                        while True:
                            await asyncio.wait_for(ws.recv(), 5)
                    except Exception as exc:
                        assert not isinstance(exc, asyncio.TimeoutError), 'control tunnel retained after parent death'
                print(json.dumps({'chromium': version['Browser'], 'unix_cdp': True, 'two_independent_targets': True,
                                  'fixture_pages': len(requests), 'network_attestation': attestations[0],
                                  'host_cdp_not_listening': True, 'control_eof_on_parent_death': True}))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            proxy.close()
            log.close()


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))
