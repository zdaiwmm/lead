import asyncio
from types import SimpleNamespace

import pytest
from lead_app import browser, telegram
from lead_app.account_profiles import for_platform
from lead_app.db import one, tx
from lead_app.growth.runtime import advance
from lead_app.growth.tools import prepare_channel
from test_growth_account_connection import login_work
from test_growth_channel_operation import channel_work


@pytest.mark.parametrize('same_profile',[False,True])
async def test_login_only_blocks_its_own_telegram_profile(growth_agent,monkeypatch,same_profile):
    ref,data,social=channel_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.social_accounts SET platform='telegram' WHERE id=%s",(social['id'],))
        c.execute("UPDATE lead.account_targets SET canonical_url='https://t.me/fixture',external_target_id='-20002' WHERE account_id=%s",(social['id'],))
    advance(ref)
    action=prepare_channel(growth_agent,ref['task_id'],data)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action['id'],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action['id'],))
    target_account=social['id'] if same_profile else for_platform(growth_agent,'facebook',create=True,new_binding=True)['account_id']
    login_work(growth_agent,target_account)
    opened=[]
    async def open_account(*args):
        opened.append(True)
    async def inspect_target(*args):
        return {'external_target_id':'-20002'}
    monkeypatch.setattr(telegram,'open_account',open_account)
    monkeypatch.setattr(telegram,'inspect_target',inspect_target)
    actor=SimpleNamespace(aid=growth_agent,profile={'id':social['profile_id']},lock=asyncio.Lock(),page=None,touch=lambda:None)
    result=await browser.Actor.publish_telegram(actor,action['id'])
    assert result['state']=='unknown'
    assert bool(opened) is not same_profile
    with tx(growth_agent) as c:
        assert one(c,'SELECT count(*) n FROM lead.action_attempts')['n']==0
