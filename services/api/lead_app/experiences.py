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
    if projection and source_document_id is None and not evidence:
        raise ValueError('Public projection requires source evidence')
    with tx(aid) as c:
        require_agent(c, aid, True)
        old = one(c, 'SELECT id FROM lead.experiences WHERE extraction_key=%s', (extraction_key,))
        if old:
            return str(old['id'])
        if source_document_id and not one(c, 'SELECT id FROM lead.knowledge_documents WHERE id=%s AND deleted_at IS NULL', (source_document_id,)):
            raise ValueError('Experience source is unavailable')
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
