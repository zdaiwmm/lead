"""Authenticated Lead MCP handlers; original business services own all authority."""
import asyncio
import json
import re
import socket

import httpx

from .local_executor_browser import request_context
from .public_reader import PublicReaderPolicyError


async def call(device, request, name, arguments, *, revalidate=None):
    payload=request.get('payload') or {}
    context=payload.get('tool_context') or {}
    if context.get('kind') == 'model':
        if (str(device.get('id',device.get('device_id'))) != str(request.get('device_id'))
                or str(device.get('agent_id')) != str(request.get('agent_id'))
                or str(device.get('tenant_id')) != str(request.get('tenant_id'))
                or name not in {'lead_public_read','lead_document_read','lead_task_read'}):
            raise ValueError('executor_tool_scope_changed')
        if name != 'lead_public_read':
            if context.get('workspace_request') or set(arguments) != {'id'}:
                raise ValueError('executor_tool_scope_changed')
            result = await asyncio.to_thread(_business_read, request, name, arguments['id'])
            if revalidate is not None:
                with revalidate():
                    pass
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False,default=str)}],
                'structuredContent':result}
        if set(arguments) != {'url'}:
            raise ValueError('executor_tool_scope_changed')
        from .admin_settings import for_agent
        async def check_research_settings():
            settings,_=await asyncio.to_thread(for_agent,str(request['agent_id']),'models')
            tools=settings['tools']
            if (not tools.get('public_web')
                    or context.get('mode')=='community_discovery' and not tools.get('community_research')
                    or context.get('requires_advanced_research') and not tools.get('advanced_research')):
                raise ValueError('executor_public_research_disabled')
        await check_research_settings()
        from .fetch import fetch_product
        from .research import public_url
        url=public_url(arguments['url'])
        if context.get('mode')=='community_discovery':
            from urllib.parse import urlsplit
            # The existing community researcher reads source pages, not Telegram.
            if (urlsplit(url).hostname or '').lower() in ('t.me','telegram.me','web.telegram.org','telegram.org'):
                raise ValueError('community_source_access_unavailable')
        growth_source = (revalidate is not None and request.get('task_id')
                         and await asyncio.to_thread(_growth_public_read, revalidate))
        try:
            observed=await fetch_product(url,external_links=True)
        except (PublicReaderPolicyError, OSError, httpx.RequestError, httpx.HTTPStatusError) as error:
            if isinstance(error, PermissionError):
                raise
            await check_research_settings()
            if revalidate is not None:
                with revalidate():
                    pass
            return public_read_failure(url, error)
        if not growth_source:
            await check_research_settings()
        if revalidate is not None and not growth_source:
            with revalidate():
                pass
        observed['producer']='codex-public-read'
        if growth_source:
            observed = await asyncio.to_thread(_growth_public_read, revalidate, observed)
        if context.get('browser_run_id'):
            observed=await asyncio.to_thread(_record_public_read,request,context,observed)
        # fetch_product enforces the existing public-network/redirect boundary.
        return {'content':[{'type':'text','text':json.dumps(observed,ensure_ascii=False,default=str)}],
            'structuredContent':observed if isinstance(observed,dict) else {'result':observed}}
    context=request_context(device,request)
    from .channel_contracts import ExecutionRef
    from .execution_routes import endpoint, resolve
    from .security import internal_token
    aid=str(request['agent_id'])
    route=await asyncio.to_thread(resolve,aid,profile_id=context['profile_id'])
    target=endpoint(ExecutionRef.model_validate(route))
    async with httpx.AsyncClient(timeout=55,trust_env=False,follow_redirects=False) as client:
        response=await client.post(target+f"/agents/{aid}/local-executor/{request['id']}/tool",
            headers={'Authorization':'Bearer '+internal_token()},json={'context':context,'name':name,'arguments':arguments})
    if response.is_error:
        try:
            failure=response.json()
        except ValueError:
            failure={'code':'browser_tool_unavailable'}
        return {'isError':True,'content':[{'type':'text','text':json.dumps(failure,ensure_ascii=False)}]}
    return response.json()


def public_read_failure(url, error):
    """Keep typed source errors finite; caller authority barriers still apply."""
    if isinstance(error, PublicReaderPolicyError):
        failure = {'code': 'source_policy_denied', 'category': 'policy', 'failure_scope': 'source',
            'retryable': False, 'knowledge_progress': False, 'business_progress': False}
        return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(failure)}],
            'structuredContent': failure}
    codes = {'source_dns_unavailable', 'source_transport_unavailable', 'source_timeout',
        'reader_busy', 'reader_unavailable', 'reader_invalid_response'}
    reason = getattr(error, 'code', None)
    if reason not in codes:
        reason = ('source_dns_unavailable' if isinstance(error, socket.gaierror)
            else 'source_timeout' if isinstance(error, (TimeoutError, httpx.TimeoutException))
            else 'source_http_error' if isinstance(error, httpx.HTTPStatusError)
            else 'source_transport_unavailable')
    failure = {'code': 'source_unavailable', 'reason': reason,
        'failure_scope': 'platform' if reason.startswith('reader_') else 'source',
        'source_ref': url, 'knowledge_progress': False, 'business_progress': False}
    if isinstance(error, httpx.HTTPStatusError):
        failure['http_status'] = error.response.status_code
    return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(failure, ensure_ascii=False)}],
        'structuredContent': failure}


def _search_index(url):
    from urllib.parse import urlsplit

    from .operating_work import SITE_SEARCH
    parsed = urlsplit(url)
    host, path = (parsed.hostname or '').lower().removeprefix('www.'), parsed.path.rstrip('/')
    if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'):
        return path in {'', '/html', '/lite'}
    if (host.startswith('google.') or host in {'bing.com', 'search.yahoo.com', 'search.brave.com'}):
        return path in {'/search', '/web'}
    return any(host == domain and path == urlsplit(prefix).path.rstrip('/')
               for domain, prefix, _ in SITE_SEARCH.values())


def _growth_public_read(revalidate, observed=None):
    """Retain only a current growth Task's real source, under the original guard."""
    from .admin_settings import effective
    from .db import one, require_agent
    from .growth.store import enabled
    from .growth.tools import _remember, source_failure

    with revalidate() as (c, request):
        aid = str(request['agent_id'])
        task = one(c, 'SELECT type,state FROM lead.tasks WHERE id=%s', (request['task_id'],))
        if not task or not task['type'].startswith('growth.'):
            return False if observed is None else observed
        context = request['payload'].get('tool_context') or {}
        agent = require_agent(c, aid)
        if (context.get('kind') != 'model' or str(context.get('task_id')) != str(request['task_id'])
                or task['state'] != 'running' or agent['desired_state'] in {'paused','archived'} or not enabled(agent)):
            raise ValueError('executor_scope_changed')
        settings, _ = effective(c, 'models', aid)
        tools = settings['tools']
        if (not tools.get('public_web')
                or context.get('mode') == 'community_discovery' and not tools.get('community_research')
                or context.get('requires_advanced_research') and not tools.get('advanced_research')):
            raise ValueError('executor_public_research_disabled')
        if observed is None:
            return True
        reason = (source_failure(observed) or ('javascript_required' if observed.get('requires_javascript') else None)
                  or ('search_index' if _search_index(observed['url']) else None))
        if reason:
            return {**observed, 'source_state': reason, 'knowledge_progress': False}
        reference, added = _remember(aid, observed, connection=c)
        if not reference:
            return {**observed, 'source_state': 'empty', 'knowledge_progress': False}
        return {**observed, 'document_id': str(reference.id), 'evidence_ref': reference.model_dump(mode='json'),
                'knowledge_progress': added, 'business_progress': False, 'trust': 'observed_source_claims'}


def _business_read(request, name, reference):
    from uuid import UUID

    from .db import all_rows, one, require_agent, tx
    identifier = str(UUID(str(reference)))
    with tx(str(request['agent_id'])) as c:
        require_agent(c, request['agent_id'])
        if name == 'lead_document_read':
            row = one(c, "SELECT id,title,source_ref,source_type,trust_state,created_at FROM lead.knowledge_documents WHERE id=%s AND deleted_at IS NULL", (identifier,))
            if not row:
                raise LookupError('source_unavailable')
            return {**row, 'chunks':all_rows(c, "SELECT ordinal,body FROM lead.knowledge_chunks WHERE document_id=%s ORDER BY ordinal", (identifier,))}
        row = one(c, "SELECT id,type,state,input->'spec' specification,result->'output' output FROM lead.tasks WHERE id=%s", (identifier,))
        if not row:
            raise LookupError('task_unavailable')
        artifacts, limited = _content_artifacts(c, str(request['agent_id']), row.get('output'))
        return {**row, 'resolved_artifacts': artifacts, 'resolved_artifacts_limited': limited}


def _content_artifacts(c, aid, output):
    """Resolve only immutable content references already retained by this task."""
    from uuid import UUID

    from .db import one
    from .growth.store import digest

    references = (output or {}).get('artifacts', []) if isinstance(output, dict) else []
    if not isinstance(references, list):
        return [], True
    result, limited = [], len(references) > 16
    for ref in references[:16]:
        if not isinstance(ref, dict) or ref.get('kind') != 'content':
            continue
        item = {'kind': 'content', 'available': False}
        try:
            identifier = str(UUID(str(ref.get('id'))))
        except ValueError:
            result.append(item)
            continue
        revision, sha = ref.get('revision'), ref.get('hash')
        if (type(revision) is not int or not 1 <= revision <= 2147483647
                or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)):
            result.append(item)
            continue
        item.update(id=identifier, revision=revision, hash=sha)
        result.append(item)
    budget = 256000 - len(json.dumps({'resolved_artifacts': result, 'resolved_artifacts_limited': False},
                                   ensure_ascii=False, default=str).encode())
    for index, item in enumerate(result):
        if 'id' not in item:
            continue
        content = one(c, """SELECT CASE WHEN data_bytes<=256000 THEN body END body,
                CASE WHEN data_bytes<=256000 THEN source_refs END source_refs,
                CASE WHEN data_bytes<=256000 THEN validation END validation,data_bytes
            FROM (SELECT *,coalesce(octet_length(body::text),4)+coalesce(octet_length(source_refs::text),4)
                    +coalesce(octet_length(validation::text),4) data_bytes
                FROM (SELECT body,source_refs,CASE WHEN jsonb_typeof(validation_result)='object' THEN
                    (SELECT coalesce(jsonb_object_agg(field,validation_result->field),'{}'::jsonb)
                     FROM unnest(ARRAY['publication_status','optional_media_missing','business_profile_id',
                        'growth_task_id','claim_checks']) fields(field) WHERE validation_result ? field)
                    END validation FROM lead.content_items
                    WHERE tenant_id=lead.ctx_tenant() AND agent_id=%s AND id=%s AND revision=%s
                        AND content_hash=%s) projected) bounded""",
            (aid, item['id'], item['revision'], item['hash']))
        if content and content['data_bytes'] > 256000:
            limited = True
        if (content and isinstance(content['body'], dict) and digest(content['body']) == item['hash']
                and isinstance(content['source_refs'], list) and isinstance(content['validation'], dict)):
            candidate = {**item, 'available': True, 'body': content['body'],
                'source_refs': content['source_refs'],
                'validation': content['validation']}
            size = (len(json.dumps(candidate, ensure_ascii=False, default=str).encode())
                    - len(json.dumps(item, ensure_ascii=False, default=str).encode()))
            if size <= budget:
                result[index], budget = candidate, budget - size
            else:
                limited = True
    return result, limited


def _record_public_read(request,context,observed):
    """A real HTTP read in the retained evidence table, never claimed as DOM."""
    from .db import j, one, require_agent, tx, uid
    from .scope import current_tenant
    aid=str(request['agent_id'])
    with tx(aid) as c:
        agent=require_agent(c,aid)
        run=one(c,'SELECT * FROM lead.browser_runs WHERE id=%s FOR UPDATE',(context['browser_run_id'],))
        if (not run or str(run['agent_id'])!=aid or run['state']!='running'
                or run['control_version']!=agent['control_version'] or run['config_revision']!=agent['config_revision']):
            raise ValueError('executor_research_scope_changed')
        step=uid()
        sequence=one(c,'SELECT coalesce(max(seq),0)+1 n FROM lead.browser_steps WHERE run_id=%s',(run['id'],))['n']
        value={**observed,'engine':'codex-web','elements':[],'has_password':False}
        # Real source links retain their labels; these are observations, not grants.
        from .community_links import telegram_link
        value['community_links']=[{'href':item,'name':''} for item in value.get('links',[])
            if isinstance(item,str) and telegram_link(item)]
        value['community_entries']=[]
        c.execute("""INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state,output,ended_at)
            VALUES(%s,%s,%s,%s,%s,%s,'codex-public-read',%s,'completed',%s,now())""",
            (step,current_tenant(),aid,run['id'],sequence,uid(),j({'url':observed['url']}),j(value)))
        return {**value,'evidence_id':step,'run_id':str(run['id'])}
