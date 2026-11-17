"""A release hold retains a real startup fact without resuming operations."""
import pytest
from lead_app import domain, home_release_probe, worker
from lead_app.db import one, require_agent, tx
from lead_app.growth.contracts import WorkResult
from lead_app.growth.runtime import (
    advance,
    consume_triggers,
    ensure_decision,
    finish,
    recover_workspace,
)
from lead_app.scope import service_scope

from scripts import home_live_maintenance as site

SHA = 'a' * 40


@pytest.fixture
def held_project(growth_agent, monkeypatch, tmp_path):
    revision = tmp_path / 'release-revision'
    revision.write_text(SHA)
    monkeypatch.setattr(home_release_probe, 'REVISION', revision)
    domain.connect_lab(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (growth_agent,))
        c.execute("""INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,payload,expires_at)
            VALUES(lead.ctx_tenant(),%s,'manage_community','{}',now()+interval '1 hour')""", (growth_agent,))

    def site_sql(query):
        with tx(growth_agent) as c:
            c.execute(query.removeprefix('BEGIN;').removesuffix('COMMIT;'))

    monkeypatch.setattr(site, 'site_sql', site_sql)
    site.maintenance('hold', SHA)
    return growth_agent


def startup_facts(aid):
    with tx(aid) as c:
        return [row['payload'] for row in c.execute("""SELECT payload FROM lead.outbox_events
            WHERE event_type='growth.wake' AND payload->>'reason'='configuration_changed'
            ORDER BY occurred_at,id""").fetchall()]


def startup():
    with service_scope():
        worker.model_became_available()


def test_release_startup_is_pending_until_exact_hold_restores(held_project):
    aid = held_project
    with tx(aid) as c:
        before = require_agent(c, aid)
    startup()
    startup()
    facts = startup_facts(aid)
    assert len(facts) == 2 and facts[0] == facts[1]
    for fact in facts:
        assert ensure_decision(aid, [fact]) is None
    recover_workspace()
    with tx(aid) as c:
        paused = require_agent(c, aid)
        assert paused['desired_state'] == 'paused'
        assert paused['onboarding_state']['home_release_maintenance'] == before['onboarding_state']['home_release_maintenance']
        assert len(paused['onboarding_state']['growth_runtime']['pending_triggers']) == 1
        assert all(paused[key] == before[key] for key in ('control_version', 'config_revision', 'authorization_version'))
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")['n'] == 0

    site.maintenance('restore', SHA)
    recover_workspace()
    ref = ensure_decision(aid)
    assert ref is not None
    assert ensure_decision(aid, facts) == ref
    claim = advance(ref)['claim']
    assert finish(claim, WorkResult(outcome='completed'))['accepted_control']
    assert consume_triggers(aid, facts) is None
    startup()
    assert ensure_decision(aid, [startup_facts(aid)[-1]]) is None
    with tx(aid) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")['n'] == 1


@pytest.mark.parametrize('changed', ['owner_pause', 'old_sha', 'invalid_sha', 'missing_revision', 'invalid_revision',
                                   'control', 'config', 'authorization', 'targets', 'accounts', 'grants', 'delegations'])
def test_startup_does_not_wake_owner_pause_or_changed_hold(held_project, changed):
    aid = held_project
    with tx(aid) as c:
        if changed == 'owner_pause':
            c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state-'home_release_maintenance' WHERE id=%s", (aid,))
        elif changed in ('old_sha', 'invalid_sha'):
            c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,
                '{home_release_maintenance,sha}',to_jsonb(%s::text)) WHERE id=%s""",
                ('b'*40 if changed == 'old_sha' else 'invalid', aid))
        elif changed == 'missing_revision':
            home_release_probe.REVISION.unlink()
        elif changed == 'invalid_revision':
            home_release_probe.REVISION.write_text('invalid')
        elif changed == 'control':
            c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (aid,))
        elif changed == 'config':
            domain.snapshot_preferences(c, aid)
        elif changed == 'authorization':
            c.execute('UPDATE lead.agents SET authorization_version=authorization_version+1 WHERE id=%s', (aid,))
        elif changed == 'targets':
            c.execute("UPDATE lead.account_targets SET state='disabled'")
        elif changed == 'accounts':
            c.execute('UPDATE lead.social_accounts SET automation_enabled=NOT automation_enabled')
        elif changed == 'grants':
            c.execute('UPDATE lead.authorization_grants SET revoked_at=now()')
        else:
            c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE type='manage_community'")
    startup()
    assert startup_facts(aid) == []
    with tx(aid) as c:
        assert require_agent(c, aid)['desired_state'] == 'paused'
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")['n'] == 0


def test_running_startup_does_not_require_a_home_release_mount(growth_agent, monkeypatch, tmp_path):
    monkeypatch.setattr(home_release_probe, 'REVISION', tmp_path / 'absent')
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (growth_agent,))
    startup()
    assert len(startup_facts(growth_agent)) == 1
