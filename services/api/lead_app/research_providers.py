"""Optional HTTP research tools with scoped, durable billing reservations.

Trusted deployment configuration supplies verified endpoint eligibility and a
bounded allowance. No key or allowance is inferred from advertised free tiers.
Network calls have zero retries; an uncertain call is never automatically sent
again. Existing budget reservations/usage_events are the billing history.
"""
import hashlib
import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import env
from .db import j, one, require_agent, tx

ENDPOINTS = {
    ('tavily', 'web.search'): ('POST', 'https://api.tavily.com/search'),
    ('tavily', 'web.extract'): ('POST', 'https://api.tavily.com/extract'),
    ('tikhub', 'social.search'): ('GET', 'https://api.tikhub.io/api/v1/reddit/app/fetch_dynamic_search'),
    ('tikhub', 'social.item'): ('GET', 'https://api.tikhub.io/api/v1/reddit/app/fetch_post_details'),
    ('tikhub', 'social.comments'): ('GET', 'https://api.tikhub.io/api/v1/reddit/app/fetch_post_comments'),
}


def _now():
    return datetime.now(timezone.utc)


def _failure(provider, operation, reason, **extra):
    return {'state':'unavailable', 'reason':reason, 'provider':provider, 'operation':operation,
        'sources':[], 'access_mode':'provider_public', 'failure_scope':{'provider':provider,
        'operation':operation, 'endpoint':ENDPOINTS.get((provider,operation),('',None))[1],
        'access_mode':'provider_public'}, **extra}


def configuration(provider, operation):
    values = env()
    if (provider,operation) not in ENDPOINTS:
        return None, 'unsupported_operation'
    key = values.get(provider.upper()+'_API_KEY')
    if not key:
        return None, 'missing_key'
    try:
        conf = json.loads(values.get('LEAD_RESEARCH_ALLOWANCES') or '{}')[provider]
        pricing = conf['operations'][operation]
        start, end = (datetime.fromisoformat(conf[k]) for k in ('starts_at','ends_at'))
        if not start <= _now() < end or not conf['evidence_ref'] or not pricing['price_version']:
            return None, 'eligibility_unverified'
        if not pricing.get('trial_eligible') and not conf.get('paid_authorization_ref'):
            return None, 'payment_not_authorized'
        unit = 'provider_millicredits' if provider == 'tavily' else 'usd_micros'
        for k in ('limit_units','project_limit_units','question_limit_units'):
            if type(conf[k]) is not int or conf[k]<0:
                raise ValueError(k)
        if type(pricing['max_units']) is not int or pricing['max_units']<=0:
            raise ValueError('max_units')
        # Actual credential identity, not a model-controlled connection label.
        connection = provider+':'+hashlib.sha256(key.encode()).hexdigest()
        return {**conf, **pricing, 'key':key, 'unit':unit, 'connection':connection,
            'start':start, 'end':end, 'period_key':start.isoformat()}, None
    except (KeyError,ValueError,TypeError):
        return None, 'eligibility_unverified'


def _args(provider, operation, args):
    if provider == 'tavily':
        if operation == 'web.search':
            if set(args)-{'query','max_results'} or not isinstance(args.get('query'),str) or not 1<=len(args['query'])<=400:
                raise ValueError('invalid_search')
            count = args.get('max_results',5)
            if type(count) is not int or not 1<=count<=10:
                raise ValueError('invalid_result_limit')
            return {'query':args['query'],'max_results':count,'search_depth':'basic',
                'include_answer':False,'include_raw_content':True,'include_usage':True,'auto_parameters':False}
        if set(args) != {'urls'} or not isinstance(args['urls'],list) or not 1<=len(args['urls'])<=5:
            raise ValueError('invalid_extract')
        for url in args['urls']:
            from .fetch import resolve_public
            resolve_public(url)
        return {**args,'extract_depth':'basic','include_usage':True}
    if args.get('platform') != 'reddit':
        raise ValueError('unsupported_platform')
    allowed = {'platform','query','after'} if operation=='social.search' else {'platform','post_id','after'}
    if set(args)-allowed:
        raise ValueError('unsupported_parameter')
    result={'need_format':True}
    if operation=='social.search':
        if not isinstance(args.get('query'),str) or not 1<=len(args['query'])<=200:
            raise ValueError('invalid_query')
        result['query']=args['query']
    else:
        if not re.fullmatch(r't3_[a-z0-9]{1,17}',args.get('post_id','')):
            raise ValueError('invalid_post_id')
        result['post_id']=args['post_id']
    if args.get('after'):
        if operation=='social.item' or not isinstance(args['after'],str) or len(args['after'])>4096:
            raise ValueError('invalid_cursor')
        result['after']=args['after']
    return result


def _public_url(value):
    u=urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.port not in (None,80,443):
        raise ValueError('public_url_required')
    host=u.hostname.lower().rstrip('.')
    if host=='localhost' or host.endswith(('.local','.localhost','.internal')) or '.' not in host:
        raise ValueError('public_url_required')
    try:
        address=ipaddress.ip_address(host)
    except ValueError:
        address=None
    if address and not address.is_global:
        raise ValueError('public_url_required')
    return urlunsplit((u.scheme,u.netloc,u.path,u.query,''))


def normalize(provider, operation, data, request_id=None):
    now=_now().isoformat()
    if provider=='tavily':
        rows=data.get('results',[])
    else:
        content=data.get('data',{})
        if not isinstance(content,dict):
            return _failure(provider,operation,'unsupported_response')
        if 'children' in content:
            rows=content['children']
        elif isinstance(content.get('postInfoById'),dict):
            post=content['postInfoById']
            rows=post.get('commentForest',{}).get('trees',[]) if operation=='social.comments' else [post]
        else:
            return _failure(provider,operation,'unsupported_response')
    if not isinstance(rows,list):
        return _failure(provider,operation,'unsupported_response')
    sources=[]
    seen=set()
    for row in rows[:20]:
        value=row.get('post',row.get('comment',row.get('data',row))) if isinstance(row,dict) else {}
        url=value.get('permalink') or value.get('url')
        if provider=='tikhub' and re.fullmatch(r't3_[a-z0-9]{1,17}',str(value.get('id',''))):
            url='https://www.reddit.com/comments/'+value['id'][3:]+'/'
        if isinstance(url,str) and url.startswith('/') and provider=='tikhub':
            url='https://www.reddit.com'+url
        try:
            url=_public_url(url)
        except (ValueError,TypeError,AttributeError):
            continue
        # Same original URL has the same identity regardless of provider.
        identity=hashlib.sha256(url.encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        body=value.get('raw_content') or value.get('selftext') or value.get('body') or value.get('content')
        excerpt=value.get('title') or value.get('postTitle') or (value.get('content') if provider=='tavily' else '') or ''
        sources.append({'source_id':identity,'url':url,'content_id':value.get('id'),
            'published_at':value.get('published_date') or value.get('created_utc'), 'fetched_at':now,
            'body':str(body)[:12000] if body else None, 'excerpt':str(excerpt)[:4000],
            'provider':provider,'request_id':request_id,'independently_verified':False,
            'metrics':{key:value.get(key) for key in ('score','num_comments')},
            'metrics_basis':'provider_reported', 'access_mode':'provider_public'})
    failures=[{'url':r.get('url'),'reason':'extract_failed','details':str(r.get('error',''))[:300]}
        for r in data.get('failed_results',[])[:20] if isinstance(r,dict)]
    page=data.get('data',{}).get('pageInfo',{}) if isinstance(data.get('data'),dict) else {}
    return {'state':'partial' if failures else 'complete' if sources else 'empty', 'sources':sources,
        'read_errors':failures, 'provider':provider,'operation':operation,'request_id':request_id,
        'access_mode':'provider_public','sampled':True,'next_cursor':page.get('endCursor'),
        'has_more':page.get('hasNextPage'), 'usage':data.get('usage')}


def _reserve(aid, task_id, provider, operation, payload, call_key, conf):
    digest=hashlib.sha256(json.dumps([task_id,provider,operation,payload],sort_keys=True).encode()).hexdigest()
    opkey='research:'+call_key
    with tx(aid) as c:
        # Shared connection first, then project. All writers follow this order.
        c.execute('''INSERT INTO lead.research_allowances(tenant_id,connection_ref,period_key,unit,limit_units)
            VALUES(lead.ctx_tenant(),%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (conf['connection'],conf['period_key'],conf['unit'],conf['limit_units']))
        quota=one(c,'''SELECT * FROM lead.research_allowances WHERE connection_ref=%s AND period_key=%s FOR UPDATE''',
            (conf['connection'],conf['period_key']))
        agent=require_agent(c,aid,True)
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(task_id,))
        if not task or task['state'] in ('cancelled','failed') or agent['desired_state'] in ('paused','archived'):
            raise ValueError('task_unavailable')
        old=one(c,'SELECT * FROM lead.budget_reservations WHERE operation_key=%s',(opkey,))
        if old:
            snapshot=old['pricing_snapshot']['research']
            if snapshot['args_hash']!=digest:
                raise ValueError('call_key_conflict')
            return None,snapshot.get('result') or _failure(provider,operation,'billing_unknown',call_key=call_key)
        experiment=one(c,'SELECT question_key FROM lead.growth_experiments WHERE id=%s',(task['experiment_id'],)) if task.get('experiment_id') else None
        question=experiment['question_key'] if experiment else task['input'].get('purpose') or task['type']
        units=conf['max_units']
        if quota['unit']!=conf['unit'] or quota['spent_units']+quota['reserved_units']+units>min(quota['limit_units'],conf['limit_units']):
            raise ValueError('shared_quota_exhausted')
        unit=provider+':'+conf['unit']
        c.execute('''INSERT INTO lead.budget_periods(tenant_id,agent_id,starts_at,ends_at,unit,limit_units)
          VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(aid,conf['start'],conf['end'],unit,conf['project_limit_units']))
        period=one(c,'SELECT * FROM lead.budget_periods WHERE starts_at=%s AND unit=%s FOR UPDATE',(conf['start'],unit))
        if period['spent_units']+period['reserved_units']+units>min(period['limit_units'],conf['project_limit_units']):
            raise ValueError('project_quota_exhausted')
        spent=one(c,'''SELECT coalesce(sum(r.units),0) n FROM lead.budget_reservations r
          WHERE r.period_id=%s AND r.state<>'released' AND r.pricing_snapshot#>>'{research,question}'=%s''',(period['id'],question))['n']
        if spent+units>conf['question_limit_units']:
            raise ValueError('question_quota_exhausted')
        snapshot={'research':{'args_hash':digest,'question':question,'task_id':str(task_id),
            'connection':conf['connection'],'period_key':conf['period_key'],'unit':conf['unit'],
            'price_version':conf['price_version'],'provider':provider,'operation':operation}}
        row=one(c,'''INSERT INTO lead.budget_reservations(tenant_id,agent_id,period_id,operation_key,units,state,expires_at,provider,pricing_snapshot)
          VALUES(lead.ctx_tenant(),%s,%s,%s,%s,'reserved',%s,%s,%s) RETURNING id''',
          (aid,period['id'],opkey,units,conf['end'],provider,j(snapshot)))
        c.execute('UPDATE lead.budget_periods SET reserved_units=reserved_units+%s WHERE id=%s',(units,period['id']))
        c.execute('UPDATE lead.research_allowances SET reserved_units=reserved_units+%s WHERE connection_ref=%s AND period_key=%s',
            (units,conf['connection'],conf['period_key']))
        return str(row['id']),None


def _settle(aid, reservation, conf, result, actual=None):
    with tx(aid) as c:
        one(c,'SELECT * FROM lead.research_allowances WHERE connection_ref=%s AND period_key=%s FOR UPDATE',(conf['connection'],conf['period_key']))
        one(c,'SELECT id FROM lead.agents WHERE id=%s FOR NO KEY UPDATE',(aid,))
        row=one(c,'SELECT * FROM lead.budget_reservations WHERE id=%s FOR UPDATE',(reservation,))
        if row['state']!='reserved':
            return
        snapshot=row['pricing_snapshot']
        snapshot['research']['result']=result
        c.execute('UPDATE lead.budget_reservations SET state=%s,pricing_snapshot=%s WHERE id=%s',
            ('unknown' if actual is None else 'settled',j(snapshot),reservation))
        if actual is None:
            return
        c.execute('UPDATE lead.budget_periods SET reserved_units=reserved_units-%s,spent_units=spent_units+%s WHERE id=%s',
            (row['units'],actual,row['period_id']))
        c.execute('UPDATE lead.research_allowances SET reserved_units=reserved_units-%s,spent_units=spent_units+%s WHERE connection_ref=%s AND period_key=%s',
            (row['units'],actual,conf['connection'],conf['period_key']))
        c.execute('''INSERT INTO lead.usage_events(tenant_id,agent_id,reservation_id,event_key,category,units,rate_version,occurred_at,details)
            VALUES(lead.ctx_tenant(),%s,%s,%s,'research_provider',%s,%s,now(),%s) ON CONFLICT DO NOTHING''',
            (aid,reservation,reservation,actual,conf['price_version'],j({'unit':conf['unit'],'provider':snapshot['research']['provider'],
             'request_id':result.get('request_id'),'billing':'known','money_usd':actual/1_000_000 if conf['unit']=='usd_micros' else None})))


async def research_call(aid, task_id, provider, operation, args, call_key):
    conf, reason=configuration(provider,operation)
    if reason:
        return _failure(provider,operation,reason)
    try:
        if not isinstance(call_key,str) or not 1<=len(call_key)<=200:
            raise ValueError('invalid_call_key')
        payload=_args(provider,operation,args)
        reservation,cached=_reserve(aid,task_id,provider,operation,payload,call_key,conf)
    except ValueError as error:
        return _failure(provider,operation,str(error))
    if cached is not None:
        return {**cached,'replayed':True}
    method,url=ENDPOINTS[(provider,operation)]
    actual=None
    try:
        async with httpx.AsyncClient(timeout=45,follow_redirects=False,trust_env=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            response=await client.request(method,url,headers={'Authorization':'Bearer '+conf['key']},
                **({'json':payload} if method=='POST' else {'params':payload}))
        if response.status_code!=200:
            reason={401:'credentials_invalid',403:'permission_denied',404:'missing',410:'deleted',429:'rate_limited'}.get(response.status_code,'provider_error')
            result=_failure(provider,operation,reason,http_status=response.status_code)
        else:
            data=response.json()
            if provider=='tikhub' and data.get('code',200)!=200:
                result=_failure(provider,operation,{403:'permission_denied',404:'missing',410:'deleted'}.get(data.get('code'),'provider_error'))
            else:
                result=normalize(provider,operation,data,data.get('request_id') or response.headers.get('x-request-id'))
            usage=(data.get('usage') or {}).get('credits') if provider=='tavily' else None
            if type(usage) in (int,float) and math.isfinite(usage) and 0<=usage*1000<=conf['max_units']:
                actual=math.ceil(usage*1000)
            # TikHub successful responses do not provide a billing receipt in
            # the documented generic envelope. Preserve unknown until reconciled.
        result['billing_state']='known' if actual is not None else 'unknown'
    except (httpx.HTTPError,ValueError,TypeError,AttributeError):
        result=_failure(provider,operation,'transport_unknown',billing_state='unknown')
    _settle(aid,reservation,conf,result,actual)
    return result
