"""Original connection/discovery consumers use observed identity fields."""
import os
import re

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.parametrize('mode',['login','wrong_subject'])
@pytest.mark.asyncio
async def test_original_connection_accepts_adjacent_self_entry_and_labeled_private_value(monkeypatch,mode):
    import native_connection_product as helper
    from lead_app import account_profiles

    original=helper.NativeBoard
    class ObservedLayoutBoard(original):
        def handler(self):
            handler=super().handler()
            send=handler.send
            def actual_layout(handler,status,body='',**kwargs):
                body=body.replace(' · <a rel="me" href="/account">Your account settings</a></p>',
                    ' · Your account settings</p><a rel="me" href="/account">Your account settings</a>')
                body=re.sub(r'<p>Account ID: <strong id="subject">([^<]+)</strong></p>',
                    r'<p id="subject">Account ID: \1</p>',body)
                return send(handler,status,body,**kwargs)
            handler.send=actual_layout
            return handler
    monkeypatch.setattr(helper,'NativeBoard',ObservedLayoutBoard)
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def recorded_identity(context):
        from lead_app.db import one, tx
        with tx(context['aid']) as c:
            return one(c,'SELECT connection_data FROM lead.takeover_sessions WHERE id=%s',
                (context['pending']['takeover_id'],))['connection_data']
    result=await helper.verify_login(monkeypatch,mode=mode,after_discovery=recorded_identity if mode=='login' else None)
    assert not result['privacy']['literal_in_model_request'] and not result['privacy']['literal_in_workdir']
    if mode=='login':
        assert result['task_state']=='succeeded'
        assert result['discovery']['outcome']=='completed' and len(result['discovery']['output']['targets'])==1
        assert result['account']['external_account_id'].startswith('member-')
        assert result['action_check']['self_value_prefix']=='Account ID: '
    else:
        assert result['task_state']=='waiting_user' and result['account']['external_account_id'] is None
    assert result['oracle']['posts']==[]
