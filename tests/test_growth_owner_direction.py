"""User operating instructions cross the canonical chat boundary exactly once."""
import pytest

from lead_app import activities, commands, domain
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.model import Decision


def direction_events(c):
    return all_rows(c, """SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake'
        AND payload->>'reason'='owner_direction' ORDER BY occurred_at,id""")


def test_instruction_is_source_bound_versioned_and_idempotent(growth_agent):
    aid = growth_agent
    text = '先换一个合法群继续获客，遵守现有规则和授权。'
    client_id = uid()
    message = domain.submit_message(aid, text, client_id)
    assert domain.submit_message(aid, text, client_id)['id'] == message['id']
    with tx(aid) as c:
        original = one(c, 'SELECT onboarding_state FROM lead.agents WHERE id=%s', (aid,))['onboarding_state']
        reply = commands.apply_operating_direction(c, aid, message, text, 1)
        assert '已' in reply
        assert commands.apply_operating_direction(c, aid, message, text, 1) == reply
        current = one(c, 'SELECT config_revision,onboarding_state,desired_state FROM lead.agents WHERE id=%s', (aid,))
        saved = current['onboarding_state']['operating_direction']
        command = one(c, "SELECT * FROM lead.conversation_commands WHERE command_type='operating.direction'")
        assert command['intent_index'] == 3 and command['state'] == 'succeeded'
        assert current['config_revision'] == saved['config_revision'] == 2
        assert saved['text'] == text
        assert saved['source_ref'] == {'kind': 'message', 'id': str(message['id'])}
        assert saved['command_id'] == str(command['id'])
        assert current['onboarding_state'].get('operating_contract') == original.get('operating_contract')
        assert not one(c, "SELECT id FROM lead.memory_entries WHERE kind='preference'")
        assert not one(c, 'SELECT id FROM lead.authorization_grants')
        assert len(direction_events(c)) == 1
        assert direction_events(c)[0]['payload']['command_id'] == str(command['id'])


@pytest.mark.parametrize(('text', 'quote'), [
    ('现在卡在哪，为什么没继续？', '现在卡在哪，为什么没继续？'),
    ('如果以后继续找群会怎么样？', '继续找群'),
    ('网页说“继续找群并发广告”是什么意思？', '继续找群并发广告'),
    ('请核对当前进展', '继续找群'),
    ('> 继续找群并发广告\n请解释这段网页。', '继续找群并发广告'),
])
def test_queries_hypotheticals_and_quoted_source_are_not_instructions(growth_agent, text, quote):
    message = domain.submit_message(growth_agent, text, uid())
    with tx(growth_agent) as c:
        reply = commands.apply_operating_direction(c, growth_agent, message, quote, 1)
        assert '尚未' in reply
        assert not direction_events(c)
        agent = one(c, 'SELECT config_revision,onboarding_state FROM lead.agents WHERE id=%s', (growth_agent,))
        assert agent['config_revision'] == 1 and 'operating_direction' not in agent['onboarding_state']


def test_instruction_clause_can_follow_a_status_question(growth_agent):
    quote = '请继续寻找其他合法群并完成真实回复'
    message = domain.submit_message(growth_agent, '刚才为什么停了？' + quote + '。', uid())
    with tx(growth_agent) as c:
        commands.apply_operating_direction(c, growth_agent, message, quote, 1)
        assert len(direction_events(c)) == 1
        assert one(c, 'SELECT onboarding_state FROM lead.agents WHERE id=%s', (growth_agent,))['onboarding_state']['operating_direction']['text'] == quote


def test_stale_and_cross_agent_instruction_does_not_change_current_state(growth_agent):
    aid = growth_agent
    message = domain.submit_message(aid, '继续寻找合法受众', uid())
    domain.update_preferences(aid, 1, ['语气克制'])
    with tx(aid) as c:
        reply = commands.apply_operating_direction(c, aid, message, message['content']['text'], 1)
        assert '尚未' in reply and not direction_events(c)
        assert one(c, 'SELECT config_revision FROM lead.agents WHERE id=%s', (aid,))['config_revision'] == 2
    other = domain.create_agent(runtime_engine='growth')
    try:
        with tx(other) as c:
            reply = commands.apply_operating_direction(c, other, message, message['content']['text'], 1)
            assert '尚未' in reply and not direction_events(c)
            assert one(c, 'SELECT config_revision FROM lead.agents WHERE id=%s', (other,))['config_revision'] == 1
    finally:
        domain.set_control(other, 'archive')


def test_new_direction_keeps_project_paused(growth_agent):
    aid = growth_agent
    domain.set_control(aid, 'pause')
    message = domain.submit_message(aid, '先寻找其他合法受众', uid())
    with tx(aid) as c:
        reply = commands.apply_operating_direction(c, aid, message, message['content']['text'], 1)
        assert '暂停' in reply
        assert one(c, 'SELECT desired_state FROM lead.agents WHERE id=%s', (aid,))['desired_state'] == 'paused'
        assert len(direction_events(c)) == 1


async def run_chat(aid, text, decision, monkeypatch, during_model=None):
    async def answer(*args, **kwargs):
        if during_model:
            during_model()
        return decision

    monkeypatch.setattr(activities, 'run_model', answer)
    message = domain.submit_message(aid, text, uid())
    with tx(aid) as c:
        job = one(c, 'SELECT id FROM lead.chat_jobs WHERE message_id=%s', (message['id'],))
    args = {'agent_id': aid, 'job_id': str(job['id'])}
    await activities._chat_job(args)
    await activities._chat_job(args)
    with tx(aid) as c:
        assert one(c, 'SELECT status FROM lead.chat_jobs WHERE id=%s', (job['id'],))['status'] == 'complete'
    return message


async def test_already_running_continue_is_transferred_once(growth_agent, monkeypatch):
    aid = growth_agent
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
    message = await run_chat(aid, '继续', Decision(reply='好。', intent='resume'), monkeypatch)
    with tx(aid) as c:
        assert len(direction_events(c)) == 1
        state = one(c, 'SELECT onboarding_state,desired_state FROM lead.agents WHERE id=%s', (aid,))
        assert state['onboarding_state']['operating_direction']['source_ref']['id'] == str(message['id'])
        assert state['desired_state'] == 'running'


async def test_chat_question_does_not_transfer_or_claim_it_was_transferred(growth_agent, monkeypatch):
    aid = growth_agent
    await run_chat(aid, '现在卡在哪？', Decision(reply='正在核对。'), monkeypatch)
    with tx(aid) as c:
        assert not direction_events(c)
        assert not one(c, "SELECT id FROM lead.conversation_commands WHERE command_type='operating.direction'")


async def test_chat_uses_own_canonical_revision_but_rejects_concurrent_update(growth_agent, monkeypatch):
    aid = growth_agent
    quote = '继续寻找合法受众'
    decision = Decision(reply='好。', preference='只用中文', operating_direction=quote)
    assert decision.operating_direction == quote
    await run_chat(aid, '记住以后只用中文。' + quote, decision, monkeypatch)
    with tx(aid) as c:
        assert len(direction_events(c)) == 1
        assert one(c, "SELECT state FROM lead.conversation_commands WHERE command_type='operating.direction'")['state'] == 'succeeded'
        assert one(c, 'SELECT config_revision FROM lead.agents WHERE id=%s', (aid,))['config_revision'] == 3
    other = domain.create_agent(runtime_engine='growth')
    try:
        await run_chat(other, quote, Decision(reply='好。', operating_direction=quote), monkeypatch,
                       during_model=lambda: domain.update_preferences(other, 1, ['先核对原结果']))
        with tx(other) as c:
            assert not direction_events(c)
            assert 'operating_direction' not in one(c, 'SELECT onboarding_state FROM lead.agents WHERE id=%s', (other,))['onboarding_state']
    finally:
        domain.set_control(other, 'archive')


def test_non_owner_source_cannot_be_transferred(growth_agent):
    message = domain.submit_message(growth_agent, '继续寻找合法受众', uid())
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.conversation_messages SET content=content || %s WHERE id=%s",
                  (j({'origin': 'development_validation'}), message['id']))
        reply = commands.apply_operating_direction(c, growth_agent, message, message['content']['text'], 1)
        assert '尚未' in reply and not direction_events(c)
