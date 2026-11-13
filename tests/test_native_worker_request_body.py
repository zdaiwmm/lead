"""SW request ownership requires the actual body from the same CDP session."""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('returned,allowed', [('body=exact', True), ('body=other', False), (None, False)])
async def test_streamed_worker_body_is_read_from_its_exact_network_session(returned, allowed):
    from lead_app.browser_runtime import NativeNetworkGate

    gate = NativeNetworkGate(None, None, authorize=None, claim=None, lost=None, owner_timeout=.001)
    gate.context_id = 'managed'
    gate.targets['worker-session'] = {'targetId': 'worker', 'type': 'service_worker', 'browserContextId': 'managed'}
    gate.network[('request', 'worker-session')] = {'request': {'url': 'https://board.test/posts', 'method': 'POST'}, 'type': 'Fetch'}
    calls = []

    async def send(method, params, session):
        calls.append((method, params, session))
        if returned is None:
            raise ValueError('cdp_command_rejected')
        return {'postData': returned}

    gate.send = send
    event = {'networkId': 'request', 'frameId': 'worker-fetch-frame',
             'request': {'url': 'https://board.test/posts', 'method': 'POST', 'hasPostData': True, 'postData': 'body=exact'}}
    if allowed:
        session, target = await gate.resolve_owner(event)
        assert session == 'worker-session' and target['targetId'] == 'worker'
    else:
        with pytest.raises(ValueError, match='request_owner_unresolved'):
            await gate.resolve_owner(event)
    assert calls == [('Network.getRequestPostData', {'requestId': 'request'}, 'worker-session')]


@pytest.mark.asyncio
async def test_body_read_cannot_resolve_ambiguous_workers_or_replace_a_recorded_body():
    from lead_app.browser_runtime import NativeNetworkGate

    gate = NativeNetworkGate(None, None, authorize=None, claim=None, lost=None, owner_timeout=.001)
    gate.context_id = 'managed'
    target = {'targetId': 'worker', 'type': 'service_worker', 'browserContextId': 'managed'}
    request = {'url': 'https://board.test/posts', 'method': 'POST', 'postData': 'body=exact'}
    calls = []

    async def send(*args):
        calls.append(args)
        return {'postData': 'body=exact'}

    gate.send = send
    gate.targets = {'one': target, 'two': dict(target, targetId='other-worker')}
    gate.network = {( 'request', session): {'request': {key: value for key, value in request.items() if key != 'postData'}} for session in gate.targets}
    with pytest.raises(ValueError, match='request_owner_unresolved'):
        await gate.resolve_owner({'networkId': 'request', 'frameId': 'sw-frame', 'request': request})
    assert not calls
    gate.targets.pop('two')
    gate.network.pop(('request', 'two'))
    gate.network[('request', 'one')]['request']['postData'] = 'body=wrong'
    with pytest.raises(ValueError, match='request_owner_unresolved'):
        await gate.resolve_owner({'networkId': 'request', 'frameId': 'sw-frame', 'request': request})
    assert not calls
