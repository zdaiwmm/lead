"""Queries never dispatch work. User controls share the canonical command boundary."""
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field

from ..db import all_rows, audit, emit, j, one, require_agent, tx
from ..scope import actor_ref, current_tenant
from .contracts import Contract, GoalRef, utcnow
from .outcomes import summarize
from .store import digest, objective_revision

router = APIRouter()
LABELS = {"research":"调查产品与真实需求","content":"制作有依据的内容","account":"核对账号与可运营位置",
          "channel":"推进渠道操作","observe":"核对真实结果","recover":"恢复受影响的能力","decision":"根据证据调整下一步"}


def render_growth(aid):
    with tx(aid) as c:
        agent = require_agent(c,aid)
        contract = agent["onboarding_state"].get("operating_contract",{})
        work = all_rows(c,"""SELECT t.id,t.state,t.type,t.created_at,t.scheduled_at,t.experiment_id,
            t.input#>>'{spec,purpose}' purpose,t.result#>'{runtime,wait}' wait,t.result#>'{runtime,recovery}' recovery,
            s.platform,s.id account_id FROM lead.tasks t
            LEFT JOIN lead.social_accounts s ON s.id::text=coalesce(t.input#>>'{spec,account_id}',t.input->>'account_id')
            WHERE t.type LIKE 'growth.%%' AND (t.state IN ('planned','queued','running','waiting_user','reconciling')
                OR (t.state='failed' AND t.result#>>'{runtime,recovery,status}'='blocked'))
            ORDER BY t.created_at,t.id LIMIT 50""")
        experiments = all_rows(c,"""SELECT id,revision,state,specification->>'question' question,
            specification->>'hypothesis' hypothesis,specification->>'learning_signal' learning_signal,
            specification->>'review_at' review_at,conclusion FROM lead.growth_experiments
            ORDER BY updated_at DESC,id DESC LIMIT 8""")
        recent = all_rows(c,"""SELECT id,type,state,completed_at FROM lead.tasks WHERE type LIKE 'growth.%%'
            AND type<>'growth.decision' AND state IN ('succeeded','failed','canceled')
            ORDER BY completed_at DESC,id DESC LIMIT 6""")
    for item in work+recent:
        item["title"] = LABELS.get(item["type"].removeprefix("growth."),"推进工作")
        episode = item.get("recovery")
        if episode and episode.get("status")=="blocked":
            item["wait"]={"reason":episode.get("reason") or "recovery_exhausted",
                "responsibility":"product","check_at":episode.get("next_check_at"),
                "escalation":"产品正在等待原能力修复；其他独立工作继续。"}
    waits = [item for item in work if item.get("wait")]
    return {"agent_id":str(aid),"objective":{"revision":objective_revision(agent),
        "description":contract.get("objective") or "让真正需要产品的人获得核心价值", "event_ref":contract.get("event_ref")},
        "current_work":[item for item in work if not item.get("wait")],"current_learning":experiments,
        "results":summarize(aid),"waiting":waits,"owner_actions":[item for item in waits if item["wait"].get("responsibility")=="owner"],
        "recent_work":recent,"as_of":utcnow().isoformat()}


@router.get("/api/agents/{aid}/growth")
def growth_status(aid: UUID):
    return render_growth(str(aid))


class GrowthCommand(Contract):
    command_id: UUID
    expected_revision: int = Field(ge=0)
    kind: Literal["objective.update","account.pause","account.resume","account.revoke"]
    payload: dict


class Objective(Contract):
    description: str = Field(min_length=3,max_length=2000)
    audience: str = Field(default="",max_length=1000)
    event_ref: GoalRef | None = None


class AccountControl(Contract):
    account_id: UUID


@router.post("/api/agents/{aid}/growth/commands")
def growth_command(aid: UUID, body: GrowthCommand):
    aid = str(aid)
    with tx(aid) as c:
        agent = require_agent(c,aid,True)
        identity = "growth-user:"+str(body.command_id)
        sha = digest(body.model_dump(mode="json"))
        previous = one(c,"SELECT * FROM lead.idempotency_requests WHERE actor_ref=%s AND scope='growth.user' AND idempotency_key=%s",(actor_ref(),identity))
        if previous:
            if previous["request_hash"] != sha:
                raise HTTPException(409,"这项操作的内容已变化，请重新提交")
            return json.loads(previous["response_ref"])
        if body.kind=="objective.update":
            value = Objective.model_validate(body.payload)
            if objective_revision(agent) != body.expected_revision:
                raise HTTPException(409,"目标已更新，请刷新后保存")
            if value.event_ref and value.event_ref.event_name:
                event = one(c,"SELECT * FROM lead.pixel_definitions WHERE connection_id=%s AND name=%s AND revision=%s",(
                    value.event_ref.connection_id,value.event_ref.event_name,value.event_ref.definition_revision))
                if not event:
                    raise HTTPException(422,"业务事件或口径已变化，请核对当前定义")
                if event["method"]!="server" or event["name"] in ("PageView","Engagement","Identify"):
                    raise HTTPException(422,"核心成果需要选择由业务服务端证明的价值事件")
            contract = {**agent["onboarding_state"].get("operating_contract",{}),"revision":body.expected_revision+1,
                        "objective":value.description,"audience":value.audience,
                        "event_ref":value.event_ref.model_dump(mode="json") if value.event_ref else None,
                        "source":"owner_command","source_ref":identity}
            c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract}',%s) WHERE id=%s",(j(contract),aid))
            audit(c,aid,"growth.objective_updated",{"revision":contract["revision"],"source_ref":identity})
            emit(c,aid,"growth.wake",{"reason":"objective_changed","revision":contract["revision"]})
            response = {"ok":True,"revision":contract["revision"]}
        else:
            from ..account_intents import set_intent
            account = AccountControl.model_validate(body.payload)
            desired = {"account.pause":"paused","account.resume":"running","account.revoke":"revoked"}[body.kind]
            revision = set_intent(aid,account.account_id,desired,source="owner_command",source_ref=identity,expected_revision=body.expected_revision)
            response = {"ok":True,"revision":revision}
        c.execute("""INSERT INTO lead.idempotency_requests(tenant_id,agent_id,actor_ref,scope,idempotency_key,request_hash,state,response_ref,http_status,expires_at)
            VALUES(%s,%s,%s,'growth.user',%s,%s,'complete',%s,200,now()+interval '90 days')""",
            (current_tenant(),aid,actor_ref(),identity,sha,json.dumps(response)))
        return response


@router.get("/api/agents/{aid}/work/{task_id}/basis")
def work_basis(aid: UUID, task_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c,str(aid))
        task = one(c,"SELECT input,experiment_id FROM lead.tasks WHERE id=%s",(task_id,))
        if not task:
            raise HTTPException(404)
        spec = task["input"].get("spec",{})
        experiment = one(c,"SELECT id,revision,specification FROM lead.growth_experiments WHERE id=%s",(task["experiment_id"],)) if task["experiment_id"] else None
        return {"task_id":task_id,"purpose":spec.get("purpose"),"completion_rule":spec.get("completion_rule"),
                "experiment":experiment,"sources":spec.get("evidence_refs",[]),"required_refs":spec.get("required_refs",[])}


@router.get("/api/agents/{aid}/work/{task_id}/activity")
def work_activity(aid: UUID,task_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c,str(aid))
        task = one(c,"SELECT type,state,result,created_at,completed_at FROM lead.tasks WHERE id=%s",(task_id,))
        if not task:
            raise HTTPException(404)
        records = all_rows(c,"SELECT title,detail,level,created_at FROM lead.activity WHERE task_id=%s ORDER BY created_at DESC LIMIT 50",(task_id,))
        actions = all_rows(c,"SELECT id,state,external_url,external_id,confirmed_at FROM lead.actions WHERE task_id=%s ORDER BY created_at",(task_id,))
        content = one(c,"""SELECT body FROM lead.content_items WHERE validation_result->>'growth_task_id'=%s
            OR id::text=(SELECT input->>'content_id' FROM lead.tasks WHERE id=%s) ORDER BY created_at DESC LIMIT 1""",(str(task_id),task_id))
        return {"task":{key:value for key,value in task.items() if key!="result"},
                "output":(task["result"] or {}).get("output",{}),"records":records,"actions":actions,"content":content["body"] if content else None}
