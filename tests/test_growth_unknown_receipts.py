from contextlib import contextmanager
from datetime import timedelta

import psycopg
import pytest
from lead_app import browser, domain, social_publish
from lead_app.adapters.catalog import SPECS
from lead_app.adapters.web import WebAdapter
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx
from lead_app.growth.contracts import WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.tools import prepare_channel
from psycopg.rows import dict_row
from test_adapters import TARGETS, FixtureSite
from test_growth_channel_operation import channel_work


@contextmanager
def facebook_work(aid,entry=None):
    ref,data,account = channel_work(aid,entry)
    with tx(aid) as c:
        c.execute("UPDATE lead.social_accounts SET platform='facebook',external_account_id='10001' WHERE id=%s",(account["id"],))
        c.execute("""UPDATE lead.account_targets SET target_type='page',external_target_id='10002',canonical_url=%s,
            verified_permissions=%s WHERE account_id=%s""",(TARGETS["facebook"],j({"actions":["publish"],"adapter_version":SPECS["facebook"].version}),account["id"]))
    with psycopg.connect(**database_kwargs(owner=True),row_factory=dict_row) as c:
        capability = one(c,"SELECT * FROM lead.platform_capabilities WHERE platform='facebook' AND action_type='publish' AND target_type='page'")
        c.execute("""UPDATE lead.platform_capabilities SET permission_state='approved',technical_state='unverified',adapter_version=%s
            WHERE platform='facebook' AND action_type='publish' AND target_type='page'""",(SPECS["facebook"].version,))
    try:
        yield ref,data,account
    finally:
        with psycopg.connect(**database_kwargs(owner=True)) as c:
            c.execute("""UPDATE lead.platform_capabilities SET permission_state=%s,technical_state=%s,
                adapter_version=%s,evidence_ref=%s,reviewed_at=%s
                WHERE platform='facebook' AND action_type='publish' AND target_type='page'""",tuple(capability[key] for key in
                    ("permission_state","technical_state","adapter_version","evidence_ref","reviewed_at")))


@pytest.mark.asyncio
async def test_real_submit_lost_ack_pause_and_late_receipt_do_not_repeat(growth_agent,monkeypatch):
    with facebook_work(growth_agent) as (ref,data,account):
        await real_submit(growth_agent,monkeypatch,ref,data,account)


async def real_submit(growth_agent,monkeypatch,ref,data,account):
    gateway = browser.Gateway()
    site = FixtureSite("facebook")
    try:
        actor = await gateway.get(growth_agent,account["profile_id"])
        await actor.context.route("**/*",site.route)
        await actor.context.add_cookies([{"name":"c_user","value":"10001","url":"https://www.facebook.com"}])
        old_claim = advance(ref)["claim"]
        action = prepare_channel(growth_agent,ref["task_id"],data)
        assert action["intent"]["verification_required"]
        receipt = WebAdapter.receipt
        dropped = False
        async def lost(self,*args,**kwargs):
            nonlocal dropped
            if not dropped:
                dropped = True
                raise ConnectionError("Synthetic lost acknowledgement after the actual local click")
            return await receipt(self,*args,**kwargs)
        monkeypatch.setattr(WebAdapter,"receipt",lost)
        result = await social_publish.execute(actor,str(action["id"]),growth_claim=old_claim)
        assert result["state"]=="unknown" and len(site.posts)==1
        finish(old_claim,WorkResult(outcome="unknown"))
        domain.set_control(growth_agent,"pause")
        current_claim = advance(ref,now=utcnow()+timedelta(minutes=6))["claim"]
        actual = await social_publish.execute(actor,str(action["id"]))
        assert actual["state"]=="confirmed" and len(site.posts)==1
        late = finish(old_claim,WorkResult(outcome="completed",evidence_refs=[{"kind":"action","id":action["id"]}]))
        assert not late["accepted_control"] and late["evidence_recorded"]
        finish(current_claim,WorkResult(outcome="completed",evidence_refs=[{"kind":"action","id":action["id"]}]))
        with tx(growth_agent) as c:
            assert one(c,"SELECT desired_state FROM lead.agents WHERE id=%s",(growth_agent,))["desired_state"]=="paused"
            assert one(c,"SELECT count(*) n FROM lead.actions WHERE task_id=%s",(ref["task_id"],))["n"]==1
            assert one(c,"SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s",(action["id"],))["n"]==1
            assert one(c,"SELECT lead.growth_confirm_publication(%s) ok",(action["id"],))["ok"]
            permissions = one(c,"SELECT verified_permissions FROM lead.account_targets WHERE account_id=%s",(account["id"],))["verified_permissions"]
            assert permissions["publishing_verified_formats"]==["text"]
        assert not site.uploads
    finally:
        await gateway.close(growth_agent)
