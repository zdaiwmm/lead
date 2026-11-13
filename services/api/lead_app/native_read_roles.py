"""Bounded read/session envelopes observed under existing task/page authority."""
import asyncio
import copy
import hashlib
from urllib.parse import parse_qs

from .browser_execution import origin
from .native_read_facts import inspect_search_form
from .native_session_facts import (
    message_bridges,
    reject_dangerous_endpoint,
    worker_envelope,
)


def form_body(request):
    headers={key.lower():value for key,value in request.get('headers',{}).items()}
    if not headers.get('content-type','').lower().startswith('application/x-www-form-urlencoded'):
        raise ValueError('read_body_not_a_form')
    raw=request.get('postData','')
    if not isinstance(raw,str) or len(raw)>16000:
        raise ValueError('read_body_outside_bounds')
    parsed=parse_qs(raw,keep_blank_values=True,strict_parsing=True,max_num_fields=32)
    if any(len(value)!=1 for value in parsed.values()):
        raise ValueError('read_body_duplicate_field')
    return {key:value[0] for key,value in parsed.items()}


async def private_identity(runtime,scope,page):
    if scope.get('account_id') and not scope.get('connection_ref'):
        from .native_research import NativeResearchPage
        reader=NativeResearchPage(runtime.actor,scope['task_id'])
        reader.lease=scope
        reader.page=page
        reader.context=runtime.actor.context
        await reader.verify_account_identity()


async def authorize_search(runtime,scope,target,request):
    try:
        async with asyncio.timeout(12):
            reject_dangerous_endpoint(request['url'])
            body=form_body(request)
            root=scope['target_id']
            current=runtime.pages[root]['page']
            revision=scope.get('document_revision',0)
            await private_identity(runtime,scope,current)
            copied=copy.deepcopy(scope)
            for key in ('target_id','action'):
                copied.pop(key,None)
            # Sender-page CDP waits while its POST is paused. Read its genuine
            # current endpoint/form on a bounded same-scope internal page.
            lease=await runtime.acquire_page(copied,target['url'],internal_parent=root)
            page=runtime.pages[lease['target_id']]['page']
            try:
                facts=await inspect_search_form(page,request_url=request['url'])
                envelope=facts['request']
                expected={envelope['query_field'],*envelope['fixed_fields'],*envelope['dynamic_fields']}
                if (set(body)!=expected or any(body[name]!=value for name,value in envelope['fixed_fields'].items())
                        or len(body[envelope['query_field']])>facts['max_query_length']
                        or any(not 1<=len(body[name])<=4000 for name in envelope['dynamic_fields'])):
                    return False
                await asyncio.to_thread(runtime.current_scope,scope)
                if revision!=scope.get('document_revision',0) or origin(current.url)!=origin(facts['source_url']):
                    return False
                return {'role':'search','task_id':scope['task_id'],'page_target_id':root,'binding_version':scope.get('binding_version'),
                    'document_revision':revision,'request_hash':hashlib.sha256(repr(sorted(body.items())).encode()).hexdigest()}
            finally:
                await runtime.release_page(lease['target_id'])
    except (ValueError,KeyError,TimeoutError):
        return False


async def compiled_source(runtime,session,script):
    gate=runtime.gate
    metadata=copy.deepcopy(gate.scripts.get((session,script)))
    if not metadata or session not in gate.targets:
        raise ValueError('compiled_script_unavailable')
    source=await gate.send('Debugger.getScriptSource',{'scriptId':script},session)
    if gate.scripts.get((session,script))!=metadata or session not in gate.targets:
        raise ValueError('compiled_script_changed')
    raw=source.get('scriptSource','')
    if len(raw)>200000:
        raise ValueError('compiled_script_outside_bounds')
    return raw,metadata


async def controls(page,bridge):
    return await page.evaluate(r'''bridge=>Array.from(document.querySelectorAll(bridge.selector)).filter(node=>{
        const bounds=node.getBoundingClientRect(),style=getComputedStyle(node);
        const text=(node.getAttribute('aria-label')||node.innerText||node.value||'').trim();
        const context=node.closest('section,form,fieldset,[role=group]')||node.parentElement;
        const costText=context?.innerText||text;
        if (/\brenew\b.{0,50}\bmembership\b/i.test(text)&&!/\bsession\b|sign[ -]?in|log[ -]?in|会话|登录/i.test(text)) return false;
        if (/\b(?:pay(?:ment|ing)?|charg(?:e|ed|es)|billing|checkout|purchase|subscrib(?:e|ed)|subscription|invoice|cost|price|fee|amount|currency)\b|[$€£¥￥]\s*\d|付费|付款|支付|续费|充值|扣款|订阅|购买|结算|会员费|金额/i.test(costText)) return false;
        return !node.closest('article,blockquote,nav,header,footer')&&bounds.width>0&&bounds.height>0&&style.visibility!=='hidden'&&style.display!=='none'
            && (/\b(?:refresh|renew)\b.{0,50}\b(?:session|membership|sign.in)\b|刷新.{0,20}(?:会话|会员|登录状态)|续期.{0,20}(?:会话|登录)/i.test(text))
            && !/publish|reply|delete|admin|password|log.?out|sign.?out|complete|发布|回复|删除|管理|密码|退出/i.test(text);
    }).slice(0,20).map(node=>({message:Object.fromEntries(Object.entries(bridge.fields).map(([name,key])=>[name,node.dataset[key]]))}))''',bridge)


async def authorize_maintenance(runtime,target,request):
    try:
        async with asyncio.timeout(12):
            reject_dangerous_endpoint(request['url'])
            body=form_body(request)
            meta=request['_native_network']
            session=meta['session']
            gate=runtime.gate
            if gate.targets.get(session)!=target or target.get('browserContextId')!=gate.context_id:
                return False
            recorded=gate.network[(meta['network_id'],session)]
            frame=recorded.get('initiator',{}).get('stack',{}).get('callFrames',[None])[0]
            if not frame:
                return False
            script=frame['scriptId']
            source,worker_meta=await compiled_source(runtime,session,script)
            if worker_meta.get('url')!=target['url'] or frame.get('url')!=target['url']:
                return False
            candidates=[]
            for entry in list(runtime.pages.values()):
                scope=entry['scope']
                if entry.get('internal') or scope.get('kind')=='identity_probe' or origin(target['url']) not in {origin(url) for url in scope['origins']}:
                    continue
                try:
                    await asyncio.to_thread(runtime.current_scope,scope)
                    sessions=[sid for sid,info in gate.targets.items() if info['targetId']==scope['target_id']]
                    if len(sessions)!=1:
                        continue
                    page_session=sessions[0]
                    versions=[value for value in gate.worker_versions_by_page.get(page_session,{}).values() if value.get('targetId')==target['targetId']
                        and value['status']=='activated' and value['runningStatus']=='running' and scope['target_id'] in value.get('controlledClients',[])
                        and gate.worker_versions.get(value['versionId'],{}).get('status')!='redundant']
                    if len(versions)!=1:
                        continue
                    version=copy.deepcopy(versions[0])
                    page=entry['page']
                    await private_identity(runtime,scope,page)
                    revision=scope.get('document_revision',0)
                    scripts=[(sid,info) for (owner,sid),info in list(gate.scripts.items()) if owner==page_session and info.get('url')==page.url]
                    if len(scripts)>20:
                        continue
                    for sid,_ in scripts:
                        page_source,page_meta=await compiled_source(runtime,page_session,sid)
                        for bridge in message_bridges(page_source):
                            for control in await controls(page,bridge):
                                envelope=worker_envelope(source,script_url=target['url'],line=frame['lineNumber'],column=frame['columnNumber'],message=control['message'])
                                if envelope['url']!=request['url'] or envelope['fields']!=body:
                                    continue
                                await asyncio.to_thread(runtime.current_scope,scope)
                                if revision!=scope.get('document_revision',0) or gate.scripts.get((page_session,sid))!=page_meta or gate.scripts.get((session,script))!=worker_meta or gate.worker_versions_by_page.get(page_session,{}).get(version['versionId'])!=version or gate.worker_versions.get(version['versionId'],{}).get('status')=='redundant':
                                    continue
                                digest=hashlib.sha256(repr(sorted(body.items())).encode()).hexdigest()
                                key=(scope['target_id'],digest)
                                candidates.append((key,{'role':'session_maintenance','task_id':scope['task_id'],'page_target_id':scope['target_id'],
                                    'binding_version':scope.get('binding_version'),'document_revision':revision,'worker_target_id':target['targetId'],
                                    'worker_version_id':version['versionId'],'worker_registration_id':version['registrationId'],
                                    'worker_script_id':script,'worker_script_hash':hashlib.sha256(source.encode()).hexdigest(),
                                    'page_script_id':sid,'page_script_hash':hashlib.sha256(page_source.encode()).hexdigest(),'nonce_hash':digest}))
                except (ValueError,KeyError):
                    continue
            unique={key:proof for key,proof in candidates}
            if len(unique)!=1:
                return False
            key,proof=next(iter(unique.items()))
            used={key for key in getattr(runtime,'maintenance_used',set()) if key[0] in runtime.pages}
            if key in used or len(used)>=256:
                return False
            runtime.maintenance_used=used|{key}
            return proof
    except (ValueError,KeyError,IndexError,TimeoutError):
        return False
