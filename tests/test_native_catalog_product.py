"""Catalog namespace uses the same observed native identity/method consumers.

The source is the disposable board, not a real catalog platform acceptance run.
Legacy adapters and their profiles are untouched.
"""
import os
from urllib.parse import urlsplit

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_native_catalog_identity_discovery_and_first_publication_share_generic_producers(monkeypatch):
    import native_connection_product as helper
    import test_native_action_product as action
    from lead_app import adapters

    original_begin=helper.begin
    original_verify=helper.verify_login

    async def catalog_begin(aid,platform,**kwargs):
        if platform=='web' and urlsplit(kwargs.get('site_url','')).hostname=='93.184.215.14':
            platform='facebook'
        return await original_begin(aid,platform,**kwargs)

    async def checked_verify(*args,**kwargs):
        from lead_app.db import one, tx
        publish=kwargs['after_discovery']
        async def observed(context):
            with tx(context['aid']) as c:
                account=one(c,'SELECT platform,subject_ref FROM lead.social_accounts WHERE id=%s',(context['pending']['account_id'],))
                private=one(c,'SELECT connection_data FROM lead.takeover_sessions WHERE id=%s',(context['pending']['takeover_id'],))['connection_data']
            assert account['platform']=='facebook' and account['subject_ref']['namespace']=='facebook'
            assert account['subject_ref']['id_kind']=='stable'
            assert private['self_selector']=='#subject' and private['self_subject']==context['board'].state.username
            return await publish(context)
        kwargs['after_discovery']=observed
        result=await original_verify(*args,**kwargs)
        assert result['discovery']['outcome']=='completed'
        assert result['action_check']['state']=='confirmed'
        return result

    def no_adapter(*args,**kwargs):
        raise AssertionError('Native generic identity and actions must not silently require a legacy adapter')

    monkeypatch.setattr(helper,'begin',catalog_begin)
    monkeypatch.setattr(helper,'verify_login',checked_verify)
    monkeypatch.setattr(adapters,'get_adapter',no_adapter)
    await action.test_native_first_publication_uses_actual_dom_receipt(monkeypatch)
