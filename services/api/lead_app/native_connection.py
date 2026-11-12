"""Original ConnectionRef controller for a fixed native login page.

The SDK chooses observed controls. Only this controller authorizes credential
placeholders against a real password form; authentication never borrows a
publication ActionRef. Identity evidence is read from the current self surface.
"""
import asyncio
from urllib.parse import urljoin

from .browser_execution import origin
from .connection_control import transition, unseal
from .db import j, one, tx

FORM_FACTS = '''function(){const form=this.form || this.closest('form'); if(!form)return null;
return {tag:this.tagName.toLowerCase(),type:(this.type||this.getAttribute('type')||'text').toLowerCase(),
name:this.getAttribute('name')||'',autocomplete:this.getAttribute('autocomplete')||'',method:form.method.toUpperCase(),action:form.action,
hasPassword:!!form.querySelector('input[type="password"]'),hasOtp:!!form.querySelector('input[autocomplete="one-time-code"]')};}'''


class NativeConnection:
    def __init__(self,actor,task_id,connection_id):
        self.actor=actor
        self.task_id=str(task_id)
        self.connection_id=str(connection_id)
        self.lease=None
        self.page=None
        self.observation=None
        self.proof_scope=None
        self.anonymous_self=[]
        self.operation_lock=asyncio.Lock()

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
        _,connection,account=await asyncio.to_thread(self.snapshot)
        if account['platform']!='web':
            return await self.report_identity({'self_link_selector':'body'})
        if account.get('external_account_id'):
            with tx(self.actor.aid) as c:
                previous=one(c,"""SELECT connection_data->>'self_selector' selector FROM lead.takeover_sessions
                    WHERE completed_account_id=%s AND connection_data ? 'self_selector'
                    ORDER BY ended_at DESC NULLS LAST LIMIT 1""",(account['id'],))
            if previous:
                return await self.report_identity({'self_link_selector':previous['selector']})
        return None

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
        name,values,node=args.get('name'),args.get('values') or {},args.get('node') or {}
        if name=='scroll':
            return {'allowed':True}
        if name=='navigate':
            return {'allowed':origin(values.get('url','')) in {origin(url) for url in self.lease['origins']}}
        facts=await self.node_facts(node)
        if not facts or facts['hasOtp'] or facts['autocomplete']=='one-time-code':
            return {'allowed':False,'reason':'This is a person-only challenge or an unsupported control.'}
        if not facts['hasPassword'] or facts['method']!='POST' or origin(facts['action']) not in {origin(url) for url in self.lease['origins']}:
            return {'allowed':False,'reason':'This is not the current credential form.'}
        if name=='input':
            if not facts['name']:
                return {'allowed':False,'reason':'The credential form has no verifiable field name.'}
            expected='saved_password' if facts['type']=='password' else 'saved_username'
            if facts['tag']!='input' or values.get('text') not in (expected,'<secret>'+expected+'</secret>'):
                return {'allowed':False,'reason':'Use the approved saved credential placeholder for this observed field.'}
        elif name=='click':
            if facts['tag'] not in ('button','input') or facts['type'] not in ('submit','button'):
                return {'allowed':False}
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

    async def report_identity(self,args):
        await self.descriptor()
        _,connection,account=await asyncio.to_thread(self.snapshot)
        expected=(connection['challenge_version'],connection['input_seq'],str(self.actor.session),account['binding_version'])
        if account['platform']!='web':
            from .account_connection import observe_identity
            observed=await observe_identity(self.page,account['platform'],self.actor.session)
            return self.remember_observation(observed.model_dump(mode='json'),expected)
        selector=args.get('self_link_selector','')
        if not isinstance(selector,str) or not 1<=len(selector)<=300:
            raise ValueError('self_surface_missing')
        link=self.page.locator(selector)
        if await link.count()!=1 or not await link.is_visible():
            raise ValueError('self_surface_ambiguous')
        if 'me' not in (await link.get_attribute('rel') or '').split():
            raise ValueError('not_current_self_surface')
        href=urljoin(self.page.url,await link.get_attribute('href') or '')
        subject=(await link.inner_text()).strip()
        if origin(href)!=origin(account['connection_origin']) or not subject or len(subject)>200:
            raise ValueError('self_surface_outside_connection')
        # A public profile link present before credential submission is not self
        # evidence. Generic sites lacking an authenticated self surface stop.
        if (subject in self.anonymous_self or not self.lease.get('authentication_dispatched')) and subject!=account.get('external_account_id'):
            raise ValueError('self_surface_not_authenticated')
        self.observation={'status':'authenticated','operating_subject':{'namespace':'web','external_id':subject,'id_kind':'stable'},
            'evidence_refs':['native-self:'+self.connection_id+':'+str(self.lease['challenge_version'])]}
        with tx(self.actor.aid) as c:
            c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s',
                (j({'self_url':href,'self_selector':selector,'self_subject':subject}),self.connection_id))
        return self.remember_observation(self.observation,expected)

    async def command(self,operation,args):
        if operation=='scope':
            return {key:value for key,value in (await self.descriptor()).items() if key not in ('cdp_url','headers')}
        if operation=='tool_authorize':
            return await self.authorize(args)
        if operation=='report_connection':
            return await self.report_identity(args)
        raise ValueError('native_connection_operation_unsupported')

    async def handoff(self,reason):
        if not self.lease:
            return
        _,connection,_=await asyncio.to_thread(self.snapshot)
        current=transition(self.actor.aid,self.connection_id,connection['challenge_version'],'owner',reason=reason)
        # Keep this original page and its current challenge for the owner; release
        # the SDK execution slot without transferring cookies or page ownership.
        self.lease.update(control_mode='owner',challenge_version=current['challenge_version'],input_seq=current['input_seq'])
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
        return True

    async def close(self):
        if self.lease:
            await self.actor.native_runtime.release_page(self.lease['target_id'])
            self.lease=None
        self.page=None

    async def credentials(self):
        await self.descriptor()
        _,connection,_=await asyncio.to_thread(self.snapshot)
        return unseal(self.actor.aid,self.connection_id,connection['encrypted_login'])
