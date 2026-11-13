"""The paid driver follows the existing discovery checkpoint, never retries failures."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [None, 'owner', 'failed', 'unknown', 'generation', 'checkpoint',
                                   'third_fragment', 'no_images', 'billing', 'later_admission', 'three_fragments'])
async def test_discovery_continues_only_the_same_valid_checkpoint(monkeypatch, tmp_path, blocked):
    from lead_app import config
    from lead_app.growth import runtime

    from scripts.evaluate_native_browser import AcceptanceStop, NativeCase

    monkeypatch.setattr(config, 'STATE', tmp_path)
    ref = {'task_id': 'original-task', 'task_generation': 1}
    calls, admissions, waits = [], [], []
    due = datetime.now(timezone.utc).isoformat()
    checkpoint = {'stage': 'native_discovery', 'url': 'http://synthetic.test/post',
                  'scroll': [0, 0], 'execution_ref': {'profile_id': 'same-profile'}}
    wait = {'reason': 'bounded_browser_fragment', 'producer': 'account_connection',
            'recovery_action': 'recheck', 'responsibility': 'product', 'check_at': due,
            'episode_root': ref['task_id']}
    row = {'id': ref['task_id'], 'type': 'growth.account', 'state': 'planned',
           'input': {'task_generation': 1, 'spec': {'purpose': 'discover'}},
           'result': {'runtime': {'checkpoint': checkpoint, 'wait': wait,
                                 'native_progress': {'fragments': 1}}}}
    first = {'outcome': 'waiting', 'failure_code': None, 'checkpoint': copy.deepcopy(checkpoint),
             'wait': copy.deepcopy(wait)}
    if blocked in ('failed', 'unknown'):
        first['outcome'] = blocked
    elif blocked == 'owner':
        first['wait']['responsibility'] = 'owner'
        row['result']['runtime']['wait'] = first['wait']
    elif blocked == 'generation':
        row['input']['task_generation'] = 2
    elif blocked == 'checkpoint':
        row['result']['runtime']['checkpoint'] = {**checkpoint, 'url': 'http://changed.test'}
    elif blocked == 'third_fragment':
        row['result']['runtime']['native_progress']['fragments'] = 3

    class Case(NativeCase):
        def row(self, _):
            return row

        def ledger(self):
            return ([{'id': str(n), 'state': 'settled', 'units': 200000}
                     for n in range(len(calls))],
                    [{'reservation_id': str(n), 'units': 20, 'details': {'requests': 1}}
                     for n in range(len(calls))])

    def advance(actual, **kwargs):
        admissions.append(dict(actual))
        return {'state': 'ready', 'claim': {}}

    async def execute(_):
        number = len(calls)
        calls.append(number)
        folder = tmp_path/'browser-use-runs'/str(number)
        folder.mkdir(parents=True)
        (folder/'result.json').write_text(json.dumps({'usage': {
            'request_count': 1, 'entry_count': 1, 'total_tokens': 20,
            'billing_unknown': blocked == 'billing', 'unmeasured_requests': 0,
            'vision_images_sent': 0 if blocked == 'no_images' else 1}}))
        if number == 0 or blocked == 'three_fragments' and number == 1:
            row['result']['runtime']['native_progress']['fragments'] = number+1 if blocked == 'three_fragments' else row['result']['runtime']['native_progress']['fragments']
            return first
        row['state'] = 'succeeded'
        return {'outcome': 'completed', 'output': {'targets': [{'operation': 'reply'}]}}

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(runtime, 'advance', advance)
    monkeypatch.setattr(runtime, 'execute', execute)
    monkeypatch.setattr(runtime, 'finish', lambda *args: None)
    monkeypatch.setattr('scripts.evaluate_native_browser.asyncio.sleep', sleep)
    site = SimpleNamespace(case_id='continuation', snapshot=lambda: {'requests': []})
    case = Case('synthetic', site, 'http://unused', output=tmp_path/'case.json', require_visual=True)
    if blocked not in (None, 'three_fragments', 'later_admission'):
        with pytest.raises(AcceptanceStop, match='(?:bounded_stage_failed|visual_image_missing_or_invalid):discover_reply'):
            await case.perform('discover_reply', ref)
        assert len(calls) == 1 and not waits
    elif blocked == 'later_admission':
        def later(actual, **kwargs):
            return advance(actual, **kwargs) if not calls else {'state': 'blocked'}
        monkeypatch.setattr(runtime, 'advance', later)
        with pytest.raises(AcceptanceStop, match='task_not_admitted:discover_reply:blocked'):
            await case.perform('discover_reply', ref)
        assert len(calls) == 1 and len(waits) == 1
        assert len(case.report['stages'][0]['fragments']) == 1
        assert case.report['stages'][0]['requests'] == 1
    else:
        result = await case.perform('discover_reply', ref)
        assert result['outcome'] == 'completed'
        count = 3 if blocked == 'three_fragments' else 2
        assert len(calls) == count and len(waits) == count-1
        assert admissions == [ref]*count
        assert len(case.report['stages']) == 1
        stage = case.report['stages'][0]
        assert stage['requests'] == count and stage['reservation_units'] == 200000*count
        assert [item['outcome'] for item in stage['fragments']] == ['waiting']*(count-1)+['completed']
        assert [item['fragment'] for item in stage['fragments']] == list(range(1, count+1))
