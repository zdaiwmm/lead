"""Disposable C22 API: real local Codex, original Tasks/Actions, synthetic sites.

Never mount this fixture in the product API. Every administrative route requires
the random, private fixture token. It has no real accounts or model API key.
"""
import asyncio
import hmac
import json
import os
import socket
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from lead_app import browser, domain, local_executor
from lead_app.config import STATE, env
from lead_app.db import all_rows, tx
from lead_app.growth.runtime import advance, finish, growth_execute
from lead_app.local_executor_api import router
from native_eval_a_adapter import create_a
from native_eval_b_adapter import create_b

from scripts.evaluate_native_browser import (
    AcceptanceStop,
    NativeCase,
    private_write,
    sha,
    synthetic_site_transport,
)

runtime={}


class CodexCase(NativeCase):
    """Original six business tasks, without the retired provider budget harness."""
    def submit(self,kind,purpose,payload,*,target=None):
        key=json.dumps([kind,purpose,payload,target],sort_keys=True,default=str)
        refs=self.report.setdefault('original_refs',{})
        if key not in refs:
            refs[key]=super().submit(kind,purpose,payload,target=target)
            self.persist()
        return refs[key]

    def content(self,text,*,operation='publish',parent=None):
        key=json.dumps([text,operation,parent])
        refs=self.report.setdefault('original_content_refs',{})
        if key not in refs:
            refs[key]=super().content(text,operation=operation,parent=parent)
            self.persist()
        return refs[key]

    async def perform(self,name,ref,*,expected='completed',seed=False,now=None):
        previous=(self.report.get('mechanical_seed') if seed else next(
            (row for row in reversed(self.report['stages']) if row['stage']==name),None))
        if previous and previous.get('outcome')==expected and previous.get('result'):
            if previous['task_id']!=str(ref['task_id']):
                raise AcceptanceStop('original_stage_identity_changed')
            return previous['result']
        before=self.site.snapshot()
        stage={'stage':name,'task_id':str(ref['task_id']),'task_generation':ref['task_generation'],
            'mechanical':seed,'started_at_monotonic':time.monotonic()}
        self.report['active_stage']=stage
        self.persist()
        admitted=advance(ref,now=now)
        if admitted['state']!='ready':
            raise AcceptanceStop('task_not_admitted:'+name+':'+admitted['state'])
        stage['claim']=admitted['claim']
        self.persist()
        while True:
            result=await growth_execute({'agent_id':self.aid,'claim':admitted['claim']})
            if not result.get('executor_pending'):
                break
            await asyncio.sleep(1)
        while True:
            with tx(self.aid) as c:
                requests=all_rows(c,'SELECT id,state,thread_id,turn_id,usage FROM lead.local_executor_requests WHERE task_id=%s ORDER BY created_at',(ref['task_id'],))
            if seed or not requests or all(row['state'] in ('completed','failed','interrupted','execution_unknown','canceled') for row in requests):
                break
            # A verified business fact may precede Codex's final message. Do not
            # close its daemon or finish the original Task prematurely.
            await asyncio.sleep(1)
        finish(admitted['claim'],result)
        after=self.site.snapshot()
        stage.update(outcome=result['outcome'],result=result,failure_code=result.get('failure_code'),terminal=self.row(ref)['state'],
            seconds=time.monotonic()-stage['started_at_monotonic'],requests=requests,
            executor_mode='codex' if requests and all(row.get('thread_id') for row in requests) else 'mechanical-tools',
            source_post_delta=sum(row['method']=='POST' for row in after['requests'])-sum(row['method']=='POST' for row in before['requests']))
        if seed:
            self.report['mechanical_seed']=stage
        else:
            self.report['stages'].append(stage)
        self.report.pop('active_stage',None)
        self.persist()
        if result['outcome']!=expected:
            raise AcceptanceStop('original_task_failed:'+name+':'+result['outcome']+':'+str(result.get('failure_code')))
        if stage['source_post_delta']!=(1 if name in ('login','publish','reply','unknown_seed') else 0):
            raise AcceptanceStop('unexpected_source_post_count:'+name)
        if not seed and (not requests or any(row['state']!='completed' for row in requests)):
            raise AcceptanceStop('native_codex_request_not_completed:'+name)
        return result


    async def run(self, evidence):
        from lead_app import browser
        from lead_app.account_connection import begin
        from lead_app.connection_control import LoginInput
        from lead_app.db import one, tx
        from lead_app.growth.contracts import utcnow
        try:
            if set(evidence) != {'publish', 'reply', 'unknown'} or any(not isinstance(value, str) or not value for value in evidence.values()):
                raise AcceptanceStop('fixed_content_evidence_required')
            self.report['content_evidence'] = {key: sha(value.encode()) for key, value in evidence.items()}
            if self.pending is None:
                self.pending = await begin(self.aid, 'web', site_url=self.site.entry_url,
                    login=LoginInput(username=self.site.username, password=self.site.password))
            with tx(self.aid) as c:
                task = one(c, "SELECT * FROM lead.tasks WHERE input#>>'{spec,payload,takeover_id}'=%s", (str(self.pending['takeover_id']),))
            ref = {'agent_id': self.aid, 'task_id': task['id'], 'task_generation': task['input']['task_generation']}
            await self.perform('login', ref)
            with tx(self.aid) as c:
                account = one(c, 'SELECT * FROM lead.social_accounts WHERE id=%s', (self.pending['account_id'],))
            if account['external_account_id'] != self.site.expected_subject or account['state'] != 'connected':
                raise AcceptanceStop('wrong_or_unproven_private_subject')
            actor = await browser.gateway.get(self.aid, account['profile_id'])
            controller=actor.connection_pages.get(str(self.pending['takeover_id']))
            if controller:
                await controller.reconcile()
            work = self.submit('account', 'discover', {'target_urls': [self.site.target_url], 'desired_operation': 'publish'})
            learned = await self.perform('discover_publish', work)
            targets = learned['output'].get('targets', [])
            if len(targets) != 1 or targets[0].get('operation') != 'publish':
                raise AcceptanceStop('publish_method_not_learned')
            self.target_id = targets[0]['target_id']
            work = self.content(evidence['publish'])
            await self.perform('publish', work)
            self.report['stages'][-1]['receipt'] = await self.confirmed(work, evidence['publish'])
            parent = self.report['stages'][-1]['receipt']['url']
            work = self.submit('account', 'discover', {'target_urls': [parent], 'desired_operation': 'reply'})
            learned = await self.perform('discover_reply', work)
            if len(learned['output'].get('targets', [])) != 1 or learned['output']['targets'][0].get('operation') != 'reply':
                raise AcceptanceStop('reply_method_not_learned')
            work = self.content(evidence['reply'], operation='reply', parent=parent)
            await self.perform('reply', work)
            self.report['stages'][-1]['receipt'] = await self.confirmed(work, evidence['reply'], parent)
            unknown = await self.seed(evidence['unknown'])
            posts = len(self.site.snapshot()['posts'])
            # Only the fixture clock is accelerated past the original bounded
            # read wait. No task/attempt/deadline/budget field is rewritten.
            self.report['reconcile_clock_advance_seconds'] = 360
            await self.perform('reconcile', unknown, now=utcnow()+timedelta(minutes=6))
            self.report['stages'][-1]['receipt'] = await self.confirmed(unknown, evidence['unknown'])
            if len(self.site.snapshot()['posts']) != posts:
                raise AcceptanceStop('unknown_action_resubmitted')
            self.report['completed'] = True
        except Exception as error:
            self.report['completed'] = False
            self.report['first_failure'] = str(error) if isinstance(error, AcceptanceStop) else type(error).__name__
            raise
        finally:
            self.persist()
        return self.report


@asynccontextmanager
async def lifespan(app):
    if env().get('LEAD_DEPLOYMENT_MODE')=='production' or not env().get('LEAD_DB_NAME','').startswith('codex_c22_'):
        raise ValueError('isolated_codex_c22_database_required')
    if env().get('OPENAI_API_KEY') or env().get('DEEPSEEK_API_KEY'):
        raise ValueError('codex_c22_must_have_no_model_api_key')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    endpoint='http://127.0.0.1:'+str(port)
    os.environ['LEAD_BROWSER_TRANSPORTS']=json.dumps({'shared':endpoint})
    server=uvicorn.Server(uvicorn.Config(browser.app,host='127.0.0.1',port=port,log_level='error'))
    serving=asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(.05)
    aid=domain.create_agent(runtime_engine='growth')
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(aid,))
    sites={'a':create_a(host='codex-alpha.test'),'b':create_b(host='codex-beta.test')}
    transports=ExitStack()
    transports.enter_context(synthetic_site_transport(list(sites.values())))
    runtime.update(agent_id=str(aid),sites=sites,endpoint=endpoint,cases={},case_objects={},tasks={},failure=None)
    private_write(STATE/'c22-runtime.json',{'agent_id':aid,'gateway_endpoint':endpoint,'source_sha':os.environ['LEAD_C22_SOURCE_SHA']})
    try:
        yield
    finally:
        for task in runtime['tasks'].values():
            if not task.done():
                task.cancel()
        await browser.gateway.close(aid)
        server.should_exit=True
        await serving
        for site in sites.values():
            site.close()
        transports.close()


app=FastAPI(lifespan=lifespan)
app.include_router(router)


@app.middleware('http')
async def fixture_auth(request:Request,call_next):
    if request.url.path.startswith('/fixture/') and not hmac.compare_digest(
            request.headers.get('x-fixture-token',''),os.environ['LEAD_FIXTURE_TOKEN']):
        return JSONResponse({'detail':'fixture_unavailable'},status_code=401)
    return await call_next(request)


@app.get('/fixture/health')
def health():
    return {'ready':True,'agent_id':runtime['agent_id'],'source_sha':os.environ['LEAD_C22_SOURCE_SHA']}


@app.post('/fixture/pairing')
def pairing():
    return local_executor.create_pairing(runtime['agent_id'])


@app.post('/fixture/c22/start')
async def start(body:dict):
    site_id=body.get('site','a')
    if site_id not in runtime['sites'] or site_id in runtime['tasks'] or runtime['failure']:
        raise HTTPException(409,'original_case_already_started_or_blocked')
    only_b=os.environ.get('LEAD_C22_ONLY_SITE')=='b'
    if only_b and site_id!='b':
        raise HTTPException(409,'fixture_registered_for_b_only')
    if site_id=='b' and not only_b and not runtime['cases'].get('a',{}).get('completed'):
        raise HTTPException(409,'complete_original_a_before_b')
    case=CodexCase(runtime['agent_id'],runtime['sites'][site_id],runtime['endpoint'],output=STATE/('c22-'+site_id+'.json'))
    mechanical=os.environ.get('LEAD_C22_MECHANICAL')=='1'
    case.report.update(real_model=not mechanical,model='none' if mechanical else 'gpt-6.1-sol',
        effort=None if mechanical else 'medium',source_sha=os.environ['LEAD_C22_SOURCE_SHA'],strict_blind=False)
    runtime['cases'][site_id]=case.report
    runtime['case_objects'][site_id]=case
    async def run():
        try:
            await case.run({'publish':'A concrete '+site_id+' publication for the new isolated Codex acceptance.',
                'reply':'A useful '+site_id+' reply attached to the original publication.',
                'unknown':'An independent '+site_id+' publication whose first acknowledgement is intentionally lost.'})
        except BaseException as error:
            runtime['failure']=str(error)
            case.report['exception_type']=type(error).__name__
            case.report['exception']=str(error)
            case.persist()
    runtime['tasks'][site_id]=asyncio.create_task(run())
    return {'started':site_id,'agent_id':runtime['agent_id']}


@app.get('/fixture/c22/status')
def status():
    aid=runtime['agent_id']
    with tx(aid) as c:
        requests=all_rows(c,'SELECT id,task_id,state,thread_id,turn_id,usage,result FROM lead.local_executor_requests ORDER BY created_at')
        methods=all_rows(c,"SELECT request_id,method,count(*) count FROM lead.local_executor_events GROUP BY request_id,method ORDER BY request_id,method")
    return json.loads(json.dumps({'agent_id':aid,'cases':runtime['cases'],'failure':runtime['failure'],
        'requests':requests,'event_methods':methods},default=str))


@app.post('/fixture/c22/resume')
async def resume(body:dict):
    """Explicitly re-enter known original references; never create a replacement."""
    site_id=body.get('site','a')
    case=runtime['case_objects'].get(site_id)
    task=runtime['tasks'].get(site_id)
    if not case or not task or not task.done() or case.report.get('completed') or not case.pending:
        raise HTTPException(409,'original_case_not_resumable')
    if runtime.get('failure'):
        case.report.setdefault('preserved_failures',[]).append(runtime['failure'])
    runtime['failure']=None
    case.report['first_failure']=None
    async def run():
        try:
            await case.run({'publish':'A concrete '+site_id+' publication for the new isolated Codex acceptance.',
                'reply':'A useful '+site_id+' reply attached to the original publication.',
                'unknown':'An independent '+site_id+' publication whose first acknowledgement is intentionally lost.'})
        except BaseException as error:
            runtime['failure']=str(error)
            case.report['exception_type']=type(error).__name__
            case.report['exception']=str(error)
            case.persist()
    runtime['tasks'][site_id]=asyncio.create_task(run())
    return {'resumed_original':site_id,'account_id':case.pending['account_id']}
