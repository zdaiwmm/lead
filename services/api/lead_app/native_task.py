"""Current-task native pages and bounded discovery; original DB owns authority."""
import asyncio
import copy
import time

from .browser_execution import origin, validate_method
from .db import one, tx
from .native_method_facts import inspect_editor
from .native_research import NativeResearchPage
from .research import account_read_origins


class NativeTask:
    def __init__(self,actor,task_id):
        self.actor=actor
        self.task_id=str(task_id)
        self.worker_id=self.task_id
        self.lease=None
        self.page=None
        self.observations=[]
        self.operation_lock=asyncio.Lock()
        self.steps=0
        self.corrections=0
        self.total_corrections=0
        self.fragment_failures=0
        self.deadline=time.monotonic()+180

    def remaining_seconds(self):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:
            raise ValueError('native_fragment_deadline')
        return remaining

    def start_fragment(self):
        from .db import j
        with tx(self.actor.aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE',(self.task_id,))
            progress=(task['result'] or {}).get('runtime',{}).get('native_progress',{})
            if progress.get('fragments',0)>=3 or progress.get('no_progress',0)>=3 or progress.get('corrections',0)>=8:
                raise ValueError('native_task_progress_limit')
            self.fragment_failures=0
            self.corrections=progress.get('no_progress',0)
            self.total_corrections=progress.get('corrections',0)
            progress['fragments']=progress.get('fragments',0)+1
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,native_progress}',%s) WHERE id=%s",(j(progress),self.task_id))

    def note_correction(self):
        from .db import j
        self.fragment_failures+=1
        with tx(self.actor.aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE',(self.task_id,))
            progress=(task['result'] or {}).get('runtime',{}).get('native_progress',{})
            progress['corrections']=progress.get('corrections',0)+1
            if self.fragment_failures>=2:
                progress['stopped_reason']='one_correction_exhausted'
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,native_progress}',%s) WHERE id=%s",(j(progress),self.task_id))
            self.total_corrections=progress['corrections']

    async def observe_progress(self):
        """One real state sample per official SDK step; only a digest is persisted."""
        import hashlib
        import json

        from .db import j
        self.remaining_seconds()
        await self.descriptor()
        await self.verify_private_self()
        before=await self.document()
        facts=await self.page.evaluate(r"""()=>{
            const transient='script,style,noscript,time,[datetime],[role=status],[role=progressbar],[aria-live]:not([aria-live=off])';
            const visible=node=>{const r=node.getBoundingClientRect();const s=getComputedStyle(node);
                return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none';};
            const normalized=text=>text.replace(/\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b/g,'<time>')
                .replace(/\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[AP]M)?\b/gi,'<time>').replace(/\s+/g,' ').trim();
            const text=[];const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
            while(walker.nextNode()) {const node=walker.currentNode.parentElement;
                if(node&&!node.closest(transient)&&visible(node)&&!node.getAnimations().length){const value=normalized(walker.currentNode.textContent);if(value)text.push(value);}}
            const url=new URL(location.href); for(const key of [...url.searchParams.keys()])
                if(/^(?:_|timestamp|cache_bust|cachebuster)$/i.test(key)) url.searchParams.delete(key);
            return {url:url.href,text:text.join(' ').slice(0,200000),
                viewport:[Math.floor(scrollX/Math.max(1,innerWidth)),Math.floor(scrollY/Math.max(1,innerHeight*.8))],
                fields:Array.from(document.querySelectorAll('input,textarea,select')).filter(node=>visible(node)&&!['password','hidden'].includes(node.type)&&node.autocomplete!=='one-time-code')
                    .map(node=>[node.tagName,node.name,node.value])};
        }""")
        if before!=await self.document():
            raise ValueError('native_progress_document_changed')
        signature=hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()
        with tx(self.actor.aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE',(self.task_id,))
            progress=(task['result'] or {}).get('runtime',{}).get('native_progress',{})
            seen=progress.get('seen_states',[])
            progress['no_progress']=progress.get('no_progress',0)+1 if signature in seen else 0
            progress['seen_states']=(seen+[signature])[-32:] if signature not in seen else seen
            progress['state_hash']=signature
            self.corrections=progress['no_progress']
            stopped=self.fragment_failures>=2 or self.corrections>=3 or self.total_corrections>=8 or self.steps>=20
            if stopped:
                progress['stopped_reason']='one_correction_exhausted' if self.fragment_failures>=2 else 'no_progress' if self.corrections>=3 else 'active_step_limit'
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,native_progress}',%s) WHERE id=%s",(j(progress),self.task_id))
        return {'stop':stopped,'reason':progress.get('stopped_reason') if stopped else None}

    def snapshot(self):
        with tx(self.actor.aid) as c:
            task=one(c,'SELECT t.*,a.control_version current_control FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id WHERE t.id=%s',(self.task_id,))
            spec=(task or {}).get('input',{}).get('spec',{})
            account=one(c,"SELECT * FROM lead.social_accounts WHERE id=%s AND state='connected'",(spec.get('account_id'),))
            if (not task or task['state']!='running' or not account or str(account['profile_id'])!=str(self.actor.profile['id'])
                    or task['input'].get('binding_version')!=account['binding_version']):
                raise ValueError('native_task_scope_changed')
            runtime=(task['result'] or {}).get('runtime',{})
            if runtime.get('control_version')!=task['current_control']:
                raise ValueError('native_task_control_changed')
            intent=one(c,'SELECT * FROM lead.growth_read_intent(%s)',(account['id'],))
            if not intent or intent['desired']!='running':
                raise ValueError('native_task_account_paused')
            connection=one(c,"""SELECT connection_data FROM lead.takeover_sessions WHERE completed_account_id=%s
                AND connection_data ? 'self_url' AND connection_data ? 'self_selector'
                ORDER BY ended_at DESC NULLS LAST LIMIT 1""",(account['id'],))
            if not connection:
                raise ValueError('native_private_identity_method_missing')
            account['private_self']=connection['connection_data']
            return task,account,intent

    async def start(self,url=None):
        self.remaining_seconds()
        task,account,intent=await asyncio.to_thread(self.snapshot)
        if self.lease:
            await self.descriptor()
            return await self.descriptor()
        with tx(self.actor.aid) as c:
            origins=account_read_origins(c,account['id'],account['binding_version'])
        checkpoint=(task['result'] or {}).get('runtime',{}).get('checkpoint',{})
        resume=checkpoint if checkpoint.get('stage')=='native_discovery' else {}
        if resume and resume.get('execution_ref')!=self.actor.native_runtime.execution_ref:
            raise ValueError('native_checkpoint_execution_changed')
        url=url or resume.get('url') or (task['input'].get('spec',{}).get('payload',{}).get('target_urls') or [account['private_self']['self_url']])[0]
        if origin(url) not in {origin(value) for value in origins}:
            raise ValueError('native_task_url_outside_account')
        scope={'task_id':self.task_id,'control_version':task['current_control'],'task_generation':task['input']['task_generation'],
            'claim':{'attempt_token':task['result']['runtime']['attempt_token']},'account_id':str(account['id']),
            'binding_version':account['binding_version'],'intent_revision':intent['revision'],'origins':origins,
            'private_self':account['private_self']}
        from datetime import timedelta

        from .growth.contracts import utcnow
        scope['fragment_deadline']=(utcnow()+timedelta(seconds=self.remaining_seconds())).isoformat()
        await self.actor.forward_proxy.validate_url(url)
        self.lease=await self.actor.native_runtime.acquire_page(scope,url)
        self.page=self.actor.native_runtime.pages[self.lease['target_id']]['page']
        if resume and resume.get('execution_ref')==self.actor.native_runtime.execution_ref:
            await self.page.evaluate('point=>scrollTo(point[0],point[1])',resume.get('scroll',[0,0]))
        return await self.descriptor()

    async def checkpoint(self):
        before=await self.document()
        scroll=await self.page.evaluate('()=>[scrollX,scrollY]')
        value={'stage':'native_discovery','url':self.page.url,'scroll':scroll,
            'execution_ref':self.actor.native_runtime.execution_ref}
        if before!=await self.document():
            raise ValueError('native_checkpoint_document_changed')
        return value

    async def descriptor(self):
        scope=await self.actor.native_runtime.scope_view(self.lease['target_id'])
        return {**scope,'cdp_url':self.actor.native_runtime.bridge.url,'headers':self.actor.native_runtime.bridge.headers}

    async def document(self,page=None,scope=None):
        if page is not None and page is not self.page and scope is None:
            scope=next((entry['scope'] for entry in self.actor.native_runtime.pages.values() if entry['page'] is page),None)
            if scope is None:
                raise ValueError('native_internal_page_missing')
        reader=NativeResearchPage(self.actor,self.task_id)
        reader.identity_required=False
        reader.lease=scope or self.lease
        reader.page=page or self.page
        reader.context=self.actor.context
        return await reader.observation_scope()

    async def temporary_page(self,url):
        scope=copy.deepcopy(self.lease)
        for key in ('target_id','action'):
            scope.pop(key,None)
        scope=await self.actor.native_runtime.acquire_page(scope,url,internal_parent=self.lease['target_id'])
        return self.actor.native_runtime.pages[scope['target_id']]['page'],scope

    async def verify_private_self(self):
        _,account,_=await asyncio.to_thread(self.snapshot)
        facts=account['private_self']
        page,scope=await self.temporary_page(facts['self_url'])
        try:
            before=await self.document(page,scope)
            if page.url!=facts['self_url']:
                raise ValueError('native_self_endpoint_redirected')
            field=page.locator(facts['self_selector'])
            if await field.count()!=1 or not await field.is_visible():
                raise ValueError('native_self_subject_missing')
            actual=await field.evaluate("node=>('value' in node ? node.value : node.innerText||'').trim()")
            if actual!=account['external_account_id']:
                with tx(self.actor.aid) as c:
                    c.execute("UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s AND binding_version=%s AND external_account_id=%s AND state='connected'",
                        (account['id'],account['binding_version'],account['external_account_id']))
                raise ValueError('native_self_subject_changed')
            if before!=await self.document(page,scope):
                raise ValueError('native_self_subject_changed')
            return account
        finally:
            await self.actor.native_runtime.release_page(scope['target_id'])

    async def authorize(self,args):
        self.steps+=1
        if self.steps>20 or self.corrections>=3 or self.total_corrections>=8:
            raise ValueError('native_task_progress_limit')
        await self.descriptor()
        name,values=args.get('name'),args.get('values') or {}
        if name=='scroll':
            return {'allowed':True}
        if name=='navigate':
            return {'allowed':origin(values.get('url','')) in {origin(url) for url in self.lease['origins']}}
        # Reuse the same freshly resolved, owned-frame read-link check.
        reader=NativeResearchPage(self.actor,self.task_id)
        reader.lease=self.lease
        reader.page=self.page
        reader.context=self.actor.context
        return await reader.authorize(args)

    async def record_method(self,args):
        task,account,intent=await asyncio.to_thread(self.snapshot)
        if task['type']!='growth.account' or task['input']['spec']['purpose']!='discover':
            raise ValueError('method_discovery_task_required')
        await self.verify_private_self()
        target=args['target_url']
        operation=args['operation']
        if operation not in ('publish','reply') or origin(target) not in {origin(url) for url in self.lease['origins']}:
            raise ValueError('method_discovery_scope_changed')
        excluded=intent['scope'].get('excluded_targets',[])
        if target in excluded or intent['scope'].get('target_scope')=='existing_grants' and target not in intent['scope'].get('target_ids',[]):
            raise ValueError('target_not_permitted')
        proposal={key:value for key,value in args.items() if key!='operation'}
        permission_page,permission_scope=(self.page,None) if self.page.url==target else await self.temporary_page(target)
        try:
            before=await self.document()
            permission_before=await self.document(permission_page,permission_scope) if permission_scope else before
            evidence='native-method:'+self.task_id+':'+str(len(self.observations)+1)
            method=await inspect_editor(self.page,proposal,account_id=account['id'],binding_version=account['binding_version'],
                external_account_id=account['external_account_id'],connection_origin=account['connection_origin'],
                operation=operation,evidence_ref=evidence,permission_page=permission_page)
            if before!=await self.document() or permission_scope and permission_before!=await self.document(permission_page,permission_scope):
                raise ValueError('method_document_changed')
            method['self_url']=account['private_self']['self_url']
            method['platform_permission']['selector']=args['permission_selector']
            validate_method(method,account_id=account['id'],binding_version=account['binding_version'],target_url=target,operation=operation)
            observed={'external_target_id':target,'canonical_url':target,'target_type':'web',
                'display_name':(await permission_page.title())[:200] or target,'acting_subject':account['external_account_id'],
                'access_mode':'ordinary_member','evidence_refs':[evidence],'browser_method':method,
                'producer_scope':{'task_id':self.task_id,'task_generation':task['input']['task_generation'],
                    'attempt_token':task['result']['runtime']['attempt_token'],'control_version':task['current_control'],
                    'intent_revision':intent['revision'],'execution_ref':self.actor.native_runtime.execution_ref}}
            self.observations=[value for value in self.observations if (value['canonical_url'],value['browser_method']['operations'])!=(target,[operation])]
            self.observations.append(observed)
            return {'state':'method_observed','target_url':target,'operation':operation,'role':'ordinary_member','receipt_state':'unobserved'}
        finally:
            if permission_scope:
                await self.actor.native_runtime.release_page(permission_scope['target_id'])

    async def command(self,operation,args):
        if operation=='progress':
            return await asyncio.wait_for(self.observe_progress(),self.remaining_seconds())
        if operation in ('record_method','report_receipt','tool_authorize') and self.fragment_failures>=2:
            raise ValueError('native_one_correction_exhausted')
        try:
            value=await asyncio.wait_for(self._command(operation,args),self.remaining_seconds())
            if operation=='tool_authorize' and not value.get('allowed'):
                await asyncio.to_thread(self.note_correction)
            return value
        except (ValueError,KeyError,TypeError):
            if operation in ('record_method','report_receipt','tool_authorize'):
                await asyncio.to_thread(self.note_correction)
            raise

    async def _command(self,operation,args):
        if operation=='scope':
            await self.verify_private_self()
            return {key:value for key,value in (await self.descriptor()).items() if key not in ('cdp_url','headers')}
        if operation=='tool_authorize':
            return await self.authorize(args)
        if operation=='record_method':
            self.steps+=1
            if self.steps>20 or self.corrections>=3 or self.total_corrections>=8:
                raise ValueError('native_task_progress_limit')
            return await self.record_method(args)
        raise ValueError('native_task_operation_unsupported')

    async def close(self):
        if self.lease:
            await self.actor.native_runtime.release_page(self.lease['target_id'])
            self.lease=None
        self.page=None
