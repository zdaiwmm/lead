"""Real scoped research runs through controlled, read-only browser RPC responses."""
import asyncio
import json
import socket
from datetime import timedelta

import httpx
import pytest
from lead_app import browser_tools, domain, fetch
from lead_app.db import one, tx
from lead_app.growth import tools
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work

SOURCE = "Independent makers need to compare quote line items without retyping each offer."


@pytest.fixture
def research_rpc(monkeypatch):
    monkeypatch.setenv("LEAD_BROWSER_TRANSPORTS",'{"public_browser":"http://127.0.0.1:18091"}')
    monkeypatch.setenv("LEAD_BROWSER_ROUTE_DEFAULTS",'{"default":"public_browser"}')
    original = httpx.AsyncClient
    state = {"reads":[],"closed":[],"failure":False}

    def respond(request):
        if request.method=="GET":
            return httpx.Response(302,headers={"location":"http://127.0.0.1/private"})
        assert request.url.port==18091
        aid = request.url.path.split("/")[2]
        if request.url.path.endswith("/close"):
            state["closed"].append((aid,request.url.path.split("/")[4]))
            return httpx.Response(200,json={"ok":True})
        data = json.loads(request.content)
        assert data["operation"]=="open"
        url = data["args"]["url"]
        with tx(aid) as c:
            run = one(c,"SELECT profile_id,task_id,mode FROM lead.browser_runs WHERE id=%s",(data["run_id"],))
            profile = one(c,"SELECT slot FROM lead.browser_profiles WHERE id=%s",(run["profile_id"],))
            assert profile["slot"]=="public-research" and run["mode"]=="autonomous"
        state["reads"].append((aid,url,str(run["profile_id"]),str(run["task_id"])))
        if state["failure"]:
            raise httpx.ReadTimeout("Controlled unavailable browser")
        index = "duckduckgo.com" in url
        return httpx.Response(200,json={"url":url,"title":"Search" if index else "Original source",
            "text":"An index snippet is not source evidence." if index else SOURCE,
            "elements":[{"href":"//duckduckgo.com/l/?uddg=https%3A%2F%2Fforum.example%2Ftopic"}] if index else []})

    monkeypatch.setattr(browser_tools.httpx,"AsyncClient",lambda *a,**kw:original(*a,**{**kw,"transport":httpx.MockTransport(respond)}))
    return state


def research_work(aid,payload):
    new_work(aid)
    with tx(aid) as c:
        ref = submit_work(c,aid,TaskSpec(kind="research",purpose="demand",payload=payload,review_at=utcnow()+timedelta(hours=1)))
        return one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))


@pytest.mark.parametrize("failure",[socket.gaierror("DNS unavailable"),OSError("Transport unavailable"),
    httpx.ConnectError("Connect unavailable"),httpx.ReadTimeout("Read unavailable"),None])
async def test_transport_or_scripted_page_uses_original_task_public_browser(growth_agent,monkeypatch,research_rpc,failure):
    async def direct(url,**kwargs):
        if failure:
            raise failure
        return {"url":url,"text":"JavaScript loading","requires_javascript":True}
    monkeypatch.setattr(tools,"fetch_product",direct)
    row = research_work(growth_agent,{"urls":["https://forum.example/topic"]})
    result = await tools.execute_tool(growth_agent,row,{})
    assert result.outcome=="completed" and result.evidence_refs
    assert research_rpc["reads"][0][3]==str(row["id"])
    assert len(research_rpc["closed"])==1
    with tx(growth_agent) as c:
        assert one(c,"SELECT state FROM lead.browser_runs")["state"]=="succeeded"
        assert not one(c,"SELECT id FROM lead.social_accounts")
        assert one(c,"SELECT source_ref FROM lead.knowledge_documents")["source_ref"]=="https://forum.example/topic"


async def test_search_browser_href_is_followed_but_index_never_becomes_evidence(growth_agent,monkeypatch,research_rpc):
    async def unavailable(*args,**kwargs):
        raise socket.gaierror("DNS unavailable")
    monkeypatch.setattr(tools,"fetch_product",unavailable)
    row = research_work(growth_agent,{"queries":["quote comparison needs"]})
    result = await tools.execute_tool(growth_agent,row,{})
    assert result.outcome=="completed"
    assert [r[1] for r in research_rpc["reads"]]==[
        "https://html.duckduckgo.com/html/?q=quote+comparison+needs","https://forum.example/topic"]
    assert len({r[2] for r in research_rpc["reads"]})==1 and len(research_rpc["closed"])==2
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.knowledge_documents")["n"]==1
        assert one(c,"SELECT source_ref FROM lead.knowledge_documents")["source_ref"]=="https://forum.example/topic"


async def test_browser_fallback_keeps_projects_and_evidence_separate(growth_agent,monkeypatch,research_rpc):
    async def unavailable(*args,**kwargs):
        raise OSError("No direct transport")
    monkeypatch.setattr(tools,"fetch_product",unavailable)
    other = domain.create_agent(runtime_engine="growth")
    for aid in (growth_agent,other):
        result = await tools.execute_tool(aid,research_work(aid,{"urls":["https://forum.example/topic"]}),{})
        assert result.outcome=="completed"
        with tx(aid) as c:
            assert one(c,"SELECT count(*) n FROM lead.knowledge_documents")["n"]==1
            assert one(c,"SELECT count(*) n FROM lead.browser_runs")["n"]==1
    assert len({r[2] for r in research_rpc["reads"]})==2


@pytest.mark.parametrize("error",[ValueError("Private destination"),PermissionError("Access denied"),
    httpx.HTTPStatusError("Forbidden",request=httpx.Request("GET","https://forum.example"),response=httpx.Response(403))])
async def test_policy_or_permission_refusal_never_falls_back(growth_agent,monkeypatch,research_rpc,error):
    async def refused(*args,**kwargs):
        raise error
    monkeypatch.setattr(tools,"fetch_product",refused)
    result = await tools.execute_tool(growth_agent,research_work(growth_agent,{"urls":["https://forum.example/topic"]}),{})
    assert result.outcome=="failed" and not research_rpc["reads"]


@pytest.mark.parametrize("url",["http://127.0.0.1/private","https://forum.example/redirect"])
async def test_real_fetch_private_target_or_redirect_cannot_enter_browser(growth_agent,monkeypatch,research_rpc,url):
    monkeypatch.setattr(fetch,"public_addresses",lambda *args:{"1.1.1.1"})
    result = await tools.execute_tool(growth_agent,research_work(growth_agent,{"urls":[url]}),{})
    assert result.outcome=="failed" and not research_rpc["reads"]


async def test_browser_transport_failure_exits_and_closes_owned_run(growth_agent,monkeypatch,research_rpc):
    async def unavailable(*args,**kwargs):
        raise OSError("No direct transport")
    monkeypatch.setattr(tools,"fetch_product",unavailable)
    research_rpc["failure"] = True
    result = await asyncio.wait_for(tools.execute_tool(growth_agent,research_work(growth_agent,{"urls":["https://forum.example/topic"]}),{}),2)
    assert result.outcome=="failed" and result.failure_code=="source_unavailable"
    assert len(research_rpc["reads"])==len(research_rpc["closed"])==1
    with tx(growth_agent) as c:
        assert one(c,"SELECT state FROM lead.browser_runs")["state"]=="failed"
