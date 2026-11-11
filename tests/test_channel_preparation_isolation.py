"""Source-denied clocks can prepare internal copy without fabricating evidence."""

import pytest
from lead_app import atomic_dispatch, channel_research
from lead_app.db import all_rows, j, one, tx, uid
from test_channel_authority import authorize
from test_channel_authority import managed_pair as managed_pair


@pytest.fixture
def blocked(managed_pair, monkeypatch):
    aid, a, b = managed_pair
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    authorize(managed_pair)
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.tasks SET state='canceled' WHERE type='dispatch.channel'"
        )
        clock = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'",
        )
    return (
        aid,
        a,
        {
            "agent_id": aid,
            "task_id": str(clock["id"]),
            "generation": clock["version"],
            "tick": uid(),
        },
    )


def prepare(blocked):
    aid, account, args = blocked
    result, candidate = channel_research.select(args)
    assert candidate is None and result["reason"] == "source_permission_required"
    with tx(aid) as c:
        job = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='dispatch.channel' AND state='queued'",
        )
        assert job is not None
    atomic_dispatch.execute_connected(aid, job["id"], job["version"])
    with tx(aid) as c:
        return one(
            c,
            "SELECT * FROM lead.tasks WHERE type='channel.prepare' ORDER BY created_at DESC LIMIT 1",
        )


def test_preparation_continues_without_source_permission(blocked):
    aid, account, args = blocked
    product = prepare(blocked)
    assert product["result"]["evidence_status"] == "internal_only"
    assert product["result"]["account_binding"] == [
        {
            "account_id": str(account["id"]),
            "external_account_id": account["external_account_id"],
            "binding_version": account["binding_version"],
        }
    ]
    channel_research.select({**args, "tick": uid()})
    with tx(aid) as c:
        assert (
            len(all_rows(c, "SELECT id FROM lead.tasks WHERE type='channel.prepare'"))
            == 1
        )
        assert not one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type='dispatch.channel' AND state='queued'",
        )


def test_internal_pack_cannot_become_validated_opportunity(blocked):
    from lead_app.channel_delivery import opportunity

    aid, account, args = blocked
    product = prepare(blocked)
    with tx(aid) as c:
        from lead_app.scope import current_tenant

        target = one(
            c,
            "INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,canonical_url,display_name,verified_permissions,state) VALUES(%s,%s,%s,'page','fixture-page','https://www.facebook.com/fixture','Fixture','{}','active') RETURNING id",
            (current_tenant(), aid, account["id"]),
        )
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (
                j(
                    {
                        "status": "observed",
                        "channel_fit": "viable",
                        "source_urls": ["https://www.facebook.com/fixture"],
                        "cold_entry": {
                            "source_url": "https://www.facebook.com/fixture"
                        },
                    }
                ),
                product["id"],
            ),
        )
        assert opportunity(c, target["id"]) is None


@pytest.mark.asyncio
async def test_source_wait_never_calls_browser(blocked, monkeypatch):
    from lead_app import activities, operating_kernel

    def forbidden(*a, **k):
        raise AssertionError("source-denied work accessed external service")

    monkeypatch.setattr(activities, "browser_rpc", forbidden)
    monkeypatch.setattr(operating_kernel, "run_work", forbidden)
    result = await channel_research.channel_research_tick(blocked[2])
    assert result["execution_cadence"]["state"] == "waiting_event"
    assert result["execution_cadence"]["next_at"] is None


def test_business_change_invalidates_prepared_pack(blocked, monkeypatch):
    aid, account, args = blocked
    channel_research.select(args)
    with tx(aid) as c:
        job = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='dispatch.channel' AND state='queued'",
        )
        assert job
    original = atomic_dispatch.draft_pack

    def change(*a):
        pack = original(*a)
        with tx(aid) as c:
            c.execute(
                "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description) SELECT tenant_id,agent_id,2,'Changed business' FROM lead.business_profiles WHERE revision=1"
            )
        return pack

    monkeypatch.setattr(atomic_dispatch, "draft_pack", change)
    atomic_dispatch.execute_connected(aid, job["id"], job["version"])
    with tx(aid) as c:
        assert not one(c, "SELECT 1 FROM lead.tasks WHERE type='channel.prepare'")
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (job["id"],))["state"]
            == "canceled"
        )
