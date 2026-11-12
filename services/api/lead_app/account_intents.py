"""Owner command boundary. Reauthentication never overrides an explicit pause."""
import json

from .db import audit, emit, one, require_agent, tx
from .identity_store import auth_tx
from .scope import current_tenant, principal


def auth_scope(c, aid):
    who = principal()
    allowed = who.get("agent_ids") if who else None
    c.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.agent_id',%s,true),set_config('app.allowed_agent_ids',%s,true)",
              (current_tenant(), str(aid), json.dumps(allowed) if allowed is not None else "*"))


def set_intent(aid, account_id, desired, *, source, source_ref, expected_revision=None, scope=None):
    if source not in ("connection", "owner_command", "verified_migration"):
        raise ValueError("intent_source")
    with tx(aid) as c:
        require_agent(c, aid)
        if not one(c, "SELECT id FROM lead.social_accounts WHERE id=%s", (account_id,)):
            raise ValueError("account_scope")
    with auth_tx() as c:
        auth_scope(c, aid)
        revision = c.execute("SELECT lead.growth_set_intent(%s,%s,%s,%s,%s,%s,%s)",
                             (aid, account_id, desired, source, source_ref, expected_revision,
                              __import__("psycopg").types.json.Jsonb(scope) if scope is not None else None)).fetchone()
        revision = next(iter(revision.values()))
    with tx(aid) as c:
        audit(c, aid, "account.intent_changed", {"account_id": str(account_id), "revision": revision,
                                                "desired": desired, "source": source, "source_ref": source_ref})
        # One-way projection for explicitly named legacy consumers only.
        c.execute("""UPDATE lead.social_accounts SET automation_enabled=(i.desired='running')
          FROM lead.account_operating_intents i WHERE social_accounts.id=i.account_id AND social_accounts.id=%s""", (account_id,))
        actual = one(c,"SELECT desired,revision FROM lead.account_operating_intents WHERE account_id=%s",(account_id,))
        if actual and actual["desired"] != "running":
            c.execute("""UPDATE lead.actions SET state='canceled' WHERE state='prepared' AND target_id IN
                (SELECT id FROM lead.account_targets WHERE account_id=%s)""",(account_id,))
            c.execute("""UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE type LIKE 'growth.%%'
                AND input#>>'{spec,account_id}'=%s AND state IN ('planned','queued','running','waiting_user')
                AND NOT EXISTS(SELECT FROM lead.actions a WHERE a.task_id=lead.tasks.id AND a.state IN ('dispatching','unknown','confirmed'))""",(str(account_id),))
        emit(c, aid, "growth.wake", {"reason": "account_intent_changed", "account_id": str(account_id),
            "revision":actual["revision"],"desired":actual["desired"]})
    return revision
