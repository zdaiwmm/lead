"""Isolated synthetic manager bootstrap; never used by the product runtime."""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def private_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(value, default=str))
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    import uvicorn
    from lead_app import browser, native_runner
    from lead_app.browser_runtime import PersistentRuntime
    from lead_app.forward_proxy import BrowserForwardProxy
    from lead_app.native_action import NativeAction

    boot = json.loads(Path(sys.argv[1]).read_text())
    assert os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') == '1'
    assert os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    model = boot['model']
    assert model['model'] == 'fixture-transport'
    assert urlsplit(model['base_url']).hostname == '127.0.0.1'
    native_runner.resolve_model = lambda _: dict(model)

    async def fixture_connect(self, ip, port):
        destination = boot['destinations'].get(f'{ip}:{port}')
        if not destination:
            raise ValueError('synthetic_destination_not_registered')
        return await asyncio.open_connection('127.0.0.1', destination)

    BrowserForwardProxy.connect = fixture_connect
    original_start = PersistentRuntime.start

    async def start(self, upstream):
        result = await original_start(self, upstream)
        actor = self.actor
        proxy = actor.forward_proxy
        private_json(boot['ready'], {
            'pid': os.getpid(), 'ppid': os.getppid(), 'nonce': boot['nonce'],
            'epoch': actor.epoch, 'session': actor.session,
            'profile': actor.profile['id'], 'proxy_port': proxy.port,
            'proxy_auth': proxy.auth, 'boundary': proxy.boundary,
        })
        return result

    PersistentRuntime.start = start
    original_verify = NativeAction.verify_request

    async def verify(self, request, decision):
        async def hold():
            private_json(boot['cut'], {
                'pid': os.getpid(), 'nonce': boot['nonce'],
                'action_id': self.action_id, 'task_id': self.task_id,
                'epoch': self.actor.epoch,
            })
            # The root Fetch handler is awaiting this call; upstream APIRequest
            # cannot start until it returns. CAS position is selected below.
            await asyncio.Event().wait()
        stage = boot.get('cut_stage', 'after_claim' if boot.get('cut_after_claim') else None)
        if stage == 'before_claim':
            await hold()
        allowed = await original_verify(self, request, decision)
        if allowed and stage == 'after_claim':
            await hold()
        return allowed

    NativeAction.verify_request = verify
    endpoint = urlsplit(boot['endpoint'])
    assert endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1'
    uvicorn.run(browser.app, host='127.0.0.1', port=endpoint.port, log_level='error')


if __name__ == '__main__':
    main()
