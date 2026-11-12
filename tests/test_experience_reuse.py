from uuid import uuid4

from lead_app import domain, experiences
from lead_app.db import tx
from lead_app.scope import tenant_scope


def public_source(aid, title='community sources', body='Public useful source'):
    with tx(aid) as c:
        doc=c.execute("INSERT INTO lead.knowledge_documents(tenant_id,agent_id,revision,title,source_type,source_ref,body_ref,sha256,trust_state) VALUES(lead.ctx_tenant(),%s,1,%s,'public_read','https://example.com/guide','fixture','hash','untrusted_source') RETURNING id",(aid,title)).fetchone()['id']
        c.execute("INSERT INTO lead.knowledge_chunks(tenant_id,agent_id,document_id,ordinal,body,search_vector) VALUES(lead.ctx_tenant(),%s,%s,0,%s,to_tsvector('simple',%s))",(aid,doc,body,body))
        return str(doc)

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
            conclusion='Private detailed conclusion', source_document_id=public_source(aid), evidence=[{'url':'https://example.com/guide'}],
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
    doc=public_source(aid,'community','source')
    experiences.record_experience(aid, 'fixture:doc', kind='operation', problem='community', conclusion='source',
        source_document_id=str(doc), public_projection={'problem':'community','conclusion':'source','conditions':{},'sources':['https://example.com/guide']})
    assert experiences.retrieve_experience(aid, {}, 'community')
    with tx(aid) as c:
        c.execute('UPDATE lead.knowledge_documents SET deleted_at=now() WHERE id=%s',(doc,))
    assert experiences.retrieve_experience(aid, {}, 'community') == []


def test_extract_task_is_optional_once_and_requires_actual_quote(growth_agent):
    from datetime import timedelta

    from lead_app.db import j
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.store import submit_work
    aid=growth_agent
    with tx(aid) as c:
        doc=c.execute("INSERT INTO lead.knowledge_documents(tenant_id,agent_id,revision,title,source_type,source_ref,body_ref,sha256,trust_state) VALUES(lead.ctx_tenant(),%s,1,'source','web','https://example.com/research','fixture','hash','untrusted') RETURNING id",(aid,)).fetchone()['id']
        c.execute("INSERT INTO lead.knowledge_chunks(tenant_id,agent_id,document_id,ordinal,body,search_vector) VALUES(lead.ctx_tenant(),%s,%s,0,'Creators share concrete workflow questions',to_tsvector('simple','Creators share concrete workflow questions'))",(aid,doc))
        tid=submit_work(c,aid,TaskSpec(kind='research',purpose='community learning',review_at=utcnow()+timedelta(hours=1)))['task_id']
        c.execute("UPDATE lead.tasks SET state='succeeded',result=%s WHERE id=%s",(j({'output':{'output':{'findings':[{'document_id':str(doc),'quote':'Creators share concrete workflow questions','finding':'Concrete workflow questions provide a useful research starting point'}]}}}),tid))
    identifier=experiences.extract_task(aid,tid)
    assert identifier and experiences.extract_task(aid,tid)==identifier
    with tx(aid) as c:
        assert c.execute('SELECT count(*) n FROM lead.experiences WHERE extraction_key=%s',('task:'+str(tid),)).fetchone()['n']==1
        assert c.execute('SELECT public_projection FROM lead.experiences WHERE id=%s',(identifier,)).fetchone()['public_projection'] is None


def test_restricted_member_cannot_read_other_project_projection(growth_agent):
    from lead_app.scope import current_tenant
    aid=growth_agent
    other=domain.create_agent(runtime_engine='growth')
    try:
        experiences.record_experience(aid,'public:restricted',kind='research',problem='community sources',conclusion='private',
            source_document_id=public_source(aid,'community','public'),evidence=[{'url':'https://example.com/guide'}],public_projection={'problem':'community','conclusion':'public','conditions':{},'sources':['https://example.com/guide']})
        assert experiences.retrieve_experience(other,{},'community')
        with tenant_scope(current_tenant(),{'issuer':'fixture','subject':'limited','agent_ids':[other]}):
            assert experiences.retrieve_experience(other,{},'community')==[]
            with tx(other) as c:
                assert c.execute('SELECT * FROM lead.experiences WHERE agent_id=%s',(aid,)).fetchall()==[]
        experiences.stop_sharing(aid)
        assert experiences.retrieve_experience(other,{},'community')==[]
        assert experiences.retrieve_experience(aid,{},'community')
    finally:
        domain.set_control(other,'archive')


def test_private_sources_and_arbitrary_projection_cannot_be_shared(growth_agent):
    import pytest
    aid=growth_agent
    doc=public_source(aid,'community','Public source text')
    args={'kind':'research','problem':'community','conclusion':'private analysis','source_document_id':doc,
        'public_projection':{'problem':'community','conclusion':'Secret customer profile','conditions':{},'sources':['https://example.com/guide']}}
    with pytest.raises(ValueError,match='bounded extract'):
        experiences.record_experience(aid,'bad-projection',**args)
    args['public_projection']['conclusion']='Public source text'
    with tx(aid) as c:
        c.execute("UPDATE lead.knowledge_documents SET source_type='owner_private' WHERE id=%s",(doc,))
    with pytest.raises(ValueError,match='bounded extract'):
        experiences.record_experience(aid,'private-document',**args)
    args['source_document_id']=None
    with pytest.raises(ValueError,match='persisted public source'):
        experiences.record_experience(aid,'fake-source-url',**args)


def test_database_rejects_unproven_public_projection(growth_agent):
    import psycopg
    import pytest
    from lead_app.db import j
    with pytest.raises(psycopg.errors.RaiseException,match='public_experience_source_required'):
        with tx(growth_agent) as c:
            c.execute("""INSERT INTO lead.experiences(tenant_id,agent_id,extraction_key,kind,problem,conclusion,public_projection)
              VALUES(lead.ctx_tenant(),%s,'sql:unproven','research','community','private',%s)""",
              (growth_agent,j({'problem':'community','conclusion':'private','conditions':{},'sources':['https://example.com/guide']})))
