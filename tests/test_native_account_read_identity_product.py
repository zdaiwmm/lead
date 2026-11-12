"""One real Linux account-read identity boundary, using only fixture A.

A real original login binds A. The local server then reports a different current
private subject while DB binding and old DOM remain A. The real research tool
must reject before returning or persisting that private body as A's source.
"""
import json
import os
import sys
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required',
)


@pytest.mark.asyncio
async def test_account_read_rejects_current_private_subject_that_differs_from_binding(monkeypatch):
    from lead_app import account_profiles, browser
    from lead_app.browser_tools import BrowserTools
    from lead_app.db import all_rows, one, tx
    from lead_app.growth import runtime, tools
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.store import submit_work
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    changed_subject = 'SYNTHETIC-PRIVATE-DIFFERENT-SUBJECT'

    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError('This account-read case does not need a research model')
    monkeypatch.setattr(tools, 'run_model', forbidden_model)

    async def check(context):
        aid, pending, board = context['aid'], context['pending'], context['board']
        account_id = str(pending['account_id'])
        actor = await browser.gateway.get(aid, context['task']['input']['spec']['payload']['profile_id'])
        controller = actor.connection_pages[str(pending['takeover_id'])]
        assert context['result'].outcome == 'completed'
        with tx(aid) as connection:
            original = one(connection, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (pending['takeover_id'],))
            account = one(connection, 'SELECT * FROM lead.social_accounts WHERE id=%s', (account_id,))
            assert original['state'] == 'ended' and original['completed_account_id'] == account['id']
            assert not all_rows(connection, "SELECT id FROM lead.takeover_sessions WHERE state IN ('active','requested','ending')")
        assert account['state'] == 'connected' and account['external_account_id'] == board.state.username
        assert await controller.page.locator('#subject').inner_text() == board.state.username
        saved_sessions = dict(board.state.sessions)
        assert saved_sessions
        # Same genuine HttpOnly cookie, changed server-side session identity.
        # Preserve the stale A DOM to ensure it cannot establish current self.
        board.state.sessions = {key: changed_subject for key in saved_sessions}
        observed = []
        original_command = BrowserTools.command

        async def capture_real_command(tool, operation, args=None):
            result = await original_command(tool, operation, args)
            observed.append((operation, result))
            return result
        monkeypatch.setattr(BrowserTools, 'command', capture_real_command)
        try:
            with tx(aid) as connection:
                ref = submit_work(connection, aid, TaskSpec(kind='research', purpose='account-read-identity',
                    account_id=account_id, payload={'access_mode': 'account', 'account_id': account_id,
                        'urls': [board.origin + '/account'], 'question': 'Read the current private account page'},
                    review_at=utcnow()+timedelta(hours=1)))
            claim = runtime.advance(ref)['claim']
            before_model = len(context['model_calls'])
            result = await runtime.execute(claim)
            assert runtime.finish(claim, result)['accepted_control']
            with tx(aid) as connection:
                task = one(connection, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
                run = one(connection, 'SELECT * FROM lead.browser_runs WHERE task_id=%s', (ref['task_id'],))
                documents = all_rows(connection, "SELECT d.source_type,k.body FROM lead.knowledge_documents d JOIN lead.knowledge_chunks k ON k.document_id=d.id WHERE d.source_ref=%s", (board.origin + '/account',))
                bound = one(connection, 'SELECT state,external_account_id FROM lead.social_accounts WHERE id=%s', (account_id,))
            assert run and str(run['profile_id']) == str(actor.profile['id'])
            assert str(task['input']['spec']['account_id']) == account_id
            assert bound == {'state': 'connected', 'external_account_id': board.state.username}
            dto = observed[0][1] if observed else {}
            diagnostics = {'dto_fields': sorted(dto), 'dto_error': dto.get('error'),
                'dto_contains_other_private_subject': changed_subject in json.dumps(dto),
                'persisted_other_private_source': any(changed_subject in document['body'] for document in documents),
                'research_outcome': result['outcome'], 'task_state': task['state'],
                'same_bound_account_and_profile': True, 'login_takeover_ended': True,
                'new_model_requests': len(context['model_calls'])-before_model,
                'wrong_subject_reason': 'wrong_subject' in json.dumps(dto)}
            print('ACCOUNT_READ_IDENTITY_DIAGNOSTICS ' + json.dumps(diagnostics, sort_keys=True))
            assert not diagnostics['dto_contains_other_private_subject'], diagnostics
            assert not diagnostics['persisted_other_private_source'], diagnostics
            assert diagnostics['dto_error'] and diagnostics['wrong_subject_reason'], diagnostics
            return diagnostics
        finally:
            board.state.sessions = saved_sessions

    await verify_login(monkeypatch, mode='account_read', owner_hook=check)
