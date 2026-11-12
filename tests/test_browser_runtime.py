"""Real local sockets exercise manager-owned authenticated CDP transport."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from lead_app.browser_runtime import ControlBridge
from websockets.asyncio.client import connect
from websockets.asyncio.server import unix_serve
from websockets.exceptions import InvalidStatus


class ControlBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_token_and_closes_existing_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'cdp.sock')
            async def echo(ws):
                async for data in ws:
                    await ws.send(data)
            async with unix_serve(echo, path):
                bridge = await ControlBridge(path, '/devtools/browser/test').start()
                try:
                    with self.assertRaises(InvalidStatus):
                        async with connect(bridge.url):
                            self.fail('unauthenticated control accepted')
                    async with connect(bridge.url, additional_headers=bridge.headers) as first, connect(bridge.url, additional_headers=bridge.headers) as second:
                        await first.send('one')
                        await second.send('two')
                        self.assertEqual(await first.recv(), 'one')
                        self.assertEqual(await second.recv(), 'two')
                        await bridge.close()
                        await asyncio.wait_for(first.wait_closed(), 2)
                        await asyncio.wait_for(second.wait_closed(), 2)
                finally:
                    await bridge.close()


if __name__ == '__main__':
    unittest.main()


class RequestOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_request_id_never_borrows_other_frame_and_ambiguity_fails(self):
        from lead_app.browser_runtime import NativeNetworkGate
        gate = NativeNetworkGate(None,None,authorize=None,claim=None,lost=None,owner_timeout=.001)
        request = {'url':'https://fixture.test/same','method':'GET'}
        gate.targets = {'one':{'targetId':'page-1'},'two':{'targetId':'page-2'}}
        gate.network = {('shared','one'):{'request':request,'frameId':'frame-1'},
                        ('shared','two'):{'request':request,'frameId':'frame-2'}}
        event = {'networkId':'shared','request':request,'frameId':'frame-1'}
        session, target = await gate.resolve_owner(event)
        self.assertEqual((session,target['targetId']),('one','page-1'))
        gate.network[('shared','two')]['frameId']='frame-1'
        with self.assertRaisesRegex(ValueError,'request_owner_ambiguous'):
            await gate.resolve_owner(event)
        gate.network.clear()
        with self.assertRaisesRegex(ValueError,'request_owner_unresolved'):
            await gate.resolve_owner(event)
