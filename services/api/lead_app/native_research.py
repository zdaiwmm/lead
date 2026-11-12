"""Task-owned native research pages in the manager's existing instance."""
import asyncio
import copy
from urllib.parse import urljoin

from .browser_execution import origin
from .db import one, tx
from .research import (
    ResearchPage,
    account_read_origins,
    public_url,
    task_research_scope,
)


class NativeAccountIdentityError(ValueError):
    def __init__(self,condition):
        self.condition=condition
        super().__init__(condition)


class NativeResearchPage(ResearchPage):
    def __init__(self, actor, run_id):
        super().__init__(actor, run_id)
        self.lease = None
        self.command_lock = asyncio.Lock()
        self.identity_lock=asyncio.Lock()
        self.identity_required=True
        self.identity_failure=None

    async def start(self, url):
        run = await asyncio.to_thread(self.gate)
        if self.lease:
            return await self.descriptor()
        if not self.actor.native_runtime or not run.get('task_id'):
            raise ValueError('native_research_requires_current_task')
        with tx(self.actor.aid) as c:
            task = one(c,'SELECT * FROM lead.tasks WHERE id=%s',(run['task_id'],))
            if not task or task['state'] != 'running':
                raise ValueError('native_research_requires_current_task')
            runtime = (task['result'] or {}).get('runtime',{})
            scope = {'task_id':str(task['id']), 'control_version':run['control_version'],
                     'task_generation':task['input'].get('task_generation'),
                     'claim':{'attempt_token':runtime.get('attempt_token')}, 'origins':[public_url(url)]}
            context=task_research_scope(c,task)
            payload=context['payload']
            recovery=context['recovery_scope']
            if recovery and url != recovery['url']:
                raise ValueError('recovery_read_url_changed')
            if payload.get('access_mode') == 'account':
                account_id = payload.get('account_id')
                if str(context['account_id']) != str(account_id):
                    raise ValueError('research_account_scope_missing')
                intent = one(c,'SELECT desired,revision FROM lead.account_operating_intents WHERE account_id=%s',(account_id,))
                if not intent or intent['desired'] != 'running':
                    raise ValueError('research_account_paused')
                origins=account_read_origins(c,account_id,context['binding_version'])
                if origin(url) not in {origin(value) for value in origins}:
                    raise ValueError('research_url_outside_account')
                scope.update(account_id=account_id,binding_version=context['binding_version'],
                             intent_revision=intent['revision'],origins=origins)
        await self.actor.forward_proxy.validate_url(url)
        private=None
        if scope.get('account_id'):
            with tx(self.actor.aid) as c:
                private=one(c,"""SELECT connection_data FROM lead.takeover_sessions WHERE completed_account_id=%s
                    AND connection_data ? 'self_url' AND connection_data ? 'self_selector'
                    ORDER BY ended_at DESC NULLS LAST LIMIT 1""",(scope['account_id'],))
            if not private or origin(private['connection_data']['self_url']) not in {origin(value) for value in scope['origins']}:
                raise NativeAccountIdentityError('account_identity_unverified')
        initial=private['connection_data']['self_url'] if private else url
        self.lease = await self.actor.native_runtime.acquire_page(scope,initial,private_identity=bool(private))
        self.page = self.actor.native_runtime.pages[self.lease['target_id']]['page']
        self.context = self.actor.context
        if private:
            await self.verify_account_identity(fresh_page=self.page)
            await self.actor.native_runtime.clear_identity_mode(self.lease['target_id'])
            if url!=initial:
                await self.page.goto(url,wait_until='domcontentloaded',timeout=45000)
        return await self.descriptor()

    async def verify_account_identity(self,*,fresh_page=None):
        if not self.lease or not self.lease.get('account_id'):
            return
        async with self.identity_lock:
            await self.actor.native_runtime.scope_view(self.lease['target_id'])
            with tx(self.actor.aid) as c:
                account=one(c,"SELECT * FROM lead.social_accounts WHERE id=%s AND state='connected' AND binding_version=%s",
                    (self.lease['account_id'],self.lease['binding_version']))
                private=one(c,"""SELECT connection_data FROM lead.takeover_sessions WHERE completed_account_id=%s
                    AND connection_data ? 'self_url' AND connection_data ? 'self_selector'
                    ORDER BY ended_at DESC NULLS LAST LIMIT 1""",(self.lease['account_id'],))
            if not account or not private or private['connection_data'].get('self_subject')!=account['external_account_id']:
                raise NativeAccountIdentityError('account_identity_unverified')
            facts=private['connection_data']
            if origin(facts['self_url']) not in {origin(value) for value in self.lease['origins']}:
                raise NativeAccountIdentityError('account_identity_unverified')
            scope=None
            page=fresh_page
            if page is None:
                copied=copy.deepcopy(self.lease)
                copied.pop('target_id',None)
                scope=await self.actor.native_runtime.acquire_page(copied,facts['self_url'],internal_parent=self.lease['target_id'],private_identity=True)
                page=self.actor.native_runtime.pages[scope['target_id']]['page']
            try:
                if page.url!=facts['self_url']:
                    raise NativeAccountIdentityError('account_identity_unverified')
                from .native_identity_facts import inspect_private_subject
                try:
                    observed=await inspect_private_subject(page,subject_selector=facts['self_selector'],
                        value_prefix=facts.get('self_value_prefix',''),value_suffix=facts.get('self_value_suffix',''))
                except ValueError as error:
                    raise NativeAccountIdentityError('account_identity_unverified') from error
                actual=observed['subject']
                if actual!=account['external_account_id']:
                    with tx(self.actor.aid) as c:
                        c.execute("UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s AND binding_version=%s AND state='connected'",
                            (account['id'],account['binding_version']))
                    raise NativeAccountIdentityError('account_subject_changed')
                await self.actor.native_runtime.scope_view(self.lease['target_id'])
            finally:
                if scope:
                    await self.actor.native_runtime.release_page(scope['target_id'])

    async def ensure(self):
        if not self.lease:
            raise ValueError('native_page_not_started')
        await self.descriptor()

    async def descriptor(self):
        await asyncio.to_thread(self.gate)
        await self.verify_account_identity()
        scope = await self.actor.native_runtime.scope_view(self.lease['target_id'])
        bridge = self.actor.native_runtime.bridge
        return {**scope,'cdp_url':bridge.url,'headers':bridge.headers}

    async def observation_scope(self):
        if self.identity_required:
            await self.verify_account_identity()
        scope = await self.actor.native_runtime.scope_view(self.lease['target_id'])
        allowed = {origin(url) for url in scope['origins']}
        session = await self.context.new_cdp_session(self.page)
        try:
            tree = await session.send('Page.getFrameTree')
            def inspect(node):
                frame = node['frame']
                if origin(frame['url']) not in allowed:
                    raise ValueError('observation_frame_out_of_scope')
                facts = [(frame['id'], frame.get('loaderId'), frame['url'])]
                for child in node.get('childFrames', []):
                    facts.extend(inspect(child))
                return facts
            facts = inspect(tree['frameTree'])
            # Include OOPIFs omitted by this renderer's Page frame tree. A frame
            # detached or navigated during capture changes the final facts.
            for frame in self.page.frames:
                if origin(frame.url) not in allowed:
                    raise ValueError('observation_frame_out_of_scope')
            return scope, facts, [(frame, frame.url) for frame in self.page.frames]
        finally:
            await session.detach()

    async def command(self, operation, args, command_id):
        try:
            if self.identity_failure:
                raise NativeAccountIdentityError(self.identity_failure)
            return await self._command(operation,args,command_id)
        except NativeAccountIdentityError as error:
            self.identity_failure=error.condition
            await self.close()
            return {'error':'ACCOUNT_SUBJECT_CHANGED' if error.condition=='account_subject_changed' else 'ACCOUNT_IDENTITY_UNVERIFIED',
                'condition':error.condition,'reason':'wrong_subject' if error.condition=='account_subject_changed' else 'identity_unverified'}

    async def _command(self, operation, args, command_id):
        # Scope metadata is consumed by the pinned worker, never by the model.
        if operation == 'open':
            if not self.lease:
                await self.start(args['url'])
            elif public_url(args['url']) != self.page.url:
                raise ValueError('native_page_requires_explicit_navigation')
            return await super().command('read',{},command_id)
        if operation == 'scope':
            descriptor = await self.descriptor()
            return {key:value for key,value in descriptor.items() if key not in ('cdp_url','headers')}
        if operation == 'tool_authorize':
            await self.descriptor()
            return await self.authorize(args)
        if operation == 'read':
            # Existing recorder persists source text and same-run evidence.
            return await super().command(operation,args,command_id)
        raise ValueError('native_research_uses_native_tools')

    async def authorize(self, args):
        scope = await self.actor.native_runtime.scope_view(self.lease['target_id'])
        name, values = args.get('name'), args.get('values',{})
        if name == 'scroll':
            return {'allowed':True}
        if name == 'navigate':
            url = public_url(values.get('url',''))
            await self.actor.forward_proxy.validate_url(url)
            return {'allowed':origin(url) in {origin(item) for item in scope['origins']},'url':url}
        node = args.get('node') or {}
        if name != 'click' or node.get('target_id') not in scope['targets']:
            return {'allowed':False,'reason':'This research task only reads the assigned page.'}
        # Read current native node facts ourselves; SDK/model-supplied labels
        # cannot grant authority. No arbitrary expression comes from the caller.
        gate=self.actor.native_runtime.gate
        sessions=[session for session,info in gate.targets.items()
                  if info['targetId']==node['target_id'] and gate.owners.get(session)==self.lease['target_id']]
        if len(sessions)!=1:
            return {'allowed':False}
        try:
            info = await gate.send('DOM.describeNode',{'backendNodeId':node['backend_node_id']},sessions[0])
            actual = info['node']
            attrs = actual.get('attributes',[])
            attrs = dict(zip(attrs[::2],attrs[1::2]))
            if actual['nodeName'].lower() != 'a' or 'download' in attrs or attrs.get('target') == '_blank':
                return {'allowed':False}
            url = public_url(urljoin(gate.targets[sessions[0]]['url'],attrs.get('href','')))
            await self.actor.forward_proxy.validate_url(url)
            return {'allowed':origin(url) in {origin(item) for item in scope['origins']},'url':url}
        except (KeyError,ValueError):
            return {'allowed':False}

    async def close(self):
        if self.lease:
            await self.actor.native_runtime.release_page(self.lease['target_id'])
            self.lease = None
        self.page = self.context = None
