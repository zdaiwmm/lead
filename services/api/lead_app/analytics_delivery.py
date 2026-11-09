"""Leased Outbox -> Temporal delivery, with scoped stale-work recovery."""

import asyncio

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .analytics_ingest import TASK_QUEUE, scoped_tx, work_args
from .db import all_rows, one, uid
from .db import pooled_tx as tx
from .scope import service_scope, tenant_scope


def reconcile(tenant, agent, scope):
    with scoped_tx(tenant, agent, scope) as c:
        # A published wake-up is not proof of a committed clean fact. Recover a
        # dead activity/workflow using a new dispatch generation, same event ID.
        c.execute("""UPDATE lead.outbox_events o SET published_at=NULL,
            available_at=now(),lease_token=NULL,lease_until=NULL,
            dispatch_generation=o.dispatch_generation+1
            FROM lead.analytics_ingest_events e
            WHERE o.id=e.outbox_id AND o.tenant_id=e.tenant_id AND o.agent_id=e.agent_id
              AND o.event_type='analytics.ingest' AND e.state='pending'
              AND e.next_attempt_at<=now() AND (e.lease_until IS NULL OR e.lease_until<now())
              AND o.published_at<now()-interval '2 minutes'""")


def claim_delivery(tenant, agent, scope):
    with scoped_tx(tenant, agent, scope) as c:
        row = one(
            c,
            """SELECT o.id AS delivery_id,o.dispatch_generation,e.*
            FROM lead.outbox_events o JOIN lead.analytics_ingest_events e
              ON o.id=e.outbox_id AND o.tenant_id=e.tenant_id AND o.agent_id=e.agent_id
            WHERE o.event_type='analytics.ingest' AND o.published_at IS NULL
              AND o.available_at<=now() AND (o.lease_until IS NULL OR o.lease_until<now())
              AND e.state='pending' AND e.next_attempt_at<=now()
            ORDER BY o.occurred_at,o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED""",
        )
        if not row:
            return None
        lease = uid()
        c.execute(
            "UPDATE lead.outbox_events SET lease_token=%s,lease_until=now()+interval '30 seconds',delivery_attempts=delivery_attempts+1 WHERE id=%s",
            (lease, row["delivery_id"]),
        )
        return {**row, "delivery_lease": lease}


def finish_delivery(tenant, agent, scope, row, success):
    with scoped_tx(tenant, agent, scope) as c:
        return c.execute(
            """UPDATE lead.outbox_events SET
            published_at=CASE WHEN %s THEN now() ELSE NULL END,
            available_at=CASE WHEN %s THEN available_at ELSE now()+make_interval(secs=>least(300,power(2,least(delivery_attempts,8))::integer)) END,
            delivery_error=CASE WHEN %s THEN NULL ELSE 'DELIVERY_UNAVAILABLE' END,
            lease_token=NULL,lease_until=NULL
            WHERE id=%s AND lease_token=%s AND dispatch_generation=%s""",
            (
                success,
                success,
                success,
                row["delivery_id"],
                row["delivery_lease"],
                row["dispatch_generation"],
            ),
        ).rowcount


async def deliver_scope(client, tenant, agent, scope, limit=20):
    reconcile(tenant, agent, scope)
    count = 0
    for _ in range(limit):
        row = claim_delivery(tenant, agent, scope)
        if not row:
            break
        try:
            args = work_args(row)
            await client.start_workflow(
                "AnalyticsIngestWorkflow",
                args,
                id=f"analytics:{tenant}:{agent}:{row['id']}:{row['dispatch_generation']}",
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass  # A previous request was accepted but its acknowledgement was lost.
        except Exception:
            finish_delivery(tenant, agent, scope, row, False)
            break
        finish_delivery(tenant, agent, scope, row, True)
        count += 1
    return count


_archived_offsets = {}


async def deliver_partition(client, tenant, aid, scope):
    from .analytics_reporting import refresh_scope
    from .atomic_dispatch import read_batch_enabled

    if not read_batch_enabled(aid):
        sent = await deliver_scope(client, tenant, aid, scope)
        return sent + len(await asyncio.to_thread(refresh_scope, tenant, aid, scope))
    # A dirty day is committed by ingestion. It can be refreshed while the
    # dispatcher waits for Temporal to accept *other* events in this scope.
    # Join both operations even on failure before starting another partition.
    results = await asyncio.gather(deliver_scope(client, tenant, aid, scope),
                                   asyncio.to_thread(refresh_scope, tenant, aid, scope),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results[0] + len(results[1])


async def deliver_analytics(client, tenant):

    changed = 0
    with tenant_scope(tenant, None), service_scope():
        with tx() as c:
            agents = all_rows(c, "SELECT id,desired_state FROM lead.agents ORDER BY id")
        active = [a for a in agents if a["desired_state"] != "archived"]
        archived = [a for a in agents if a["desired_state"] == "archived"]
        # Historical applications get bounded round-robin work; they cannot
        # delay every active application behind thousands of archived profiles.
        historical = []
        if archived:
            offset = _archived_offsets.get(tenant, 0) % len(archived)
            historical = [
                archived[(offset + i) % len(archived)]
                for i in range(min(10, len(archived)))
            ]
            _archived_offsets[tenant] = (offset + len(historical)) % len(archived)
        for agent in active + historical:
            aid = str(agent["id"])
            with tx(aid, tenant_id=tenant) as c:
                tests = all_rows(
                    c, "SELECT id FROM lead.pixel_tests ORDER BY created_at"
                )
            for scope in ["live"] + [str(test["id"]) for test in tests]:
                changed += await deliver_partition(client, tenant, aid, scope)
    return changed
