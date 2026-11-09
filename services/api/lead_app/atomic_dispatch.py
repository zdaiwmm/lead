"""Persist and execute account-free channel preparation one platform at a time."""

import asyncio

from temporalio import activity

from .config import env
from .db import emit, j, one, tx
from .operating_playbooks import catalog, channel_for
from .operating_workspace import channel_config, save_workspace
from .scope import current_tenant, scoped_activity


def enabled(aid):
    return str(aid) in {
        v.strip() for v in env().get("LEAD_PARALLEL_AGENT_IDS", "").split(",")
    }


def enqueue_channel_work(c, aid, agent, business):
    for channel in catalog()["channels"]:
        platform = channel["platform"]
        _, version = channel_config(c, aid, platform)
        revision = agent["config_revision"]
        source = str(business["id"])
        key = f"channel-prepare:{aid}:{source}:{revision}:{platform}:{version}"
        control = agent["control_version"]
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
            WHERE NOT EXISTS(SELECT 1 FROM lead.tasks WHERE workflow_id=%s)
            ON CONFLICT(workflow_id) DO NOTHING RETURNING id""",
            (current_tenant(), aid, f"dispatch:{key}:{control}", j(intent), key),
        )
        if row:
            emit(c, aid, "dispatch.channel", {"task_id": str(row["id"])})


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
            and agent["desired_state"] == "running"
            and business
            and str(business["id"]) == intent["business_id"]
            and channel_for(platform)
            and agent["config_revision"] == intent["config_revision"]
            and agent["control_version"] == intent["control_version"]
            and channel_config(c, aid, platform)[1] == intent["channel_version"]
        )
        if active:
            product = save_workspace(c, aid, agent, business, platform)
            state, result = "succeeded", {"product_task_id": product["id"]}
        else:
            state, result = "canceled", {"reason": "dispatch_scope_changed"}
        c.execute(
            """UPDATE lead.tasks SET state=%s,result=%s,started_at=now(),completed_at=now()
            WHERE id=%s""",
            (state, j(result), task_id),
        )


@activity.defn
@scoped_activity
async def prepare_channel_atom(args: dict):
    await asyncio.to_thread(execute_channel, args["agent_id"], args["task_id"])


@activity.defn
@scoped_activity
async def close_channel_atom(args: dict):
    with tx(args["agent_id"]) as c:
        c.execute(
            """UPDATE lead.tasks SET state='failed',completed_at=now(),result=%s
            WHERE id=%s AND type='dispatch.channel' AND state='queued'""",
            (j({"reason": "channel_prepare_attempts_exhausted"}), args["task_id"]),
        )
