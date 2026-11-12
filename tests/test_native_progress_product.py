"""The official SDK loop stops on actual unchanged state and one correction."""
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_native_sdk_no_progress_and_one_directed_correction_are_bounded(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import discover
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def bounded(context):
        aid=context['aid']
        checks={}
        for mode,expected,reason in [('no_progress',3,'no_progress'),('correction',2,'one_correction_exhausted')]:
            with tx(aid) as c:
                targets=c.execute('SELECT * FROM lead.account_targets ORDER BY id').fetchall()
                grants=c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()
                ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                    payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),dedupe_key='native-bound-'+mode))
            claim=advance(ref)['claim']
            with tx(aid) as c:
                task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            start=len(context['model_calls'])
            context['workflow'].update(kind=mode,step=0)
            result=await discover(aid,task)
            finish(claim,result)
            assert len(context['model_calls'])-start==expected
            assert result.output['targets']==[] and result.output['missing_conditions']
            with tx(aid) as c:
                progress=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['native_progress']
                assert progress['stopped_reason']==reason
                assert c.execute('SELECT * FROM lead.account_targets ORDER BY id').fetchall()==targets
                assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()==grants
            assert progress['no_progress']==3 if mode=='no_progress' else progress['corrections']==2
            assert len(progress['state_hash'])==64
            checks[mode]=expected
        return checks
    result=await verify_login(monkeypatch,after_discovery=bounded)
    assert result['action_check']=={'no_progress':3,'correction':2}
