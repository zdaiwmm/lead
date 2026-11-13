"""Original ConnectionRef controller for a fixed native login page.

The SDK chooses observed controls. Only this controller authorizes credential
placeholders against a real password form; authentication never borrows a
publication ActionRef. Identity evidence is read from the current self surface.
"""
import asyncio
import re
import time

from .browser_execution import origin
from .connection_control import transition, unseal
from .db import j, one, tx

FORM_FACTS = '''function(){const form=this.form || this.closest('form'); if(!form)return null;
return {tag:this.tagName.toLowerCase(),type:(this.type||this.getAttribute('type')||'text').toLowerCase(),
name:this.getAttribute('name')||'',autocomplete:this.getAttribute('autocomplete')||'',
method:(form.getAttribute('method')||'GET').toUpperCase(),action:new URL(form.getAttribute('action')||location.href,form.baseURI).href,
passwordCount:form.querySelectorAll('input[type="password"]').length,
newPassword:!!form.querySelector('input[autocomplete="new-password"]'),
passwordEmpty:Array.from(form.elements).some(node=>node.type==='password'&&!node.disabled&&!node.value.length),
validatesOnSubmit:this.type==='submit'&&!form.noValidate&&!this.formNoValidate,
requiredEmpty:Array.from(form.elements).some(node=>node.willValidate&&node.validity.valueMissing),
invalidFields:Array.from(form.elements).some(node=>node.willValidate&&!node.validity.valid),
formText:form.innerText,heading:form.closest('main,section')?.querySelector('h1,h2')?.innerText||document.title,
fields:Array.from(form.elements).filter(node=>node.name&&!node.disabled).map(node=>node.name),
controls:Object.fromEntries(Array.from(form.elements).filter(node=>node.name&&!node.disabled&&
    /(?:^|[\\[.])(?:action|op|operation|mode|command|intent|role|privilege|access|access_level|scope)(?:\\])?$/i.test(node.name)).map(node=>[node.name,node.value])),
hasPassword:!!form.querySelector('input[type="password"]'),hasOtp:!!form.querySelector('input[autocomplete="one-time-code"]')};}'''


class NativeConnection:
    def __init__(self,actor,task_id,connection_id):
        self.actor=actor
        self.runtime=getattr(actor,'native_runtime',None)
        self.task_id=str(task_id)
        self.connection_id=str(connection_id)
        self.lease=None
        self.page=None
        self.observation=None
        self.proof_scope=None
        self.self_entry=None
        self.anonymous_self=[]
        self.operation_lock=asyncio.Lock()
        self.challenge_lock=asyncio.Lock()

    async def owner_page(self):
        flow=(self.lease or {}).get('auth_flow') or {}
        if flow and not flow.get('completed'):
            gate=self.actor.native_runtime.gate
            until=time.monotonic()+2
            while time.monotonic()<until:
                target=flow.get('popup_target')
                if target and any(info.get('targetId')==target and gate.owners.get(sid)==self.lease['target_id']
                        for sid,info in gate.targets.items()):
                    for page in self.actor.context.pages:
                        if page.is_closed():
                            continue
                        session=await self.actor.context.new_cdp_session(page)
                        try:
                            info=(await session.send('Target.getTargetInfo'))['targetInfo']
                        finally:
                            await session.detach()
                        if info['targetId']==target:
                            return page
                await asyncio.sleep(.02)
            raise ValueError('connection_auth_popup_missing')
        return self.page

    async def advance_auth_flow(self):
        flow=(self.lease or {}).get('auth_flow') or {}
        if not flow or flow.get('completed') or not flow.get('callback'):
            return False
        page=await self.owner_page()
        if origin(page.url) not in {origin(value) for value in self.lease['origins']} or page.url not in flow['urls']:
            return False
        links=await page.locator('a[href]').evaluate_all("nodes=>nodes.filter(node=>/return|continue|back|返回|继续/i.test(node.innerText)).map(node=>node.href)")
        links=[url for url in links if origin(url) in {origin(value) for value in self.lease['origins']}]
        if len(set(links))!=1:
            return False
        self.lease['authentication_dispatched']=True
        flow['completed']=True
        await self.page.goto(links[0],wait_until='domcontentloaded',timeout=30000)
        await page.close()
        return True

    async def prepare_owner_input(self,page,body):
        """Freeze one actually submitted person form before Chrome pauses it."""
        from .native_frame_roles import guard_owner_input
        await guard_owner_input(self,page,body)
        flow=(self.lease or {}).get('auth_flow') or {}
        if page is self.page and body.kind=='click' and (not flow or flow.get('completed')):
            from .native_auth_flow import popup_destination, reserve
            facts=await page.evaluate('''point=>{const node=document.elementFromPoint(point.x,point.y)?.closest('a,button,input');
                if(!node)return null;return {tag:node.tagName,text:node.innerText||node.value||'',href:node.href,
                    target:node.target||'',onclick:node.getAttribute('onclick')||'',form:!!node.form};}''',{'x':body.x,'y':body.y})
            if destination:=popup_destination(facts,page.url):
                await self.actor.forward_proxy.validate_url(destination)
                reserve(self.lease,destination)
            return
        if not flow or flow.get('completed') or page is self.page or body.kind not in ('click','key'):
            return
        if body.kind=='key' and body.text!='Enter':
            return
        from .native_auth_flow import body_hash
        target=flow.get('popup_target')
        before=self.actor.native_runtime.gate.documents.get(target)
        values=await page.evaluate('''args=>{
            const node=args.kind==='click'?document.elementFromPoint(args.x,args.y):document.activeElement;
            const submit=node?.closest('button,input[type=submit]');
            if(args.kind==='click'&&(!submit||submit.type!=='submit'))return null;
            const form=node?.form||submit?.form;if(!form||(form.getAttribute('method')||'GET').toUpperCase()!=='POST'||
                !['','application/x-www-form-urlencoded'].includes(form.enctype))return null;
            if(!/sign[\\s-]*in|log[\\s-]*in|authenticate|verify|登录|登入|验证/i.test(form.innerText+' '+document.title)||
                /delete|purchase|billing|subscribe|购买|删除|支付/i.test(form.innerText))return null;
            if(!form.querySelector('input[type=password],input[autocomplete="one-time-code"]'))return null;
            const fields={};for(const item of form.elements){
                if(!item.name||item.disabled||['button','reset','submit','file'].includes(item.type))continue;
                if(item.type==='checkbox'||item.type==='radio')return null;
                if(Object.hasOwn(fields,item.name))return null;fields[item.name]=item.value;
            }
            if(submit?.name)fields[submit.name]=submit.value;
            return {url:new URL(form.getAttribute('action')||location.href,form.baseURI).href,fields};
        }''',{'kind':body.kind,'x':body.x,'y':body.y})
        if not values or len(values['fields'])>40 or origin(values['url'])!=origin(page.url):
            return
        from .native_auth_flow import check_auth_form
        check_auth_form(values['url'],values['fields'])
        if before!=self.actor.native_runtime.gate.documents.get(target):
            raise ValueError('connection_auth_document_changed')
        flow['document']=before
        flow['owner_form']={'url':values['url'],'body_hash':body_hash(values['fields']),
            'document':before,'version':self.lease['challenge_version'],'used':False,
            'expires':time.monotonic()+10}
        if values['url'] not in flow['urls']:
            flow['urls'].append(values['url'])

    def snapshot(self):
        with tx(self.actor.aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(self.task_id,))
            connection=one(c,"SELECT * FROM lead.takeover_sessions WHERE id=%s AND purpose='login'",(self.connection_id,))
            payload=((task or {}).get('input') or {}).get('spec',{}).get('payload',{})
            if (not task or task['type']!='growth.account' or not connection
                    or str(payload.get('takeover_id'))!=self.connection_id
                    or str(connection['profile_id'])!=str(self.actor.profile['id'])):
                raise ValueError('native_connection_scope_changed')
            account=one(c,'SELECT * FROM lead.social_accounts WHERE id=%s',(payload.get('connection_account_id'),))
            if not account or str(account['profile_id'])!=str(self.actor.profile['id']):
                raise ValueError('native_connection_account_changed')
            return task,connection,account

    async def start(self):
        task,connection,account=await asyncio.to_thread(self.snapshot)
        if connection['control_mode']!='automatic':
            raise ValueError('connection_owner_control_active')
        if self.lease:
            entry=self.actor.native_runtime.pages.get(self.lease['target_id'])
            if not entry:
                self.lease=None
            else:
                if not entry.get('slot_held',True):
                    await asyncio.wait_for(self.actor.native_runtime.slots.acquire(),30)
                    entry['slot_held']=True
                runtime=(task['result'] or {}).get('runtime',{})
                self.lease.update(control_mode='automatic',challenge_version=connection['challenge_version'],
                    input_seq=connection['input_seq'],task_generation=task['input'].get('task_generation'),
                    claim={'attempt_token':runtime.get('attempt_token')})
                for role in self.lease.get('frame_roles',[]):
                    if role['task_id']==self.task_id and role['task_generation']==task['input'].get('task_generation'):
                        role['claim']=dict(self.lease['claim'])
                await self.advance_auth_flow()
                return await self.descriptor()
        url=connection['connection_data'].get('site_url')
        if not url or origin(url)!=origin(account['connection_origin']):
            raise ValueError('native_connection_origin_changed')
        runtime=(task['result'] or {}).get('runtime',{})
        scope={'task_id':self.task_id,'control_version':connection['control_version'],
            'task_generation':task['input'].get('task_generation'),'claim':{'attempt_token':runtime.get('attempt_token')},
            'connection_ref':self.connection_id,'challenge_version':connection['challenge_version'],
            'input_seq':connection['input_seq'],'control_mode':'automatic','account_id':str(account['id']),
            'binding_version':account['binding_version'],'origins':[account['connection_origin']],
            'authentication_endpoints':connection['connection_data'].get('authentication_endpoints',[])}
        await self.actor.forward_proxy.validate_url(url)
        self.lease=await self.actor.native_runtime.acquire_page(scope,url)
        self.page=self.actor.native_runtime.pages[self.lease['target_id']]['page']
        self.actor.page=self.page
        self.anonymous_self=await self.page.locator('a[rel~="me"]').all_text_contents()
        return await self.descriptor()

    async def observe_current(self):
        await self.advance_auth_flow()
        _,connection,account=await asyncio.to_thread(self.snapshot)
        if account.get('external_account_id'):
            with tx(self.actor.aid) as c:
                previous=one(c,"""SELECT connection_data->>'self_selector' selector,connection_data->>'self_url' self_url,
                        connection_data->>'self_link_selector' link_selector FROM lead.takeover_sessions
                    WHERE completed_account_id=%s AND connection_data ? 'self_selector'
                    ORDER BY ended_at DESC NULLS LAST LIMIT 1""",(account['id'],))
            if previous and previous.get('self_url'):
                self.self_entry={'url':previous['self_url'],'selector':previous.get('link_selector')}
                await self.page.goto(previous['self_url'],wait_until='domcontentloaded',timeout=30000)
                return await self.report_identity({'subject_selector':previous['selector']})
        return None

    async def person_only_challenge(self):
        from .native_challenges import person_challenge
        flow=(self.lease or {}).get('auth_flow') or {}
        if flow.get('popup_target') and not flow.get('completed'):
            return True
        from .native_frame_roles import needs_review
        return bool(await person_challenge(self.page)) or await needs_review(self)

    async def refresh_challenge(self,*,force=False,settling=False):
        from .connection_control import refresh_owner_challenge
        from .native_challenges import person_challenge
        async with self.challenge_lock:
            task,connection,_=await asyncio.to_thread(self.snapshot)
            result={key:connection[key] for key in ('state','control_mode','challenge_version','input_seq')}
            if (connection['control_mode']!='owner' or connection['state']!='active' or not self.lease
                    or not self.page or self.page.is_closed() or connection['input_inflight']):
                return result
            self.lease.update(control_mode='owner',challenge_version=connection['challenge_version'],input_seq=connection['input_seq'],
                task_generation=task['input'].get('task_generation'))
            await asyncio.to_thread(self.actor.native_runtime.current_scope,self.lease)
            try:
                page=await self.owner_page()
            except ValueError:
                if settling:
                    return result
                raise
            flow=self.lease.get('auth_flow') or {}
            target=flow.get('popup_target') if not flow.get('completed') else self.lease['target_id']
            before=self.actor.native_runtime.gate.documents.get(target) or self.lease.get('document_token')
            from playwright.async_api import Error as PlaywrightError
            try:
                async with asyncio.timeout(4):
                    facts=await person_challenge(page)
                    if not facts and page is not self.page and not flow.get('callback'):
                        import hashlib
                        import json
                        markers=await page.locator('form').evaluate_all('''forms=>forms.filter(form=>(form.getAttribute('method')||'GET').toUpperCase()==='POST'&&
                            form.querySelector('input[type=password],input[autocomplete="one-time-code"]')).map(form=>[
                                new URL(form.getAttribute('action')||location.href,form.baseURI).href,Array.from(form.querySelectorAll('input[type=hidden][name]')).filter(node=>
                                    /nonce|csrf|state|token/i.test(node.name)).map(node=>[node.name,node.value])])''')
                        facts={'kind':'identity_sign_in','fingerprint':hashlib.sha256(json.dumps([page.url,markers],sort_keys=True).encode()).hexdigest(),
                            'reason':'请在原身份站点窗口完成登录；本站保存的密码不会发送给该身份站点。'}
            except (PlaywrightError,TimeoutError):
                if settling:
                    return result
                raise ValueError('connection_challenge_navigation_pending') from None
            if before!=(self.actor.native_runtime.gate.documents.get(target) or self.lease.get('document_token')):
                if settling:
                    return result
                raise ValueError('connection_challenge_document_changed')
            marker={'kind':facts['kind'] if facts else None,'fingerprint':facts['fingerprint'] if facts else None,'document':before}
            reason=facts['reason'] if facts else '本人步骤已完成，请交回自动核验当前身份。'
            result=await asyncio.to_thread(refresh_owner_challenge,self.actor.aid,self.connection_id,
                connection['challenge_version'],connection['input_seq'],marker,reason=reason,force=force)
            if result['challenge_version']!=connection['challenge_version']:
                self.observation=self.proof_scope=self.self_entry=None
            self.lease.update(challenge_version=result['challenge_version'],input_seq=result['input_seq'])
            return result

    async def descriptor(self):
        scope=await self.actor.native_runtime.scope_view(self.lease['target_id'])
        task,connection,account=await asyncio.to_thread(self.snapshot)
        if connection['control_mode']!='automatic' or connection['challenge_version']!=self.lease['challenge_version']:
            raise ValueError('connection_control_changed')
        return {**scope,'cdp_url':self.actor.native_runtime.bridge.url,
                'headers':self.actor.native_runtime.bridge.headers}

    async def node_facts(self,node):
        scope=await self.descriptor()
        if node.get('target_id') not in scope['targets']:
            raise ValueError('connection_foreign_node')
        gate=self.actor.native_runtime.gate
        sessions=[sid for sid,info in gate.targets.items()
                  if info['targetId']==node['target_id'] and gate.owners.get(sid)==self.lease['target_id']]
        if len(sessions)!=1:
            raise ValueError('connection_node_owner_ambiguous')
        sid=sessions[0]
        resolved=await gate.send('DOM.resolveNode',{'backendNodeId':node['backend_node_id']},sid)
        object_id=resolved['object'].get('objectId')
        if not object_id:
            raise ValueError('connection_stale_node')
        try:
            result=await gate.send('Runtime.callFunctionOn',{'objectId':object_id,'functionDeclaration':FORM_FACTS,
                'returnByValue':True},sid)
            return result.get('result',{}).get('value')
        finally:
            await gate.send('Runtime.releaseObject',{'objectId':object_id},sid)

    async def authorize(self,args):
        await self.descriptor()
        from .native_frame_roles import readonly_node
        if readonly_node(self.lease,args.get('node') or {}):
            return {'allowed':False,'reason':'This approved public frame can only be read.'}
        name,values,node=args.get('name'),args.get('values') or {},args.get('node') or {}
        if name=='scroll':
            return {'allowed':True}
        if name=='navigate':
            return {'allowed':origin(values.get('url','')) in {origin(url) for url in self.lease['origins']}}
        if name=='click':
            from .native_auth_flow import popup_destination, reserve
            facts=await self.auth_control(node)
            if destination:=popup_destination(facts,self.page.url):
                await self.actor.forward_proxy.validate_url(destination)
                reserve(self.lease,destination)
                return {'allowed':True,'reason':'The observed sign-in popup stays on this original connection.'}
        facts=await self.node_facts(node)
        if not facts or facts['hasOtp'] or facts['autocomplete']=='one-time-code':
            return {'allowed':False,'reason':'This is a person-only challenge or an unsupported control.'}
        purpose=facts.get('formText','')+' '+facts.get('heading','')
        if (facts.get('passwordCount')!=1 or facts.get('newPassword')
                or not re.search(r'sign\s*in|log\s*in|login|authenticate|登录|登入',purpose,re.I)
                or re.search(r'delete|remove|destroy|reset|change\s*password|register|sign\s*up|删除|重置|修改密码|注册',purpose,re.I)):
            return {'allowed':False,'reason':'The current form is not a single-password sign-in form.'}
        if not facts['hasPassword'] or facts['method']!='POST' or origin(facts['action']) not in {origin(url) for url in self.lease['origins']}:
            return {'allowed':False,'reason':'This is not the current credential form.'}
        from .native_auth_flow import check_auth_form
        check_auth_form(facts['action'],facts['fields'],controls=facts['controls'])
        if name=='input':
            if not facts['name']:
                return {'allowed':False,'reason':'The credential form has no verifiable field name.'}
            expected='saved_password' if facts['type']=='password' else 'saved_username'
            if facts['tag']!='input' or values.get('text') not in (expected,'<secret>'+expected+'</secret>'):
                return {'allowed':False,'reason':'Use the approved saved credential placeholder for this observed field.'}
        elif name=='click':
            if facts['tag'] not in ('button','input') or facts['type'] not in ('submit','button'):
                return {'allowed':False}
            if facts['passwordEmpty']:
                return {'allowed':False,'reason':'The observed sign-in form has an empty password field. Fill that field before submitting.'}
            if facts['validatesOnSubmit'] and facts['requiredEmpty']:
                return {'allowed':False,'reason':'The observed sign-in form has an empty required field. Complete that field before submitting.'}
            if facts['validatesOnSubmit'] and facts['invalidFields']:
                return {'allowed':False,'reason':'The observed sign-in form is invalid. Correct its observed fields before submitting.'}
        else:
            return {'allowed':False}
        if name=='input':
            self.lease.setdefault('authentication_fields',{})[facts['name']]=expected
        endpoint=facts['action']
        if endpoint not in self.lease['authentication_endpoints']:
            self.lease['authentication_endpoints'].append(endpoint)
            with tx(self.actor.aid) as c:
                c.execute('''UPDATE lead.takeover_sessions SET connection_data=connection_data || %s
                    WHERE id=%s AND state='active' AND control_mode='automatic' AND challenge_version=%s''',
                    (j({'authentication_endpoints':self.lease['authentication_endpoints']}),self.connection_id,self.lease['challenge_version']))
        return {'allowed':True}

    async def auth_control(self,node):
        await self.descriptor()
        gate=self.actor.native_runtime.gate
        sessions=[sid for sid,info in gate.targets.items() if info['targetId']==node.get('target_id')
            and info['targetId']==self.lease['target_id'] and gate.owners.get(sid)==self.lease['target_id']]
        if len(sessions)!=1:
            return None
        sid=sessions[0]
        result=await gate.send('DOM.resolveNode',{'backendNodeId':node['backend_node_id']},sid)
        identifier=result['object'].get('objectId')
        if not identifier:
            return None
        try:
            value=await gate.send('Runtime.callFunctionOn',{'objectId':identifier,'functionDeclaration':'''function(){
                const node=this.closest('a,button,input');if(!node)return null;
                const r=node.getBoundingClientRect();if(!r.width||!r.height)return null;
                return {tag:node.tagName,text:node.innerText||node.value||'',href:node.href,target:node.target||'',
                    onclick:node.getAttribute('onclick')||'',form:!!node.form};}''','returnByValue':True},sid)
            return value.get('result',{}).get('value')
        finally:
            await gate.send('Runtime.releaseObject',{'objectId':identifier},sid)

    def current_observation(self):
        _,connection,account=self.snapshot()
        scope=(connection['challenge_version'],connection['input_seq'],str(self.actor.session),account['binding_version'])
        if self.proof_scope==scope:
            return self.observation
        self.observation=None
        return None

    def remember_observation(self,observed,expected):
        _,connection,account=self.snapshot()
        current=(connection['challenge_version'],connection['input_seq'],str(self.actor.session),account['binding_version'])
        if current!=expected:
            raise ValueError('connection_changed_during_identity_observation')
        self.proof_scope=current
        self.observation=observed
        return observed

    async def inspect_self(self,args):
        await self.descriptor()
        from .native_identity_errors import IdentityFactError
        from .native_identity_facts import inspect_current_identity, inspect_self_entry
        _,_,account=await asyncio.to_thread(self.snapshot)
        try:
            absent=all(args.get(name) in (None,'') for name in ('self_link_selector','acting_subject_selector','self_subject_prefix','self_subject_suffix'))
            if absent:
                facts=await inspect_current_identity(self.page,connection_origin=account['connection_origin'])
            else:
                facts=await inspect_self_entry(self.page,link_selector=args.get('self_link_selector'),
                    subject_selector=args.get('acting_subject_selector'),subject_prefix=args.get('self_subject_prefix',''),
                    subject_suffix=args.get('self_subject_suffix',''),connection_origin=account['connection_origin'])
                facts.update(acting_subject_selector=args['acting_subject_selector'],self_subject_prefix=args['self_subject_prefix'],
                    self_subject_suffix=args.get('self_subject_suffix',''))
        except IdentityFactError as error:
            if absent or error.code not in ('identity_value_missing','identity_subject_invalid','identity_current_self_unestablished','identity_current_subject_semantics_missing','identity_selector_invalid'):
                raise
            facts=await inspect_current_identity(self.page,connection_origin=account['connection_origin'])
        href=facts['self_url']
        self.self_entry={'url':href,'selector':facts['self_link_selector'],'acting_subject':facts['acting_subject'],
            'acting_subject_selector':facts['acting_subject_selector'],'self_subject_prefix':facts['self_subject_prefix'],
            'self_subject_suffix':facts['self_subject_suffix']}
        await self.page.goto(href,wait_until='domcontentloaded',timeout=30000)
        await self.descriptor()
        return {'state':'self_surface_opened','url':self.page.url}

    async def report_identity(self,args):
        await self.descriptor()
        _,connection,account=await asyncio.to_thread(self.snapshot)
        expected=(connection['challenge_version'],connection['input_seq'],str(self.actor.session),account['binding_version'])
        selector=args.get('subject_selector','')
        if not isinstance(selector,str) or not 1<=len(selector)<=300:
            raise ValueError('self_subject_missing')
        if not self.self_entry or self.page.url!=self.self_entry['url']:
            raise ValueError('self_surface_not_opened')
        from .native_identity_facts import inspect_private_subject
        observed=await inspect_private_subject(self.page,subject_selector=selector,
            value_prefix=args.get('value_prefix',''),value_suffix=args.get('value_suffix',''))
        # Cached/SW content may retain the previous subject. The private probe
        # keeps this cookie jar, exit and original ConnectionRef, but obtains a
        # fresh network response before accepting a new binding.
        import copy
        scope=copy.deepcopy(self.lease)
        scope.pop('target_id',None)
        fresh_scope=await self.actor.native_runtime.acquire_page(scope,self.self_entry['url'],
            internal_parent=self.lease['target_id'],private_identity=True)
        try:
            fresh=self.actor.native_runtime.pages[fresh_scope['target_id']]['page']
            if fresh.url!=self.self_entry['url']:
                raise ValueError('self_surface_not_authenticated')
            current=await inspect_private_subject(fresh,subject_selector=selector,
                value_prefix=observed['value_prefix'],value_suffix=observed['value_suffix'])
            if current!=observed:
                raise ValueError('private_self_subject_changed')
        finally:
            await self.actor.native_runtime.release_page(fresh_scope['target_id'])
        await self.descriptor()
        subject=observed['subject']
        if self.self_entry.get('acting_subject') and subject!=self.self_entry['acting_subject']:
            raise ValueError('private_self_and_acting_subject_disagree')
        if not self.lease.get('authentication_dispatched') and subject!=account.get('external_account_id'):
            raise ValueError('self_surface_not_authenticated')
        href=self.self_entry['url']
        kind='handle' if re.search(r'user\s*name|username|handle|用户名',observed['evidence']['label'],re.I) else 'stable'
        self.observation={'status':'authenticated','operating_subject':{'namespace':account['platform'],'external_id':subject,'id_kind':kind},
            'evidence_refs':['native-self:'+self.connection_id+':'+str(self.lease['challenge_version'])]}
        with tx(self.actor.aid) as c:
            c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s',
                (j({'self_url':href,'self_link_selector':self.self_entry['selector'],'self_selector':selector,'self_subject':subject,
                    'self_value_prefix':observed['value_prefix'],'self_value_suffix':observed['value_suffix'],
                    **{key:self.self_entry[source] for key,source in (
                        ('acting_subject_selector','acting_subject_selector'),('acting_subject_prefix','self_subject_prefix'),
                        ('acting_subject_suffix','self_subject_suffix')) if source in self.self_entry}}),self.connection_id))
        return self.remember_observation(self.observation,expected)

    async def command(self,operation,args):
        if operation=='read_dependencies':
            from .native_frame_roles import candidates
            return await candidates(self)
        if operation=='read_dependency_decision':
            from .native_frame_roles import decide
            return await decide(self,args)
        if operation=='owner_status':
            from .native_frame_roles import candidates, needs_review
            _,connection,_=await asyncio.to_thread(self.snapshot)
            if connection['control_mode']=='owner' and 'read_dependencies' in connection['connection_data']:
                await candidates(self)
            result=await self.refresh_challenge()
            if connection['control_mode']=='owner' and 'read_dependencies' not in connection['connection_data'] and await needs_review(self):
                await candidates(self)
            return result
        if operation=='preflight':
            await self.descriptor()
            return {'owner_required':await self.person_only_challenge()}
        if operation=='scope':
            return {key:value for key,value in (await self.descriptor()).items() if key not in ('cdp_url','headers')}
        if operation=='tool_authorize':
            return await self.authorize(args)
        if operation=='inspect_self':
            return await self.inspect_self(args)
        if operation=='report_connection':
            return await self.report_identity(args)
        raise ValueError('native_connection_operation_unsupported')

    async def handoff(self,reason):
        if not self.lease:
            return
        page=await self.owner_page()
        endpoints=await page.locator('form').evaluate_all("""forms=>forms.filter(form=>(form.getAttribute('method')||'GET').toUpperCase()==='POST' &&
            form.querySelector('input[type="password"],input[autocomplete="one-time-code"]')).map(form=>new URL(form.getAttribute('action')||location.href,form.baseURI).href)""")
        for endpoint in endpoints:
            if (origin(endpoint) in {origin(url) for url in self.lease['origins']} or page is not self.page
                    and origin(endpoint)==origin(page.url)) and endpoint not in self.lease['authentication_endpoints']:
                self.lease['authentication_endpoints'].append(endpoint)
        _,connection,_=await asyncio.to_thread(self.snapshot)
        if self.lease.get('auth_flow'):
            from .growth.contracts import utcnow
            self.lease['auth_flow']['expires']=time.monotonic()+max(0,(connection['expires_at']-utcnow()).total_seconds())
        with tx(self.actor.aid) as c:
            c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s AND challenge_version=%s',
                (j({'authentication_endpoints':self.lease['authentication_endpoints']}),self.connection_id,connection['challenge_version']))
        current=transition(self.actor.aid,self.connection_id,connection['challenge_version'],'owner',reason=reason)
        # Keep this original page and its current challenge for the owner; release
        # the SDK execution slot without transferring cookies or page ownership.
        self.lease.update(control_mode='owner',challenge_version=current['challenge_version'],input_seq=current['input_seq'])
        await self.refresh_challenge()
        await self.actor.native_runtime.yield_page(self.lease['target_id'])
        return current

    async def reconcile(self):
        from .growth.contracts import utcnow
        task,connection,_=await asyncio.to_thread(self.snapshot)
        if connection['state']!='active' or connection['expires_at']<=utcnow():
            with tx(self.actor.aid) as c:
                c.execute("UPDATE lead.takeover_sessions SET state='expired',ended_at=coalesce(ended_at,now()),encrypted_login=NULL WHERE id=%s AND expires_at<=now() AND state='active'",(self.connection_id,))
            await self.close()
            return False
        if self.lease and connection['control_mode']=='owner':
            self.lease.update(control_mode='owner',challenge_version=connection['challenge_version'],
                input_seq=connection['input_seq'],task_generation=task['input'].get('task_generation'))
            await self.actor.native_runtime.yield_page(self.lease['target_id'])
            await self.refresh_challenge()
        return True

    async def close(self):
        if self.lease and (self.lease.get('auth_flow') or {}).get('popup_target') and not self.lease['auth_flow'].get('completed'):
            try:
                popup=await self.owner_page()
                if popup is not self.page:
                    await popup.close()
            except ValueError:
                pass
        if self.lease:
            lease,self.lease=self.lease,None
            if self.runtime:
                await self.runtime.release_page(lease['target_id'])
        self.page=None

    async def credentials(self):
        await self.descriptor()
        _,connection,_=await asyncio.to_thread(self.snapshot)
        return unseal(self.actor.aid,self.connection_id,connection['encrypted_login'])
