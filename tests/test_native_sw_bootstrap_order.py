"""The page must hold the first worker script until its root Network observer exists."""
import asyncio
import json
import unittest

from lead_app.browser_runtime import NativeNetworkGate
from websockets.protocol import State


class BootstrapWire:
    """Model Chromium's page-only main-script throttle and delayed renderer ACK."""
    state = State.OPEN

    def __init__(self, *, root=None, child=None, before_network=None):
        self.queue = asyncio.Queue()
        self.commands = []
        self.terminal = asyncio.Event()
        self.result = None
        self.held = False
        self.pending_network = None
        self.created = False
        self.root = root or {'targetId': 'sw-target', 'type': 'service_worker', 'browserContextId': 'managed'}
        self.child = child or dict(self.root)
        self.before_network = before_network
        self.network_written = asyncio.Event()
        self.lifecycle_processed = asyncio.Event()
        self.reader_barrier_delivered = False
        self.detach_seen = asyncio.Event()
        self.request = {'url': 'https://fixture.test/sw.js', 'method': 'GET'}
        self.event('Target.attachedToTarget', {'sessionId': 'page-session', 'waitingForDebugger': True,
            'targetInfo': {'targetId': 'page', 'type': 'page', 'browserContextId': 'managed'}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.reader_barrier_delivered:
            self.lifecycle_processed.set()
        raw = await self.queue.get()
        if json.loads(raw).get('params', {}).get('_synthetic_reader_barrier'):
            self.reader_barrier_delivered = True
        return raw

    def event(self, method, params, session=None):
        self.queue.put_nowait(json.dumps({'method': method, 'params': params,
            **({'sessionId': session} if session else {})}))

    def acknowledge(self, command):
        self.queue.put_nowait(json.dumps({'id': command['id'], 'result': {}}))

    def fetch(self, observed):
        if observed:
            self.event('Network.requestWillBeSent', {'requestId': 'sw-target', 'type': 'Script',
                'request': self.request}, 'root-sw')
        self.event('Fetch.requestPaused', {'requestId': 'intercept', 'networkId': 'sw-target',
            'frameId': 'sw-target', 'resourceType': 'Other', 'request': self.request})

    async def send(self, raw):
        command = json.loads(raw)
        self.commands.append(command)
        method, session = command['method'], command.get('sessionId')
        if method == 'Target.setAutoAttach' and session == 'page-session' and not self.created:
            self.created = True
            self.held = any(item.get('type') == 'service_worker' and not item.get('exclude')
                for item in command['params']['filter'])
            self.event('Target.attachedToTarget', {'sessionId': 'root-sw', 'targetInfo': self.root,
                'waitingForDebugger': True})
            if self.held:
                self.event('Target.attachedToTarget', {'sessionId': 'page-sw', 'targetInfo': self.child,
                    'waitingForDebugger': True}, 'page-session')
            else:
                # RequestSent before Network.enable is not replayed by Chrome.
                self.fetch(observed=False)
        if method == 'Network.enable' and session == 'root-sw' and self.held:
            # No renderer exists before the script fetch; waiting for this ACK
            # before releasing the page's throttle would deadlock.
            self.pending_network = command
            if self.before_network:
                self.before_network(self)
                self.event('Runtime.consoleAPICalled', {'_synthetic_reader_barrier': True})
                # Suspend this transport write until the reader has processed
                # the injected detach/replacement, so the test checks observed
                # revocation rather than guessing at an event still in flight.
                await self.lifecycle_processed.wait()
            self.network_written.set()
            return
        if method == 'Runtime.runIfWaitingForDebugger' and session == 'page-sw':
            self.fetch(observed=self.pending_network is not None)
            if self.pending_network:
                self.acknowledge(self.pending_network)
                self.pending_network = None
            self.held = False
        if method == 'Target.detachFromTarget':
            self.detach_seen.set()
            if session != 'page-session' or command['params'] != {'sessionId': 'page-sw'}:
                self.queue.put_nowait(json.dumps({'id': command['id'], 'error': {'message': 'No session with given id'}}))
                return
            self.held = False
        if method in ('Fetch.continueRequest', 'Fetch.failRequest'):
            self.result = method
            self.terminal.set()
        self.acknowledge(command)


class BootstrapOrderTests(unittest.IsolatedAsyncioTestCase):
    def start_gate(self, wire):
        async def authorize(scope, target, request):
            self.assertIsNone(scope)
            self.assertEqual((target['targetId'], target['browserContextId']), ('sw-target', 'managed'))
            self.assertEqual(request['method'], 'GET')
            return {'kind': 'read'}

        gate = NativeNetworkGate(None, None, authorize=authorize, claim=None, lost=None, owner_timeout=.03)
        gate.ws = wire
        gate.context_id, gate.ready, gate.stopping = 'managed', True, True
        gate.roots['page'] = {'connection_ref': 'synthetic-login', 'account_id': 'synthetic-account'}
        reader = asyncio.create_task(gate.receive())

        async def close():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            for task in list(gate.tasks):
                task.cancel()
            await asyncio.gather(*list(gate.tasks), return_exceptions=True)
        self.addAsyncCleanup(close)
        return gate

    async def test_page_throttle_precedes_first_script_and_does_not_wait_for_renderer_ack(self):
        wire = BootstrapWire()
        gate = self.start_gate(wire)
        await asyncio.wait_for(wire.terminal.wait(), 1)
        self.assertEqual(wire.result, 'Fetch.continueRequest')
        workers = [row for row in wire.commands if row['method'] == 'Network.enable'
            and row.get('sessionId') in ('root-sw', 'page-sw')]
        self.assertEqual([row['sessionId'] for row in workers], ['root-sw'])
        self.assertEqual([session for (request, session) in gate.network if request == 'sw-target'], ['root-sw'])
        self.assertNotIn('page-sw', gate.targets)
        self.assertNotIn('page-sw', gate.owners)

    async def check_refused(self, wire):
        gate = self.start_gate(wire)
        await asyncio.wait_for(wire.network_written.wait(), 1)
        # An acknowledged command after the injected lifecycle events is a
        # deterministic reader barrier, not a timing sleep.
        await gate.send('Target.getTargets')
        self.assertFalse(any(row['method'] == 'Runtime.runIfWaitingForDebugger'
            and row.get('sessionId') == 'page-sw' for row in wire.commands))
        self.assertNotEqual(wire.result, 'Fetch.continueRequest')
        return gate

    async def test_same_url_different_worker_target_cannot_release_throttle(self):
        child = {'targetId': 'another-worker', 'type': 'service_worker', 'browserContextId': 'managed'}
        await self.check_refused(BootstrapWire(child=child))

    async def test_foreign_context_cannot_release_throttle(self):
        root = {'targetId': 'sw-target', 'type': 'service_worker', 'browserContextId': 'foreign'}
        child = {**root, 'browserContextId': 'managed'}
        await self.check_refused(BootstrapWire(root=root, child=child))

    async def test_detached_parent_cannot_release_or_lend_connection_scope(self):
        def detach(wire):
            wire.event('Target.detachedFromTarget', {'sessionId': 'page-session'})
        gate = await self.check_refused(BootstrapWire(before_network=detach))
        self.assertNotIn('page-session', gate.targets)
        self.assertNotIn('page-sw', gate.owners)

    async def test_detached_child_is_not_resumed_by_queued_setup(self):
        def detach(wire):
            wire.event('Target.detachedFromTarget', {'sessionId': 'page-sw'}, 'page-session')
        gate = await self.check_refused(BootstrapWire(before_network=detach))
        self.assertNotIn('page-sw', gate.worker_throttles)
        self.assertNotIn('page-sw', gate.initialized)

    async def test_replaced_root_session_cannot_use_old_network_start(self):
        def replace(wire):
            wire.before_network = None
            info = {'targetId': 'replacement', 'type': 'service_worker', 'browserContextId': 'managed'}
            wire.event('Target.attachedToTarget', {'sessionId': 'root-sw', 'targetInfo': info,
                'waitingForDebugger': True})
        gate = await self.check_refused(BootstrapWire(before_network=replace))
        self.assertEqual(gate.targets['root-sw']['targetId'], 'replacement')

    async def test_multiple_root_bindings_are_not_combined_into_one_owner(self):
        def duplicate(wire):
            wire.event('Target.attachedToTarget', {'sessionId': 'another-root', 'targetInfo': wire.root,
                'waitingForDebugger': True})
        await self.check_refused(BootstrapWire(before_network=duplicate))

    async def test_ambiguous_root_refusal_detaches_throttle_in_its_original_page_handler(self):
        def duplicate(wire):
            wire.event('Target.attachedToTarget', {'sessionId': 'another-root', 'targetInfo': wire.root,
                'waitingForDebugger': True})
        wire = BootstrapWire(before_network=duplicate)
        gate = await self.check_refused(wire)
        await asyncio.wait_for(wire.detach_seen.wait(), 1)
        await gate.send('Target.getTargets')
        detaches = [row for row in wire.commands if row['method'] == 'Target.detachFromTarget']
        self.assertEqual([(row.get('sessionId'), row['params']) for row in detaches],
            [('page-session', {'sessionId': 'page-sw'})])
        self.assertFalse(wire.held)
        self.assertFalse(any(row['method'] == 'Target.closeTarget' for row in wire.commands))
