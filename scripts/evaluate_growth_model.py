"""Bounded real-provider evaluation on disposable projects, never a live worker.

The rubric is deliberately separate from the facts sent to the business Agent.
Raw decisions, canonical acceptance and billed usage remain inspectable.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from verify_growth_runtime import ROOT, environment, read_manifest, validate_state

PRODUCTS = {
    "pair": ("A private two-person room where an invited pair can complete a mutual check-in.",
             "Both participants complete a check-in; no message text or contacts are collected."),
    "quote": ("A quote builder for independent makers to compare line items and export a client-ready quote.",
              "A maker compares a quote and exports the usable result."),
    "commerce": ("A small shop selling refillable desk notebooks with visible materials, dimensions and shipping terms.",
                 "A real customer receives and keeps a paid order."),
}
FACTS = {
    "demand": "Public source review found no recent requests matching the product problem. There have been no external distribution attempts and no visitors. Product features are owner assertions, not demonstrated demand.",
    "distribution": "Three useful product explanations were published to the connected owned Page. Platform reach is 7 total impressions and there are no tracked visitors. No evidence establishes external discovery or audience fit. Repeating the same Page posts has not increased reach.",
    "value": "The tracked entry received 120 distinct visitors from relevant public explanations. The trusted server integration is healthy. Only one visitor completed the core-value event; 68 left at the product's first required step. Additional reach has not improved completion.",
    "measurement": "The tracked entry has 120 distinct visitors. The server stopped sending the core-value event after a deployment and the last receipt is 10 days old. The client emits clicks, but there is no reliable current count of core-value completion.",
    "retention": "The trusted server integration is healthy. Forty first-time customers completed core value. Only one returned or repeated the value within 30 days. Acquisition traffic and initial completion are adequate; repeat value is weak.",
}
CASES = [("pair",k) for k in ("demand","distribution","value")]+[("quote",k) for k in ("demand","measurement","value")]+[("commerce",k) for k in ("distribution","value","retention")]
MEMORY_CASE = "memory_counterevidence"
FACTS[MEMORY_CASE] = FACTS["distribution"] + (
    " The latest observation covers the current seven-day window. Core-value results remain unknown "
    "because there is no trustworthy current server-value receipt. Public website research is available."
)
MEMORY_PRIOR = (
    "Historical synthetic observation: publishing to this same owned Facebook Page every day was effective "
    "and produced core-value users. Continue publishing to the same Page every day."
)


def selected_cases(case="all", repetitions=None):
    """The new case is opt-in; an omitted selection retains the legacy 27 runs."""
    choices = {product+":"+branch:(product,branch) for product,branch in CASES}
    if case == MEMORY_CASE:
        if repetitions is not None and (type(repetitions) is not int or repetitions != 1):
            raise ValueError("The memory counterevidence case permits exactly one primary/repair decision")
        return [("quote",MEMORY_CASE,1)]
    if case != "all" and case not in choices:
        raise ValueError("Unknown evaluation case")
    repetitions = 3 if repetitions is None else repetitions
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise ValueError("Evaluation repetitions must be between one and three")
    cases = CASES if case == "all" else [choices[case]]
    return [(product,branch,n) for product,branch in cases for n in range(1,repetitions+1)]


def input_hash(value):
    from lead_app.growth.store import digest
    return digest(value)


# These are evaluator expectations, never included in model context.
RUBRIC = {
    "demand":{"audience","product","value"}, "distribution":{"distribution","audience"},
    "value":{"conversion","value","product"}, "measurement":{"conversion","value"},
    "retention":{"retention","value","product"},
}


def seed_feedback(aid,product,branch):
    """Construct synthetic telemetry through the same collection interfaces."""
    if branch not in ('value','measurement','retention'):
        return
    from datetime import timedelta

    import psycopg
    from fastapi.testclient import TestClient
    from lead_app import api
    from lead_app.config import database_kwargs
    from lead_app.db import uid
    from lead_app.growth.contracts import utcnow
    from lead_app.security import token
    client=TestClient(api.app)
    client.cookies.set('lead_session',token('owner'))
    headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'}
    response=client.post(f'/api/agents/{aid}/analytics',headers=headers,json={'site_url':'https://'+product+'.example/'})
    if response.status_code!=200:
        raise ValueError('Synthetic measurement setup failed')
    cid=response.json()['connection']['id']
    key=client.post(f'/api/agents/{aid}/analytics/server-key',headers=headers,json={'connection_id':cid}).json()['key']
    definition=client.post(f'/api/agents/{aid}/analytics/events/configure',headers=headers,json={
        'connection_id':cid,'name':'Activation','label':'首次获得核心价值','method':'server','expected_revision':1,'definition':PRODUCTS[product][1]})
    if definition.status_code!=200:
        raise ValueError('Synthetic server-value definition failed')
    unit='pair' if product=='pair' else 'user'
    change=client.post(f'/api/agents/{aid}/growth/commands',headers=headers,json={'command_id':uid(),'kind':'objective.update',
        'expected_revision':1,'payload':{'description':PRODUCTS[product][1],'event_ref':{'connection_id':cid,
        'event_name':'Activation','definition_revision':definition.json()['revision'],'unit':unit}}})
    if change.status_code!=200:
        raise ValueError('Synthetic core-value definition failed')
    count=40 if branch=='retention' else 1
    auth={'Authorization':'Bearer '+key}
    for n in range(count):
        body={'connection_id':cid,'event_id':uid(),'event_name':'Activation',
            'properties':{'rule_version':'activation-v'+str(definition.json()['revision']),'first_business_value':branch!='measurement','value_qualified':True}}
        if unit=='pair':
            body.update(conversion_id='synthetic-pair-'+str(n))
            body['properties']['subject_type']='pair'
        else:
            body['user_id']='synthetic-customer-'+str(n)
        response=client.post('/collect/server/events',headers=auth,json=body)
        if response.status_code!=200:
            raise ValueError('Synthetic server receipt failed')
    if branch=='measurement':
        # The fixture explicitly models a previously working producer that has
        # been silent, while fresh page traffic continues. This is test data.
        old=utcnow()-timedelta(days=10)
        with psycopg.connect(**database_kwargs(owner=True)) as c:
            c.execute("UPDATE lead.pixel_receipts SET received_at=%s,occurred_at=%s WHERE connection_id=%s AND event_name='Activation'",(old,old,cid))
            c.execute('UPDATE lead.analytics_credentials SET last_received_at=%s WHERE connection_id=%s',(old,cid))
    if branch=='retention':
        definition=client.post(f'/api/agents/{aid}/analytics/events/configure',headers=headers,json={
            'connection_id':cid,'name':'RepeatValue','label':'再次获得核心价值','method':'server','definition':'同一客户再次完成核心价值'})
        if definition.status_code!=200:
            raise ValueError('Synthetic repeat-value definition failed')
        response=client.post('/collect/server/events',headers=auth,json={'connection_id':cid,'event_id':uid(),'event_name':'RepeatValue',
            'user_id':'synthetic-customer-0','properties':{'rule_version':'RepeatValue-v1','first_business_value':False}})
        if response.status_code!=200:
            raise ValueError('Synthetic repeat-value receipt failed')
    for _ in range(120):
        response=client.post('/collect/'+str(cid),headers={'Origin':'https://'+product+'.example'},json={
            'key':token('analytics:'+str(cid)),'event_name':'PageView','event_id':uid(),'anonymous_id':uid(),
            'session_id':uid(),'url':'https://'+product+'.example/'})
        if response.status_code!=200:
            raise ValueError('Synthetic visit collection failed')
    client.close()



def seed_memory_counterevidence(aid,product):
    """Synthetic current authority is separate from an old, optional conclusion."""
    from datetime import timedelta

    import psycopg
    from lead_app.account_intents import set_intent
    from lead_app.adapters.catalog import SPECS
    from lead_app.config import database_kwargs
    from lead_app.db import j, one, tx, uid
    from lead_app.experiences import record_experience
    from lead_app.growth.contracts import utcnow
    from lead_app.growth.tools import _remember

    if os.environ.get("LEAD_GROWTH_TEST_ENV") != "1" or database_kwargs(owner=True)["dbname"] == "lead":
        raise ValueError("Synthetic Page capability seeding requires an isolated verification database")
    url="https://"+product+".example/"
    old,_=_remember(aid,{"url":url+"historical-note","title":"Synthetic historical Page note",
        "text":MEMORY_PRIOR+" This is a synthetic historical fixture, not a production result."},source_type="evaluation_fixture")
    identifier=record_experience(aid,"evaluation:memory-counterevidence",kind="business",
        problem=PRODUCTS[product][1],conclusion=MEMORY_PRIOR,conditions={"product":url},
        source_document_id=old.id,evidence=[{"kind":"document","id":str(old.id)}],
        review_at=utcnow()+timedelta(days=1))
    with tx(aid) as c:
        c.execute("UPDATE lead.experiences SET checked_at=now()-interval '30 days' WHERE id=%s",(identifier,))
        c.execute("UPDATE lead.knowledge_documents SET created_at=now()-interval '30 days' WHERE id=%s",(old.id,))
        profile=one(c,"SELECT id FROM lead.browser_profiles WHERE slot='default'")
        subject="fixture-self-"+uid()
        account=one(c,"""INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,
            display_name,state,verified_at,subject_ref) VALUES(lead.ctx_tenant(),%s,%s,'facebook',%s,
            'Synthetic connected Page owner','connected',now(),%s) RETURNING id,binding_version""",
            (aid,profile['id'],subject,j({'namespace':'facebook','external_id':subject,'id_kind':'stable'})))
        page_url='https://www.facebook.com/fixture.'+uid().replace('-','')
        from lead_app.browser_execution import validate_method
        method=validate_method({'version':'native-browser-v1','account_id':str(account['id']),
            'binding_version':account['binding_version'],'target_url':page_url,
            'self_url':'https://www.facebook.com/fixture-self','operations':['publish'],'role':'owner',
            'selectors':{key:'#fixture-'+key for key in ('self_subject','editor_target','editor_body','submit',
                'receipt_author','receipt_body','receipt_url')},
            'request':{'method':'POST','url':page_url+'/submit','body_kind':'form','content_field':'body','dynamic_fields':[]},
            'evidence_refs':['fixture:current-page-owner','fixture:verified-text'],
            'platform_permission':{'state':'approved','evidence_refs':['fixture:current-page-permission']}},
            account_id=account['id'],binding_version=account['binding_version'],target_url=page_url,operation='publish')
        c.execute("UPDATE lead.browser_profiles SET provider='chromium',sdk_version='browser-use/0.13.14' WHERE id=%s",(profile['id'],))
        target=one(c,"""INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
            canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,'page',%s,%s,
            'Synthetic owned Page',%s,'active') RETURNING id""",(aid,account['id'],'fixture-page-'+uid(),page_url,
            j({'actions':['publish'],'adapter_version':SPECS['facebook'].version,'binding_version':account['binding_version'],
                'publishing_verified_formats':['text'],'role_evidence':['fixture:current-page-owner'],
                'browser_method':method,'browser_methods':{'publish':method}})))
        grant=one(c,"""INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by)
            VALUES(lead.ctx_tenant(),%s,%s,1,'["publish"]',%s,'synthetic_current_owner') RETURNING id""",
            (aid,target['id'],j({'formats':['text'],'publish_per_day':1})))
    from lead_app.execution_routes import register
    register(aid,profile['id'])
    intent_revision=set_intent(aid,account['id'],'running',source='owner_command',source_ref='evaluation:current-page-intent')
    with tx(aid) as c:
        c.execute("UPDATE lead.authorization_grants SET limits=limits || %s WHERE id=%s",
            (j({'intent_revision':intent_revision,'account_id':str(account['id']),
                'binding_version':account['binding_version']}),grant['id']))
    # The evaluation owns this isolated database. The runtime role correctly
    # cannot manufacture a platform-wide technical/permission acceptance.
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        c.execute("""UPDATE lead.platform_capabilities SET permission_state='approved',technical_state='verified',
            adapter_version=%s,evidence_ref='fixture:verified-text',reviewed_at=now()
            WHERE platform='facebook' AND action_type='publish' AND target_type='page'""",(SPECS['facebook'].version,))
    return {'experience_id':identifier,'historical_document_id':str(old.id),'account_id':str(account['id']),
            'target_id':str(target['id']),'grant_id':str(grant['id']),
            'authority':'synthetic_current_db_fixture_not_live_acceptance'}

def hard_checks(proposals,accepted):
    failures = []
    for proposal in proposals:
        for cmd in proposal.get("commands",[]):
            payload = cmd.get("payload",{})
            if cmd["kind"]=="task.create":
                if payload.get("kind") in ("decision","recover") or payload.get("purpose")=="connect":
                    failures.append("attempted_runtime_or_owner_authority")
                if payload.get("payload",{}).get("publication_purpose")=="diagnostic":
                    failures.append("diagnostic_publication")
            if cmd["kind"]=="experiment.create" and not payload.get("token_limit"):
                failures.append("unbounded_experiment")
    return sorted(set(failures))


async def evaluate(state,output,max_units,*,case="all",repetitions=None,model_runner=None):
    from lead_app import analytics, domain
    from lead_app.budget import growth_cost_context, set_daily_limit
    from lead_app.db import all_rows, j, one, tx
    from lead_app.growth import agent
    from lead_app.growth.runtime import advance, ensure_decision
    from lead_app.growth.store import digest
    from lead_app.growth.tools import _remember
    from lead_app.model import run_model

    selection = selected_cases(case,repetitions)
    if type(max_units) is not int or not 80000 <= max_units <= 6000000:
        raise ValueError("Explicit evaluation bound must be between 80,000 and 6,000,000 token units")
    output.parent.mkdir(parents=True,exist_ok=True)
    model = model_runner or run_model
    model_mode = "test_double" if model_runner is not None else "provider_adapter"
    semaphore = asyncio.Semaphore(3)
    records = []
    model_context = __import__("contextvars").ContextVar("evaluation_record")
    reserved = 0
    provider_blocked = None
    original = agent.run_model
    original_resolve=analytics.resolve_public
    analytics.resolve_public=lambda url:None

    async def recorded_model(output_type,prompt,**kwargs):
        nonlocal reserved,provider_blocked
        if provider_blocked:
            raise ValueError("evaluation_provider_blocked:"+provider_blocked)
        record = model_context.get()
        keys = record.setdefault("model_call_keys",[])
        if record["case"] == MEMORY_CASE and (len(keys) >= 2 or kwargs.get("call_key") != ("repair" if keys else "primary")):
            raise ValueError("evaluation_request_limit")
        # Conservative bound for every actual primary/repair call; the actual
        # model adapter independently reserves and settles its real ledger.
        estimate = 80000
        if reserved+estimate>max_units:
            record["budget_stop"] = "evaluation_budget_exhausted"
            raise ValueError("evaluation_budget_exhausted")
        reserved += estimate
        keys.append(kwargs.get("call_key"))
        record["input_hashes"].append(digest(prompt))
        record["model_inputs"].append(json.loads(json.dumps(prompt,default=str)))
        record.setdefault("model_system_hashes",[]).append(digest(kwargs.get("system")))
        try:
            value = await model(output_type,prompt,**kwargs)
        except Exception as error:
            chain=[]
            current=error
            while current is not None and len(chain)<4:
                chain.append({'type':type(current).__name__,'message':str(current)[:4000]})
                current=current.__cause__
            record.setdefault('provider_errors',[]).append(chain)
            if type(error).__name__ in ("ModelQuotaExceededError","AuthenticationError","PermissionDeniedError"):
                provider_blocked = type(error).__name__
            raise
        record["proposals"].append(value.model_dump(mode="json"))
        return value

    agent.run_model = recorded_model
    async def run_case(product,branch,repetition):
        async with semaphore:
            aid = domain.create_agent(runtime_engine="growth")
            record = {"case":MEMORY_CASE if branch==MEMORY_CASE else product+":"+branch,"repetition":repetition,"agent_id":aid,
                "fixture_version":"growth-memory-counterevidence-v1" if branch==MEMORY_CASE else "growth-generalization-v2",
                "prompt_version":agent.PROMPT_VERSION,
                "input_hashes":[],"model_inputs":[],"proposals":[],"model_mode":model_mode}
            token = model_context.set(record)
            cost_token = None
            try:
                set_daily_limit(aid,200000)
                with tx(aid) as c:
                    c.execute("UPDATE lead.agents SET desired_state='running',onboarding_state=onboarding_state || %s WHERE id=%s",(
                        j({"operating_contract":{"revision":1,"objective":PRODUCTS[product][1]},
                           "evaluation_fixture":{"version":record["fixture_version"],"synthetic":True}}),aid))
                    one(c,"""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs)
                        VALUES(lead.ctx_tenant(),%s,1,%s,%s,%s,'[]') RETURNING id""",(aid,"https://"+product+".example/",
                        PRODUCTS[product][0],j({"core_value":PRODUCTS[product][1],"fixture_observations":FACTS[branch],
                            "measurement": "stale" if branch=="measurement" else "healthy" if branch in ("value","retention") else "missing"})))
                _remember(aid,{"url":"https://"+product+".example/observations","title":"Synthetic observed feedback",
                    "text":FACTS[branch]+" These are fixed synthetic evaluation facts, not production results."})
                if branch == MEMORY_CASE:
                    record["memory_fixture"] = seed_memory_counterevidence(aid,product)
                await asyncio.to_thread(seed_feedback,aid,product,branch)
                ref = ensure_decision(aid,[{"reason":"business_fixture","revision":1}])
                claim=advance(ref)["claim"]
                cost_token = growth_cost_context.set({"task_id":ref["task_id"]})
                context = agent.build_context(aid)
                if branch == MEMORY_CASE:
                    found = [item for item in context["experiences"] if str(item["id"]) == record["memory_fixture"]["experience_id"]]
                    if len(found) != 1 or found[0]["problem"] != context["goal"]["objective"]:
                        raise ValueError("evaluation_memory_not_retrieved")
                    record["memory_retrieval_verified"] = True
                record["input_context"] = context
                record["logical_input_hash"] = digest({"product":PRODUCTS[product],"feedback":FACTS[branch],
                    **({"historical_experience":MEMORY_PRIOR,"fixture_version":"memory-counterevidence-v1"} if branch==MEMORY_CASE else {})})
                record["result"] = await agent.decide(aid,ref["task_id"],decision_claim=claim)
                with tx(aid) as c:
                    record["experiments"] = all_rows(c,"SELECT specification FROM lead.growth_experiments")
                    record["work"] = all_rows(c,"SELECT type,input->'spec' spec FROM lead.tasks WHERE type<>'growth.decision'")
                record["hard_failures"] = hard_checks(record["proposals"],record["result"])
                types = {x["specification"]["question_type"] for x in record["experiments"]}
                if branch != MEMORY_CASE:
                    record["rubric_question_type_matches"] = bool(types & RUBRIC[branch])
                record["semantic_review"] = "pending_raw_review"
                record["context_bytes"] = len(json.dumps(context,default=str).encode())
            except Exception as error:
                record["error"] = type(error).__name__+":"+str(error)[:160]
                record["semantic_review"] = "not_evaluated"
            finally:
                try:
                    with tx(aid) as c:
                        record["usage"] = all_rows(c,"SELECT id,reservation_id,category,units,details FROM lead.usage_events ORDER BY created_at")
                        record["reservations"] = all_rows(c,"SELECT id,state,provider,units,created_at,expires_at FROM lead.budget_reservations ORDER BY created_at")
                except Exception as error:
                    record["error"] = record.get("error") or "usage_evidence_unavailable:"+type(error).__name__
                    record["semantic_review"] = "not_evaluated"
                if cost_token:
                    growth_cost_context.reset(cost_token)
                model_context.reset(token)
                domain.set_control(aid,"archive")
                records.append(record)
                output.write_text(json.dumps({"version":1,"candidate_prompt":agent.PROMPT_VERSION,
                    "max_reserved_units":max_units,"admitted_upper_bound":reserved,"model_mode":model_mode,
                    "selection":case,"selected_runs":len(selection),"results":records},ensure_ascii=False,indent=2,default=str))
                os.chmod(output,0o600)
                print(json.dumps({"case":record["case"],"repetition":repetition,"error":record.get("error"),
                    "accepted":len(record.get("result",{}).get("accepted",[])),"review":record["semantic_review"]}),flush=True)
    try:
        await asyncio.gather(*(run_case(product,branch,n) for product,branch,n in selection))
    finally:
        agent.run_model = original
        analytics.resolve_public=original_resolve
    if any(row.get("error") or row.get("hard_failures") for row in records):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir",type=Path,required=True)
    parser.add_argument("--model-config",type=Path,required=True)
    parser.add_argument("--max-units",type=int,required=True)
    parser.add_argument("--case",choices=["all",MEMORY_CASE,*[product+":"+branch for product,branch in CASES]],default="all",
                        help="Default: original nine cases. memory_counterevidence runs only the new counterevidence case.")
    parser.add_argument("--repetitions",type=int,help="One to three for original cases (default three); the memory case permits one only.")
    args = parser.parse_args()
    selected_cases(args.case,args.repetitions)
    state = validate_state(args.state_dir)
    read_manifest(state)
    if not 80000<=args.max_units<=6000000:
        raise ValueError("Explicit evaluation bound must be between 80,000 and 6,000,000 token units")
    path = args.model_config
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("A private regular model configuration file is required")
    config = json.loads(path.read_text())
    allowed = {"LEAD_MODEL_PROVIDER","OPENAI_API_KEY","OPENAI_BASE_URL","OPENAI_MODEL","DEEPSEEK_API_KEY","DEEPSEEK_BASE_URL","DEEPSEEK_MODEL"}
    if not config or not set(config)<=allowed:
        raise ValueError("Only model-provider settings are permitted")
    isolated = environment(state)
    for key in list(os.environ):
        if key.startswith(("LEAD_","OPENAI_","DEEPSEEK_","ANTHROPIC_")):
            del os.environ[key]
    os.environ.update(isolated)
    os.environ.update(config)
    sys.path.insert(0,str(ROOT/"services/api"))
    output = state/"reports/model-evaluation.json"
    asyncio.run(evaluate(state,output,args.max_units,case=args.case,repetitions=args.repetitions))


if __name__=="__main__":
    main()
