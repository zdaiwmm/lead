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
