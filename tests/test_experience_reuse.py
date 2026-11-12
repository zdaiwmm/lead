from uuid import uuid4

from lead_app import domain, experiences
from lead_app.db import tx
from lead_app.scope import tenant_scope


def test_applicability_and_counterevidence(growth_agent):
    aid = growth_agent
    item = experiences.record_experience(aid, 'fixture:research:1', kind='research', problem='community research',
        conclusion='Small creator communities provided useful sources',
        conditions={'audience': 'creators', 'language': 'en'}, evidence=[{'url': 'https://example.com/guide'}])
    assert experiences.record_experience(aid, 'fixture:research:1', kind='research', problem='community research',
        conclusion='changed') == item
    assert experiences.retrieve_experience(aid, {'audience':'enterprise'}, 'community') == []
    rows = experiences.retrieve_experience(aid, {'audience':'creators', 'language':'en'}, 'community')
    assert len(rows) == 1 and rows[0]['id'] == item
    experiences.withdraw(aid, item, 'New evidence contradicts the former conclusion')
    assert experiences.retrieve_experience(aid, {'audience':'creators', 'language':'en'}, 'community') == []


def test_owner_projection_and_revocation(growth_agent):
    aid = growth_agent
    other = domain.create_agent(runtime_engine='growth')
    try:
        item = experiences.record_experience(aid, 'fixture:public:1', kind='research', problem='community sources',
            conclusion='Private detailed conclusion', evidence=[{'url':'https://example.com/guide'}],
            public_projection={'problem':'community sources','conclusion':'Public useful source',
                'conditions':{}, 'sources':['https://example.com/guide']})
        rows = experiences.retrieve_experience(other, {}, 'community')
        assert len(rows) == 1 and rows[0]['conclusion'] == 'Public useful source'
        assert 'evidence' not in rows[0] and 'Private' not in str(rows)
        with tenant_scope(str(uuid4())):
            assert experiences.retrieve_experience(other, {}, 'community') == []
        experiences.withdraw(aid, item, 'sharing revoked')
        assert experiences.retrieve_experience(other, {}, 'community') == []
    finally:
        domain.set_control(other, 'archive')


def test_source_delete_invalidates_public_projection(growth_agent):
    aid = growth_agent
    with tx(aid) as c:
        doc = c.execute("INSERT INTO lead.knowledge_documents(tenant_id,agent_id,revision,title,source_type,source_ref,body_ref,sha256,trust_state) VALUES(lead.ctx_tenant(),%s,1,'source','web','https://example.com/guide','fixture','hash','untrusted') RETURNING id", (aid,)).fetchone()['id']
    experiences.record_experience(aid, 'fixture:doc', kind='operation', problem='community', conclusion='source',
        source_document_id=str(doc), public_projection={'problem':'community','conclusion':'source','conditions':{},'sources':['https://example.com/guide']})
    assert experiences.retrieve_experience(aid, {}, 'community')
    with tx(aid) as c:
        c.execute('UPDATE lead.knowledge_documents SET deleted_at=now() WHERE id=%s',(doc,))
    assert experiences.retrieve_experience(aid, {}, 'community') == []
