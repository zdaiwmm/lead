"""Authenticated Lead MCP handlers; original business services own all authority."""
import asyncio
import json

import httpx

from .local_executor_browser import request_context


async def call(device, request, name, arguments):
    payload=request.get('payload') or {}
    context=payload.get('tool_context') or {}
    if context.get('kind') == 'model':
        if (str(device.get('id',device.get('device_id'))) != str(request.get('device_id'))
                or str(device.get('agent_id')) != str(request.get('agent_id'))
                or str(device.get('tenant_id')) != str(request.get('tenant_id'))
                or name != 'lead_public_read' or set(arguments) != {'url'}):
            raise ValueError('executor_tool_scope_changed')
        from .fetch import fetch_product
        from .research import public_url
        url=public_url(arguments['url'])
        if context.get('mode')=='community_discovery':
            from urllib.parse import urlsplit
            # The existing community researcher reads source pages, not Telegram.
            if (urlsplit(url).hostname or '').lower() in ('t.me','telegram.me','web.telegram.org','telegram.org'):
                raise ValueError('community_source_access_unavailable')
        observed=await fetch_product(url,external_links=True)
        observed['producer']='codex-public-read'
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
        value['community_links']=[item for item in value.get('links',[]) if telegram_link(item.get('url',item.get('href','')))]
        value['community_entries']=[]
        c.execute("""INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state,output,ended_at)
            VALUES(%s,%s,%s,%s,%s,%s,'codex-public-read',%s,'completed',%s,now())""",
            (step,current_tenant(),aid,run['id'],sequence,uid(),j({'url':observed['url']}),j(value)))
        return {**value,'evidence_id':step,'run_id':str(run['id'])}
