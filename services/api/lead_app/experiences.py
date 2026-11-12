"""Small optional evidence memory; authority and private source data never transfer."""
import logging
from urllib.parse import urlsplit

from .db import all_rows, j, one, require_agent, tx

log = logging.getLogger(__name__)
CONDITIONS = {'product', 'audience', 'region', 'language', 'value_behavior', 'location', 'operation'}


def _conditions(value):
    if not isinstance(value, dict) or set(value) - CONDITIONS:
        raise ValueError('Unsupported experience applicability')
    if any(not isinstance(v, str) or len(v) > 300 for v in value.values()):
        raise ValueError('Experience conditions must be bounded text')
    return value


def _projection(value):
    if value is None:
        return None
    if set(value) != {'problem', 'conclusion', 'conditions', 'sources'}:
        raise ValueError('Public experience requires an explicit minimal projection')
    _conditions(value['conditions'])
    for key in ('problem', 'conclusion'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 1500:
            raise ValueError('Invalid public experience')
    if not isinstance(value['sources'], list) or not 1 <= len(value['sources']) <= 8:
        raise ValueError('Public sources required')
    for source in value['sources']:
        u = urlsplit(source)
        # Authenticated/query-bearing URLs are never shared as public provenance.
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('Only public HTTPS source URLs can be shared')
    return value


def record_experience(aid, extraction_key, *, kind, problem, conclusion, conditions=None,
                      evidence=None, source_document_id=None, public_projection=None,
                      review_at=None, supersedes=None):
    """Deterministic extraction, once per result; caller must finish work first.

    A public projection must be built from public evidence, never by copying an
    arbitrary private result. This function does not invoke a model.
    """
    if kind not in {'research', 'business', 'operation'} or not extraction_key or len(extraction_key)>300:
        raise ValueError('Invalid experience identity')
    if not problem or not conclusion or len(problem)>1500 or len(conclusion)>6000:
        raise ValueError('Invalid experience content')
    conditions = _conditions(conditions or {})
    projection = _projection(public_projection)
    if projection and source_document_id is None:
        raise ValueError('Public projection requires a persisted public source')
    with tx(aid) as c:
        require_agent(c, aid, True)
        old = one(c, 'SELECT id FROM lead.experiences WHERE extraction_key=%s', (extraction_key,))
        if old:
            return str(old['id'])
        if source_document_id and not one(c, 'SELECT id FROM lead.knowledge_documents WHERE id=%s AND deleted_at IS NULL', (source_document_id,)):
            raise ValueError('Experience source is unavailable')
        if projection:
            document=one(c,"""SELECT d.*, (SELECT string_agg(k.body,' ' ORDER BY ordinal) FROM lead.knowledge_chunks k WHERE k.document_id=d.id) AS source_body
                FROM lead.knowledge_documents d WHERE d.id=%s AND d.deleted_at IS NULL""",(source_document_id,))
            body=' '.join((document or {}).get('source_body','').split())
            quote=' '.join(projection['conclusion'].split())
            if (not document or document['source_type']!='public_read'
                    or projection['sources']!=[document['source_ref']]
                    or projection['problem']!=document['title'] or not quote or quote not in body
                    or len(quote)>500
                    or any(v.casefold() not in body.casefold() for v in projection['conditions'].values())):
                raise ValueError('Public projection must be a bounded extract of its actual public source')
        if supersedes:
            if not one(c, 'UPDATE lead.experiences SET revoked_at=now(),version=version+1 WHERE id=%s RETURNING id', (supersedes,)):
                raise ValueError('Experience replacement outside scope')
        row = one(c, '''INSERT INTO lead.experiences(tenant_id,agent_id,extraction_key,kind,problem,conclusion,conditions,evidence,source_document_id,public_projection,review_at,supersedes)
          VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
          (aid, extraction_key, kind, problem, conclusion, j(conditions), j(evidence or []), source_document_id, j(projection) if projection else None, review_at, supersedes))
        return str(row['id'])


def withdraw(aid, experience_id, counterevidence):
    with tx(aid) as c:
        require_agent(c, aid, True)
        c.execute('UPDATE lead.experiences SET revoked_at=now(),counterevidence=%s,version=version+1 WHERE id=%s AND revoked_at IS NULL',
                  (str(counterevidence)[:2000], experience_id))


def retrieve_experience(aid, product, question, limit=4):
    """Optional bounded retrieval; missing/failed memory never blocks current work."""
    try:
        query = str(question).strip()[:256]
        if not query:
            return []
        product = _conditions({k:v for k,v in product.items() if k in CONDITIONS})
        with tx(aid) as c:
            require_agent(c, aid)
            rows = all_rows(c, '''SELECT e.id,e.kind,e.problem,e.conclusion,e.conditions,e.evidence,e.checked_at,e.version,
                ts_rank_cd(e.search_vector,websearch_to_tsquery('simple',%s)) rank
              FROM lead.experiences e WHERE e.revoked_at IS NULL AND (e.review_at IS NULL OR e.review_at>now())
              AND e.conditions <@ %s AND e.search_vector @@ websearch_to_tsquery('simple',%s)
              AND (e.source_document_id IS NULL OR EXISTS(SELECT 1 FROM lead.knowledge_documents d WHERE d.id=e.source_document_id AND d.deleted_at IS NULL))
              ORDER BY rank DESC,e.checked_at DESC LIMIT %s''', (query, j(product), query, max(1,min(int(limit),4))))
            public = all_rows(c, '''SELECT id,projection,checked_at FROM lead.public_experiences(%s)
              WHERE projection->'conditions' <@ %s
              AND to_tsvector('simple',(projection->>'problem')||' '||(projection->>'conclusion')) @@ websearch_to_tsquery('simple',%s)
              ORDER BY checked_at DESC LIMIT %s''', (aid,j(product),query,max(1,min(int(limit),4))))
            result = [{**r,'id':str(r['id']),'shared':False} for r in rows]
            result.extend({'id':str(r['id']),**r['projection'],'checked_at':r['checked_at'],'shared':True} for r in public)
            return result[:max(1,min(int(limit),4))]
    except Exception:
        log.info('Optional experience retrieval unavailable', exc_info=False)
        return []


def extract_task(aid, task_id):
    """At most one optional deterministic extraction after durable task success.

    No new model request and no invented finding. A failed extraction is marked
    once; completion of the original work is never rolled back or retried.
    """
    try:
        with tx(aid) as c:
            row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE', (task_id,))
            if not row or row['state'] != 'succeeded' or row['type'] not in ('growth.research','growth.observe'):
                return None
            result = row['result'] or {}
            if 'experience_extraction' in result:
                return result['experience_extraction'].get('experience_id')
            c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s",(j({'experience_extraction':{'state':'attempted'}}),task_id))
            output=result.get('output',{}).get('output',{})
            spec=row['input'].get('spec',{})
            source=None
            evidence=[]
            conclusion=None
            if row['type']=='growth.research':
                for finding in output.get('findings',[])[:4]:
                    doc=one(c, '''SELECT d.id,d.source_ref,string_agg(k.body,' ' ORDER BY k.ordinal) body
                        FROM lead.knowledge_documents d JOIN lead.knowledge_chunks k ON k.document_id=d.id
                        WHERE d.id=%s AND d.deleted_at IS NULL GROUP BY d.id''',(finding.get('document_id'),))
                    quote=finding.get('quote','')
                    if doc and quote and ' '.join(quote.split()) in ' '.join(doc['body'].split()) and finding.get('finding'):
                        source=str(doc['id'])
                        conclusion=finding['finding']
                        evidence=[{'kind':'task','id':str(task_id)},{'kind':'document','id':source,'quote':quote}]
                        break
            elif output.get('measurement')=='healthy' and output.get('window_status')=='closed' and (output.get('new_units') or 0)>0 and output.get('evidence_refs'):
                # Only observed positive units, never 'channel ineffective' from
                # missing reach/measurement or an immature observation window.
                conclusion=f"Observed {output['new_units']} new {output.get('unit','units')} under the frozen value definition; this does not establish scalable channel effectiveness."
                evidence=[{'kind':'task','id':str(task_id),'evidence_revision':output.get('evidence_revision')}]
            problem=spec.get('payload',{}).get('question') or spec.get('purpose') or 'Observed business evidence'
            conditions={}
            # Project-local conclusion is still tied to its original product.
            business=one(c,'SELECT product_url FROM lead.business_profiles ORDER BY revision DESC LIMIT 1')
            if business and business['product_url']:
                conditions['product']=business['product_url'][:300]
        identifier=None
        if conclusion:
            identifier=record_experience(aid,'task:'+str(task_id),kind='research' if row['type']=='growth.research' else 'business',
                problem=problem[:1500],conclusion=conclusion[:6000],conditions=conditions,evidence=evidence,source_document_id=source)
        with tx(aid) as c:
            c.execute('UPDATE lead.tasks SET result=result || %s WHERE id=%s',
                (j({'experience_extraction':{'state':'saved' if identifier else 'no_supported_learning','experience_id':identifier}}),task_id))
        return identifier
    except Exception:
        log.info('Optional experience extraction unavailable', exc_info=False)
        return None


def stop_sharing(aid):
    """Revoke only public projections; retain private evidence and conclusions."""
    with tx(aid) as c:
        require_agent(c, aid, True)
        c.execute("UPDATE lead.experiences SET public_projection=NULL,version=version+1 WHERE public_projection IS NOT NULL")
