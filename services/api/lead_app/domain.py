import hashlib

from .config import STATE, model_config
from .db import (
    all_rows,
    append_message,
    audit,
    emit,
    j,
    one,
    record,
    require_agent,
    tx,
    uid,
)
from .scope import actor_ref, current_tenant

PLATFORMS = [
    ("telegram", "Telegram", "https://web.telegram.org/k/"),
    ("facebook", "Facebook", "https://www.facebook.com/"),
    ("instagram", "Instagram", "https://www.instagram.com/"),
    ("youtube", "YouTube", "https://www.youtube.com/"),
    ("tiktok", "TikTok", "https://www.tiktok.com/"),
    ("x", "X", "https://x.com/"),
    ("linkedin", "LinkedIn", "https://www.linkedin.com/"),
    ("reddit", "Reddit", "https://www.reddit.com/"),
    ("pinterest", "Pinterest", "https://www.pinterest.com/"),
    ("threads", "Threads", "https://www.threads.com/"),
    ("whatsapp", "WhatsApp", "https://web.whatsapp.com/"),
]


def create_agent(request_key=None):
    aid, nid, pid = uid(), uid(), uid()
    model = model_config()
    with tx() as c:
        if request_key:
            claimed = one(
                c,
                "INSERT INTO lead.idempotency_requests(tenant_id,actor_ref,scope,idempotency_key,request_hash,state,expires_at) VALUES(%s,%s,'agent.create',%s,'empty','pending',now()+interval '7 days') ON CONFLICT(tenant_id,actor_ref,scope,idempotency_key) DO NOTHING RETURNING id",
                (current_tenant(), actor_ref(), request_key),
            )
            if not claimed:
                previous = one(
                    c,
                    "SELECT response_ref FROM lead.idempotency_requests WHERE actor_ref=%s AND scope='agent.create' AND idempotency_key=%s",
                    (actor_ref(), request_key),
                )
                return previous["response_ref"]
        c.execute(
            "INSERT INTO lead.agents(id,tenant_id,name,health_state) VALUES(%s,%s,%s,%s)",
            (aid, current_tenant(), "新 Agent", "ready"),
        )
        c.execute(
            "INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version) VALUES(%s,%s,1,'deepseek',%s,'lead-1','browser-1','1')",
            (current_tenant(), aid, model["model"]),
        )
        c.execute(
            "INSERT INTO lead.conversations(tenant_id,agent_id) VALUES(%s,%s)",
            (current_tenant(), aid),
        )
        c.execute(
            "INSERT INTO lead.network_identities(id,tenant_id,agent_id,provider_ref,endpoint_ref,secret_ref,mode,policy_version) VALUES(%s,%s,%s,'local','env','env','shared_development',1)",
            (nid, current_tenant(), aid),
        )
        c.execute(
            "INSERT INTO lead.browser_profiles(id,tenant_id,agent_id,provider,engine_version,sdk_version,config_ref,config_hash,storage_ref,key_material_ref,network_identity_id) VALUES(%s,%s,%s,'camoufox','pinned-install','0.5.8',%s,%s,%s,'local-os-permissions',%s)",
            (
                pid,
                current_tenant(),
                aid,
                str(STATE / "profiles" / aid / "identity.json"),
                hashlib.sha256(aid.encode()).hexdigest(),
                str(STATE / "profiles" / aid / "storage"),
                nid,
            ),
        )
        c.execute(
            "INSERT INTO lead.profile_leases(profile_id,tenant_id,agent_id,control_version) VALUES(%s,%s,%s,1)",
            (pid, current_tenant(), aid),
        )
        append_message(
            c,
            aid,
            "assistant",
            "你好，我是你的 AI 运营人员。你想推广什么？发给我产品网址，或者简单介绍一下。",
        )
        audit(c, aid, "agent.created")
        if request_key:
            c.execute(
                "UPDATE lead.idempotency_requests SET state='complete',response_ref=%s,http_status=201,agent_id=%s WHERE id=%s",
                (aid, aid, claimed["id"]),
            )
    return aid


def list_agents():
    with tx() as c:
        agents = all_rows(
            c,
            "SELECT a.*, b.product_url,b.description,b.facts, (SELECT count(*) FROM lead.social_accounts s WHERE s.agent_id=a.id AND s.state='connected' AND s.platform<>'lab') AS connected_accounts, (SELECT count(*) FROM lead.tasks t WHERE t.agent_id=a.id AND t.state='running') AS active_tasks FROM lead.agents a LEFT JOIN LATERAL (SELECT * FROM lead.business_profiles WHERE agent_id=a.id ORDER BY revision DESC LIMIT 1) b ON true WHERE desired_state<>'archived' ORDER BY a.created_at",
        )
    for a in agents:
        with tx(str(a["id"])) as c:
            a["metrics"] = metrics(c)
            a["latest_work"] = one(
                c,
                "SELECT title,created_at FROM lead.activity ORDER BY created_at DESC LIMIT 1",
            )
    return agents


def agent_detail(aid):
    with tx(aid) as c:
        a = require_agent(c, aid)
        a["business"] = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        a["messages"] = all_rows(
            c,
            "SELECT m.*, job.status AS job_status, job.error_code FROM lead.conversation_messages m LEFT JOIN lead.chat_jobs job ON job.message_id=m.id ORDER BY m.seq",
        )
        a["accounts"] = all_rows(
            c,
            "SELECT s.*, t.id AS target_id,t.canonical_url,t.display_name AS target_name,t.target_type,t.verified_permissions,t.state AS target_state FROM lead.social_accounts s LEFT JOIN lead.account_targets t ON t.account_id=s.id ORDER BY s.created_at",
        )
        a["activity"] = all_rows(
            c, "SELECT * FROM lead.activity ORDER BY created_at DESC LIMIT 50"
        )
        a["browser_runs"] = all_rows(
            c,
            "SELECT id,message_id,state,goal,started_at,ended_at FROM lead.browser_runs ORDER BY started_at DESC LIMIT 15",
        )
        a["tasks"] = all_rows(
            c, "SELECT * FROM lead.tasks ORDER BY created_at DESC LIMIT 30"
        )
        a["plans"] = all_rows(
            c,
            "SELECT revision,body,created_at,state FROM lead.plans ORDER BY created_at DESC LIMIT 3",
        )
        a["memory"] = all_rows(
            c,
            "SELECT * FROM lead.memory_entries WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 100",
        )
        a["profile"] = one(
            c,
            "SELECT id,provider,engine_version,config_hash,state FROM lead.browser_profiles",
        )
        a["network"] = one(
            c,
            "SELECT mode,state,expected_ip,verified_at FROM lead.network_identities ORDER BY created_at DESC LIMIT 1",
        )
        a["metrics"] = metrics(c)
        a["cards"] = all_rows(
            c,
            "SELECT id,type,state,version,payload,expires_at,expires_at>now() AS valid FROM lead.interaction_cards ORDER BY created_at",
        )
        a["assets"] = all_rows(
            c,
            "SELECT id,media_type,size_bytes,sha256,metadata,rights FROM lead.assets WHERE state='ready' ORDER BY created_at DESC LIMIT 100",
        )
        return a


def metrics(c):
    conn = one(
        c,
        "SELECT count(*) AS n FROM lead.analytics_connections WHERE state='verified' AND last_received_at>now()-interval '1 day'",
    )["n"]
    visits = one(
        c,
        "SELECT count(*) AS visits,count(*) FILTER(WHERE qualified) AS qualified FROM lead.visit_sessions WHERE started_at>now()-interval '30 days' AND trust_level<>'redirect_observed' AND tracking_link_id IS NOT NULL",
    )
    posted = one(
        c,
        "SELECT count(*) AS n FROM lead.actions a JOIN lead.account_targets t ON t.id=a.target_id JOIN lead.social_accounts s ON s.id=t.account_id JOIN lead.tasks task ON task.id=a.task_id WHERE a.state='confirmed' AND s.platform<>'lab' AND coalesce(task.input->>'bootstrap_verification','false')<>'true'",
    )["n"]
    clicks = one(
        c,
        "SELECT count(*) AS n FROM lead.visit_sessions WHERE trust_level='redirect_observed' AND exclusion_reason IS NULL AND started_at>now()-interval '30 days'",
    )["n"]
    server = one(
        c,
        "SELECT count(*) FILTER(WHERE last_received_at>now()-interval '1 day') AS fresh,max(last_received_at) AS last FROM lead.analytics_credentials WHERE state='active'",
    )
    conversions = one(
        c,
        "SELECT count(*) AS n FROM lead.conversion_events WHERE trust_level='server_verified' AND occurred_at>now()-interval '30 days'",
    )["n"]
    return {
        "clicks": clicks,
        "visits": visits["visits"] if conn else None,
        "qualified_visits": visits["qualified"] if conn else None,
        "conversions": conversions if server["fresh"] else None,
        "conversion_coverage": "connected"
        if server["fresh"]
        else "stale"
        if server["last"]
        else "not_connected",
        "conversion_last_received_at": server["last"].isoformat()
        if server["last"]
        else None,
        "published": posted,
        "coverage": "connected" if conn else "not_connected",
        "period": "30d",
        "rule": "engaged-v1",
    }


def submit_message(aid, text, client_id, asset_ids=None):
    asset_ids = list(dict.fromkeys(asset_ids or []))
    with tx(aid) as c:
        require_agent(c, aid, True)
        old = one(
            c,
            "SELECT * FROM lead.conversation_messages WHERE client_message_id=%s",
            (client_id,),
        )
        if old:
            if (
                old["content"]["text"] != text
                or old["content"].get("asset_ids", []) != asset_ids
            ):
                raise ValueError("同一个消息标识不能用于不同内容")
            return old
        if asset_ids:
            scoped = all_rows(
                c,
                "SELECT id FROM lead.assets WHERE id=ANY(%s::uuid[]) AND state='ready'",
                (asset_ids,),
            )
            if len(scoped) != len(asset_ids):
                raise LookupError("一项或多项素材不可用")
        msg = append_message(c, aid, "user", text, client_id, status="pending")
        if asset_ids:
            msg = one(
                c,
                "UPDATE lead.conversation_messages SET content=content || %s WHERE id=%s RETURNING *",
                (j({"asset_ids": asset_ids}), msg["id"]),
            )
        job = one(
            c,
            "INSERT INTO lead.chat_jobs(tenant_id,agent_id,message_id) VALUES(%s,%s,%s) RETURNING id",
            (current_tenant(), aid, msg["id"]),
        )
        emit(c, aid, "chat.enqueue", {"job_id": str(job["id"])})
        return msg


def retry_messages(aid):
    with tx(aid) as c:
        require_agent(c, aid, True)
        for job in all_rows(
            c,
            "UPDATE lead.chat_jobs SET status='pending',error_code=NULL WHERE status IN ('blocked','failed') RETURNING id,message_id",
        ):
            c.execute(
                "UPDATE lead.conversation_messages SET status='pending' WHERE id=%s",
                (job["message_id"],),
            )
            emit(c, aid, "chat.enqueue", {"job_id": str(job["id"])})


def set_control(aid, action):
    state = {
        "pause": "paused",
        "resume": "running",
        "start": "running",
        "archive": "archived",
    }[action]
    with tx(aid) as c:
        current = (
            one(c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
            if action == "archive"
            else require_agent(c, aid, True)
        )
        if not current:
            raise LookupError("Agent 不存在")
        if current["desired_state"] == state:
            if state in ("paused", "archived"):
                c.execute(
                    "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
                )
            return
        if state == "running":
            if not model_config()["key"]:
                raise ValueError("请先在 .env 中填写 DEEPSEEK_API_KEY")
            if not one(
                c,
                "SELECT 1 FROM lead.social_accounts s JOIN lead.account_targets t ON t.account_id=s.id JOIN lead.authorization_grants g ON g.target_id=t.id JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type='publish' WHERE s.state='connected' AND t.state='active' AND g.revoked_at IS NULL AND pc.technical_state='verified' AND pc.permission_state='approved' AND (s.platform='lab' OR (jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'::jsonb))>0 AND t.verified_permissions->>'adapter_version'=pc.adapter_version))",
            ):
                raise ValueError("请先完成可运营账号的连接和授权")
        c.execute(
            "UPDATE lead.agents SET desired_state=%s,health_state=%s,control_version=control_version+1,version=version+1,updated_at=now() WHERE id=%s",
            (state, "queued" if state == "running" else "stopped", aid),
        )
        c.execute(
            "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE state IN ('planned','queued') AND %s<>'running'",
            (state,),
        )
        c.execute(
            "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
        )
        if state == "archived":
            c.execute(
                "UPDATE lead.social_accounts SET state='disconnected' WHERE state<>'disconnected'"
            )
            c.execute(
                "UPDATE lead.authorization_grants SET revoked_at=now() WHERE revoked_at IS NULL"
            )
            c.execute(
                "UPDATE lead.account_targets SET state='disabled' WHERE state='active'"
            )
        audit(c, aid, "agent." + action)
        record(
            c,
            aid,
            {
                "pause": "已暂停",
                "resume": "已恢复自主运营",
                "start": "开始自主运营",
                "archive": "已归档",
            }[action],
        )
        emit(c, aid, "supervisor.wake")


def connect_lab(aid):
    with tx(aid) as c:
        require_agent(c, aid, True)
        existing = one(c, "SELECT id FROM lead.social_accounts WHERE platform='lab'")
        if existing:
            return
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        account, target = uid(), uid()
        c.execute(
            "INSERT INTO lead.social_accounts(id,tenant_id,agent_id,profile_id,platform,external_account_id,display_name,state,verified_at) VALUES(%s,%s,%s,%s,'lab',%s,'本地测试站','connected',now())",
            (account, current_tenant(), aid, profile["id"], aid),
        )
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,display_name,verified_permissions,state) VALUES(%s,%s,%s,%s,'profile',%s,'本地测试站',%s,'active')",
            (target, current_tenant(), aid, account, aid, j(["publish"])),
        )
        c.execute(
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,1,%s,'local_owner')",
            (current_tenant(), aid, target, j(["publish"])),
        )
        record(c, aid, "已连接本地测试站", "仅用于真实浏览器验证，不计入获客效果。")
        audit(c, aid, "lab.authorized")


def queue_lab_publish(aid, body, key):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        previous = one(
            c,
            "SELECT * FROM lead.tasks WHERE workflow_id=%s",
            ("lab:" + aid + ":" + key,),
        )
        if previous:
            if previous["input"].get("body") != body:
                raise ValueError("重复任务的内容不同")
            return previous
        if a["desired_state"] not in ("draft", "running"):
            raise ValueError("请先恢复 Agent")
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'lab.publish','queued',%s,%s,now()) RETURNING *",
            (
                current_tenant(),
                aid,
                "lab:" + aid + ":" + key,
                j({"body": body, "key": key, "control_version": a["control_version"]}),
            ),
        )
        emit(c, aid, "task.enqueue", {"task_id": str(task["id"])})
        return task


def snapshot_preferences(c, aid):
    a = require_agent(c, aid, True)
    rev = a["config_revision"] + 1
    prev = one(
        c,
        "SELECT * FROM lead.agent_revisions WHERE revision=%s",
        (a["config_revision"],),
    )
    active = all_rows(
        c,
        "SELECT id,body FROM lead.memory_entries WHERE kind='preference' AND deleted_at IS NULL ORDER BY created_at",
    )
    c.execute(
        "INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version,preferences) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            current_tenant(),
            aid,
            rev,
            prev["provider"],
            prev["model_ref"],
            prev["prompt_version"],
            prev["toolset_version"],
            prev["output_schema_version"],
            j(
                {
                    "entries": [
                        {"id": str(m["id"]), "text": m["body"]["text"]} for m in active
                    ]
                }
            ),
        ),
    )
    c.execute(
        "UPDATE lead.agents SET config_revision=%s,version=version+1 WHERE id=%s",
        (rev, aid),
    )
    c.execute(
        "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE state IN ('planned','queued')"
    )
    emit(c, aid, "supervisor.wake")
    return rev


def remember(c, aid, body):
    entry = one(
        c,
        "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'preference',%s,'[]','user_explicit') RETURNING id",
        (current_tenant(), aid, j({"text": body})),
    )
    snapshot_preferences(c, aid)
    return str(entry["id"])


def forget_preference(aid, memory_id, expected_revision):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        entry = one(
            c,
            "SELECT * FROM lead.memory_entries WHERE id=%s AND kind='preference'",
            (memory_id,),
        )
        if not entry:
            raise LookupError("偏好不存在")
        if entry["deleted_at"]:
            return {"ok": True, "duplicate": True, "revision": a["config_revision"]}
        if a["config_revision"] != expected_revision:
            raise ValueError("偏好刚有更新，请刷新后再操作")
        c.execute(
            "UPDATE lead.memory_entries SET deleted_at=now() WHERE id=%s", (memory_id,)
        )
        rev = snapshot_preferences(c, aid)
        audit(c, aid, "preference.removed", {"memory_id": memory_id, "revision": rev})
        record(c, aid, "已移除运营偏好", entry["body"]["text"])
        return {"ok": True, "revision": rev}
