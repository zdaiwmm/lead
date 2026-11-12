from datetime import timedelta

from lead_app.account_intents import set_intent
from lead_app.db import one, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import submit_work
from lead_app.growth.tools import prepare_channel
from test_growth_channel_operation import channel_work
from test_growth_unknown_receipts import facebook_work


def test_missing_actual_attempt_does_not_verify_first_business_format(growth_agent):
    with facebook_work(growth_agent) as (ref,data,account):
        claim = advance(ref)["claim"]
        action = prepare_channel(growth_agent,ref["task_id"],data)
        with tx(growth_agent) as c:
            c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action["id"],))
            c.execute("""UPDATE lead.actions SET state='confirmed',external_id='synthetic-unproved',
                evidence_ref='synthetic:no-browser-proof',confirmed_at=now() WHERE id=%s""",(action["id"],))
            assert not one(c,"SELECT lead.growth_receipt_valid(%s) ok",(action["id"],))["ok"]
            assert not one(c,"SELECT lead.growth_confirm_publication(%s) ok",(action["id"],))["ok"]
        finish(claim,WorkResult(outcome="completed",output={"publication_status":"confirmed"}))
        with tx(growth_agent) as c:
            assert one(c,"SELECT state FROM lead.tasks WHERE id=%s",(ref["task_id"],))["state"]=="failed"
            assert not one(c,"SELECT verified_permissions FROM lead.account_targets WHERE account_id=%s",(account["id"],))["verified_permissions"].get("publishing_verified_formats")


def test_renamed_task_cannot_repeat_but_explicit_resume_can_replace_unsubmitted_intent(growth_agent):
    ref,data,account = channel_work(growth_agent)
    with tx(growth_agent) as c:
        spec = TaskSpec.model_validate(one(c,"SELECT input FROM lead.tasks WHERE id=%s",(ref["task_id"],))["input"]["spec"])
        renamed = spec.model_copy(update={"purpose":"another-title","dedupe_key":"new-title","review_at":utcnow()+timedelta(hours=2)})
        assert submit_work(c,growth_agent,renamed)==ref
    advance(ref)
    original = prepare_channel(growth_agent,ref["task_id"],data)
    set_intent(growth_agent,account["id"],"paused",source="owner_command",source_ref="owner:pause-before-submit")
    set_intent(growth_agent,account["id"],"running",source="owner_command",source_ref="owner:resume-before-submit")
    with tx(growth_agent) as c:
        replacement = submit_work(c,growth_agent,renamed)
        assert replacement["task_id"]!=ref["task_id"]
        assert one(c,"SELECT state FROM lead.actions WHERE id=%s",(original["id"],))["state"]=="canceled"
