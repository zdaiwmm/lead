"""Task-owned native research pages in the manager's existing instance."""
import asyncio
from urllib.parse import urljoin

from .browser_execution import origin
from .db import one, tx
from .research import ResearchPage, public_url


class NativeResearchPage(ResearchPage):
    def __init__(self, actor, run_id):
        super().__init__(actor, run_id)
        self.lease = None

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
            spec = task['input'].get('spec',{})
            payload = spec.get('payload',{})
            if payload.get('access_mode') == 'account':
                account_id = payload.get('account_id')
                if str(spec.get('account_id')) != str(account_id):
                    raise ValueError('research_account_scope_missing')
                intent = one(c,'SELECT desired,revision FROM lead.account_operating_intents WHERE account_id=%s',(account_id,))
                if not intent or intent['desired'] != 'running':
                    raise ValueError('research_account_paused')
                scope.update(account_id=account_id,binding_version=task['input'].get('binding_version'),
                             intent_revision=intent['revision'])
        await self.actor.forward_proxy.validate_url(url)
        self.lease = await self.actor.native_runtime.acquire_page(scope,url)
        self.page = self.actor.native_runtime.pages[self.lease['target_id']]['page']
        self.context = self.actor.context
        return await self.descriptor()

    async def ensure(self):
        if not self.lease:
            raise ValueError('native_page_not_started')
        await self.descriptor()

    async def descriptor(self):
        await asyncio.to_thread(self.gate)
        scope = await self.actor.native_runtime.scope_view(self.lease['target_id'])
        bridge = self.actor.native_runtime.bridge
        return {**scope,'cdp_url':bridge.url,'headers':bridge.headers}

    async def command(self, operation, args, command_id):
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
            return {'allowed':origin(url) in {origin(item) for item in scope['origins']}}
        node = args.get('node') or {}
        if name != 'click' or node.get('target_id') != self.lease['target_id']:
            return {'allowed':False,'reason':'This research task only reads the assigned page.'}
        # Read current native node facts ourselves; SDK/model-supplied labels
        # cannot grant authority. No arbitrary expression comes from the caller.
        session = await self.context.new_cdp_session(self.page)
        try:
            info = await session.send('DOM.describeNode',{'backendNodeId':node['backend_node_id']})
            actual = info['node']
            attrs = actual.get('attributes',[])
            attrs = dict(zip(attrs[::2],attrs[1::2]))
            if actual['nodeName'].lower() != 'a' or 'download' in attrs or attrs.get('target') == '_blank':
                return {'allowed':False}
            url = public_url(urljoin(self.page.url,attrs.get('href','')))
            await self.actor.forward_proxy.validate_url(url)
            return {'allowed':origin(url) in {origin(item) for item in scope['origins']}}
        finally:
            await session.detach()

    async def close(self):
        if self.lease:
            await self.actor.native_runtime.release_page(self.lease['target_id'])
            self.lease = None
        self.page = self.context = None
