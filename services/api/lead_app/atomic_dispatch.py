"""Persist and execute account-free channel preparation one platform at a time."""

import asyncio

from psycopg.errors import LockNotAvailable
from temporalio import activity

from .config import env
from .db import all_rows, emit, j, one, tx
from .operating_playbooks import catalog, channel_for
from .operating_workspace import (
    channel_config,
    draft_pack,
    persist_workspace,
    preparation_key,
    save_workspace,
)
from .scope import current_tenant, scoped_activity


def enabled(aid):
    return str(aid) in {
        v.strip() for v in env().get("LEAD_PARALLEL_AGENT_IDS", "").split(",")
    }


def channel_pilot(aid):
    return str(aid) in {
        v.strip() for v in env().get("LEAD_CHANNEL_ATOM_AGENT_IDS", "").split(",")
    }


def read_batch_enabled(aid):
    return str(aid) in {
        value.strip()
        for value in env().get("LEAD_PARALLEL_READ_AGENT_IDS", "").split(",")
    }


def enqueue_channel_work(c, aid, agent, business):
    if channel_pilot(aid):
        return enqueue_connected(c, aid, agent, business)
    for channel in catalog()["channels"]:
        platform = channel["platform"]
        _, version = channel_config(c, aid, platform)
        revision = agent["config_revision"]
        source = str(business["id"])
        key = f"channel-prepare:{aid}:{source}:{revision}:{platform}:{version}"
        control = agent["control_version"]
        workflow_id = f"dispatch:{key}:{control}"
        intent = {
            "platform": platform,
            "business_id": source,
            "config_revision": revision,
            "control_version": control,
            "channel_version": version,
        }
        row = one(
            c,
            """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at)
            SELECT %s,%s,'dispatch.channel','queued',%s,%s,now()
            WHERE NOT EXISTS(SELECT 1 FROM lead.tasks WHERE workflow_id IN (%s,%s))
            ON CONFLICT(workflow_id) DO NOTHING RETURNING id""",
            (current_tenant(), aid, workflow_id, j(intent), key, workflow_id),
        )
        if row:
            emit(c, aid, "dispatch.channel", {"task_id": str(row["id"])})


def enqueue_connected(c, aid, agent, business):
    """Register only enabled account-bound work. Caller holds the project control lock."""
    accounts = all_rows(
        c,
        """SELECT DISTINCT platform FROM lead.social_accounts
        WHERE automation_enabled AND state='connected' ORDER BY platform""",
    )
    for account in accounts:
        platform = account["platform"]
        from .channel_delivery import enroll, owns
        if not channel_for(platform) or not owns(agent, platform):
            continue

        from .channel_authority import binding_snapshot, driver_accounts
        bound = binding_snapshot(driver_accounts(c, aid, platform))
        if not bound:
            continue
        enroll(c, aid, agent, platform)
        _, version = channel_config(c, aid, platform)
        intent = dict(
            platform=platform,
            business_id=str(business["id"]),
            config_revision=agent["config_revision"],
            control_version=agent["control_version"],
            channel_version=version,
            account_bindings=bound,
        )
        product_key = preparation_key(aid,business['id'],agent['config_revision'],platform,version,bound)
        job_key = f"connected:{product_key}:{agent['control_version']}"
        job = one(
            c,
            """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at)
            SELECT %s,%s,'dispatch.channel','queued',%s,%s,now()
            WHERE NOT EXISTS (SELECT 1 FROM lead.tasks WHERE workflow_id=%s)
              AND NOT EXISTS (SELECT 1 FROM lead.tasks WHERE workflow_id=%s
                              AND state NOT IN ('waiting_user','canceled'))
            ON CONFLICT(workflow_id) DO UPDATE SET state='queued',version=lead.tasks.version+1,
                scheduled_at=now(),completed_at=NULL,result=CASE
                    WHEN lead.tasks.state='waiting_user' THEN lead.tasks.result-'reason' ELSE '{}'::jsonb END
            WHERE lead.tasks.state IN ('waiting_user','canceled')
            RETURNING id,version""",
            (current_tenant(), aid, job_key, j(intent), product_key, job_key),
        )
        if job:
            emit(
                c,
                aid,
                "dispatch.channel",
                {"task_id": str(job["id"]), "generation": job["version"]},
            )


def refresh_connected(c, aid):
    agent = one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
    if not channel_pilot(aid) and not agent["onboarding_state"].get("reddit_member_delivery") and not any(agent["onboarding_state"].get("managed_channel_delivery", {}).values()):
        return
    agent = one(c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
    business = one(
        c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
    )
    if (
        agent["desired_state"] == "running"
        and business
        and business["description"].strip()
    ):
        enqueue_connected(c, aid, agent, business)


def connected_inputs(c, aid, task_id, generation, pack=None):
    # A busy control transaction must not consume an offline slot for its lifetime.
    # Temporal retries the read-only preparation after the transaction rolls back.
    c.execute("SET LOCAL lock_timeout='500ms'")
    agent = one(c, "SELECT * FROM lead.agents WHERE id=%s FOR SHARE", (aid,))
    task = one(
        c,
        """SELECT * FROM lead.tasks WHERE id=%s AND type='dispatch.channel'
        AND state='queued' AND version=%s FOR UPDATE""",
        (task_id, generation),
    )
    if not task:
        return
    intent = task["input"]
    platform = intent["platform"]
    channel, version = channel_config(c, aid, platform)
    business = one(
        c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
    )
    from .channel_authority import binding_snapshot, driver_accounts, has_driver
    from .channel_delivery import owns
    current_accounts = driver_accounts(c,aid,platform)
    allowed = (
        owns(agent,platform)
        and (not current_accounts or intent.get("account_bindings")==binding_snapshot(current_accounts) or ("account_bindings" not in intent and not has_driver(agent,platform)))
        and agent["desired_state"] == "running"
        and business
        and str(business["id"]) == intent["business_id"]
        and channel
        and version == intent["channel_version"]
        and agent["control_version"] == intent["control_version"]
        and agent["config_revision"] == intent["config_revision"]
    )
    if not allowed:
        state, reason = "canceled", "dispatch_scope_changed"
    elif not current_accounts:
        state, reason = "waiting_user", "channel_connection_required"
    else:
        bindings = binding_snapshot(current_accounts)
        if "account_bindings" not in intent:
            c.execute("UPDATE lead.tasks SET input=input || %s WHERE id=%s", (j({"account_bindings":bindings}),task_id))
        agent = {**agent, "_preparation_account_bindings":bindings}
        saved = (task["result"] or {}).get("pack")
        if not saved:
            # Reauthentication can change a binding while an internal draft was
            # being saved. Reuse its content only; capture current scope anew.
            cached = one(c, """SELECT result->'pack' AS pack FROM lead.tasks WHERE agent_id=%s AND type='dispatch.channel'
                AND id<>%s AND input->>'platform'=%s AND input->>'business_id'=%s
                AND input->>'config_revision'=%s AND input->>'channel_version'=%s AND result ? 'pack'
                ORDER BY created_at DESC,id DESC LIMIT 1""", (aid,task_id,platform,str(business["id"]),str(agent["config_revision"]),str(version)))
            if cached:
                saved = cached["pack"]
        return (agent, business, channel, version), saved
    checkpoint = {"reason": reason}
    if state == "waiting_user":
        saved = pack or (task["result"] or {}).get("pack")
        if saved is not None:
            checkpoint["pack"] = saved
    c.execute(
        """UPDATE lead.tasks SET state=%s,completed_at=CASE WHEN %s='canceled' THEN now() END,
        result=%s WHERE id=%s""",
        (state, state, j(checkpoint), task_id),
    )


def execute_connected(aid, task_id, generation):
    # Preparation is pure. Release the database connection and all locks while producing the pack.
    with tx(aid) as c:
        inputs = connected_inputs(c, aid, task_id, generation)
        if inputs:
            c.execute(
                "UPDATE lead.tasks SET started_at=clock_timestamp() WHERE id=%s",
                (task_id,),
            )
    if not inputs:
        return
    arguments, saved_pack = inputs
    pack = saved_pack or draft_pack(*arguments)
    pack = {**pack, "evidence_status":"internal_only", "business_profile_id":str(arguments[1]["id"]),
            "config_revision":arguments[0]["config_revision"], "account_binding":arguments[0]["_preparation_account_bindings"]}
    with tx(aid) as c:
        current = connected_inputs(c, aid, task_id, generation, pack)
        if not current:
            return
        product = persist_workspace(c, aid, *current[0], pack)
        c.execute(
            """UPDATE lead.tasks SET state='succeeded',completed_at=now(),
            result=%s WHERE id=%s""",
            (j({"product_task_id": product["id"]}), task_id),
        )


def execute_channel(aid, task_id):
    with tx(aid) as c:
        agent = one(
            c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,)
        )
        row = one(
            c,
            """SELECT * FROM lead.tasks WHERE id=%s AND type='dispatch.channel'
            AND state='queued' FOR UPDATE""",
            (task_id,),
        )
        if not row:
            return
        intent = row["input"]
        platform = intent["platform"]
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        active = (
            enabled(aid)
            and not channel_pilot(aid)
            and agent["desired_state"] == "running"
            and business
            and str(business["id"]) == intent["business_id"]
            and channel_for(platform)
            and agent["config_revision"] == intent["config_revision"]
            and agent["control_version"] == intent["control_version"]
            and channel_config(c, aid, platform)[1] == intent["channel_version"]
        )
        started_at = one(c, "SELECT clock_timestamp() AS at")["at"]
        if active:
            product = save_workspace(c, aid, agent, business, platform)
            state, result = "succeeded", {"product_task_id": product["id"]}
        else:
            state, result = "canceled", {"reason": "dispatch_scope_changed"}
        c.execute(
            """UPDATE lead.tasks SET state=%s,result=%s,started_at=%s,completed_at=clock_timestamp()
            WHERE id=%s""",
            (state, j(result), started_at, task_id),
        )


@activity.defn
@scoped_activity
async def prepare_channel_atom(args: dict):
    if "generation" in args:
        try:
            await asyncio.to_thread(
                execute_connected, args["agent_id"], args["task_id"], args["generation"]
            )
        except LockNotAvailable:
            return "control_busy"
        return None
    await asyncio.to_thread(execute_channel, args["agent_id"], args["task_id"])


@activity.defn
@scoped_activity
async def close_channel_atom(args: dict):
    with tx(args["agent_id"]) as c:
        c.execute(
            """UPDATE lead.tasks SET state='failed',completed_at=now(),result=%s
            WHERE id=%s AND type='dispatch.channel' AND state='queued' AND (%s::int IS NULL OR version=%s)""",
            (
                j({"reason": "channel_prepare_attempts_exhausted"}),
                args["task_id"],
                args.get("generation"),
                args.get("generation"),
            ),
        )
