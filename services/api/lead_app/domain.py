import hashlib
import json

from .config import BROWSER_BUILD, BROWSER_SDK, STATE, model_config
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


def create_agent(request_key=None, setup=None, setup_template_version=None):
    aid, nid, pid = uid(), uid(), uid()
    model = model_config()
    request_hash = (
        hashlib.sha256(json.dumps(setup, sort_keys=True).encode()).hexdigest()
        if setup
        else "empty"
    )
    with tx() as c:
        if request_key:
            claimed = one(
                c,
                "INSERT INTO lead.idempotency_requests(tenant_id,actor_ref,scope,idempotency_key,request_hash,state,expires_at) VALUES(%s,%s,'agent.create',%s,%s,'pending',now()+interval '7 days') ON CONFLICT(tenant_id,actor_ref,scope,idempotency_key) DO NOTHING RETURNING id",
                (current_tenant(), actor_ref(), request_key, request_hash),
            )
            if not claimed:
                previous = one(
                    c,
                    "SELECT response_ref,request_hash FROM lead.idempotency_requests WHERE actor_ref=%s AND scope='agent.create' AND idempotency_key=%s",
                    (actor_ref(), request_key),
                )
                if previous["request_hash"] != request_hash:
                    raise ValueError("创建资料已变化，请重试新建流程")
                return previous["response_ref"]
        c.execute(
            "INSERT INTO lead.agents(id,tenant_id,name,health_state) VALUES(%s,%s,%s,%s)",
            (
                aid,
                current_tenant(),
                setup["product_name"] if setup else "新 Agent",
                "ready",
            ),
        )
        c.execute(
            "INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version) VALUES(%s,%s,1,%s,%s,'lead-1','browser-1','1')",
            (current_tenant(), aid, model.get("provider", "deepseek"), model["model"]),
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
            "INSERT INTO lead.browser_profiles(id,tenant_id,agent_id,provider,engine_version,sdk_version,config_ref,config_hash,storage_ref,key_material_ref,network_identity_id) VALUES(%s,%s,%s,'camoufox',%s,%s,%s,%s,%s,'local-os-permissions',%s)",
            (
                pid,
                current_tenant(),
                aid,
                BROWSER_BUILD,
                BROWSER_SDK,
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
            "业务与运营方向已保存，连接一个社媒账号后即可创建 Agent。"
            if setup
            else "你好，我是你的 AI 运营人员。你想推广什么？发给我产品网址，或者简单介绍一下。",
        )
        audit(c, aid, "agent.created")
        if request_key:
            c.execute(
                "UPDATE lead.idempotency_requests SET state='complete',response_ref=%s,http_status=201,agent_id=%s WHERE id=%s",
                (aid, aid, claimed["id"]),
            )
        if setup:
            c.execute("SELECT set_config('app.agent_id',%s,true)", (aid,))
            from .admin_config import onboarding_snapshot

            _write_setup(c, aid, setup, onboarding_snapshot(c, setup_template_version))
    return aid


def _write_setup(c, aid, profile, template=None):
    from .admin_config import onboarding_snapshot, validate_setup

    template = (
        template
        or require_agent(c, aid)["onboarding_state"].get("setup_template")
        or onboarding_snapshot(c)
    )
    validate_setup(profile, template)
    if profile["platform"] not in [p[0] for p in PLATFORMS]:
        raise ValueError("请选择有效的运营渠道")
    old = one(c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
    facts = {
        "name": profile["product_name"],
        "audience": profile["audience"],
        "operating_goal": profile["goal"],
        "content_expectations": profile["content_expectations"],
        "strategy": profile["strategy"],
    }
    facts["_provenance"] = {k: {"source": "owner_setup"} for k in list(facts)}
    c.execute(
        "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs) VALUES(%s,%s,%s,%s,%s,%s,'[]')",
        (
            current_tenant(),
            aid,
            old["revision"] + 1 if old else 1,
            profile["product_url"] or None,
            profile["description"],
            j(facts),
        ),
    )
    c.execute(
        "UPDATE lead.memory_entries SET deleted_at=now() WHERE kind='preference' AND body->>'source'='setup' AND deleted_at IS NULL"
    )
    for label, key in [
        ("运营目标", "goal"),
        ("内容期望", "content_expectations"),
        ("策略倾向", "strategy"),
    ]:
        if profile[key]:
            c.execute(
                "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'preference',%s,'[]','user_explicit')",
                (
                    current_tenant(),
                    aid,
                    j({"text": label + "：" + profile[key], "source": "setup"}),
                ),
            )
    c.execute(
        "UPDATE lead.agents SET name=%s,onboarding_state=onboarding_state || %s,updated_at=now() WHERE id=%s",
        (
            profile["product_name"],
            j({"setup_pending": True, "setup": profile, "setup_template": template}),
            aid,
        ),
    )
    return snapshot_preferences(c, aid)


def update_setup(aid, profile, expected_revision):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if not a["onboarding_state"].get("setup_pending"):
            raise ValueError("新建流程已结束，请在工作台编辑资料")
        if a["config_revision"] != expected_revision:
            raise ValueError("创建资料刚有更新，请重新打开新建流程")
        revision = _write_setup(c, aid, profile)
        audit(c, aid, "agent.setup.updated")
    return {"id": aid, "config_revision": revision}


def complete_setup(aid):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if not a["onboarding_state"].get("setup_pending"):
            return {"id": aid}
        platform = a["onboarding_state"]["setup"]["platform"]
        if not one(
            c,
            "SELECT id FROM lead.social_accounts WHERE platform=%s AND state='connected' AND verified_at IS NOT NULL",
            (platform,),
        ):
            raise ValueError("请先完成所选渠道的登录验证")
        c.execute(
            "UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{setup_pending}','false'),version=version+1,updated_at=now() WHERE id=%s",
            (aid,),
        )
        append_message(
            c,
            aid,
            "assistant",
            "业务资料和运营方向已准备好。你可以在工作台启动运营，我会按照已设定的目标与偏好持续工作。",
        )
        record(c, aid, "Agent 已创建，运营资料已就绪", platform)
        audit(c, aid, "agent.setup.completed", {"platform": platform})
        emit(c, aid, "agent.created")
    return {"id": aid}


def list_agents():
    with tx() as c:
        agents = all_rows(
            c,
            "SELECT a.*, b.product_url,b.description,b.facts, (SELECT count(*) FROM lead.social_accounts s WHERE s.agent_id=a.id AND s.state='connected' AND s.platform<>'lab') AS connected_accounts, (SELECT count(*) FROM lead.tasks t WHERE t.agent_id=a.id AND t.state='running') AS active_tasks FROM lead.agents a LEFT JOIN LATERAL (SELECT * FROM lead.business_profiles WHERE agent_id=a.id ORDER BY revision DESC LIMIT 1) b ON true WHERE desired_state<>'archived' AND NOT coalesce((a.onboarding_state->>'setup_pending')::boolean,false) ORDER BY a.created_at",
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
            "SELECT m.*, job.status AS job_status, job.error_code,job.error_context->>'model_reference' AS request_model,job.error_context->>'provider' AS request_provider FROM lead.conversation_messages m LEFT JOIN lead.chat_jobs job ON job.message_id=m.id ORDER BY m.seq",
        )
        a["accounts"] = all_rows(
            c,
            "SELECT s.*, t.id AS target_id,t.canonical_url,t.display_name AS target_name,t.target_type,t.verified_permissions,t.state AS target_state, EXISTS(SELECT 1 FROM lead.authorization_grants g JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type='publish' WHERE g.target_id=t.id AND g.revoked_at IS NULL AND g.allowed_actions ? 'publish' AND pc.technical_state='verified' AND pc.permission_state='approved' AND t.state='active' AND s.state='connected' AND (s.platform='lab' OR (jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0 AND t.verified_permissions->>'adapter_version'=pc.adapter_version))) AS operational_ready FROM lead.social_accounts s LEFT JOIN LATERAL (SELECT t.* FROM lead.account_targets t WHERE t.account_id=s.id ORDER BY (t.state='active') DESC,(jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0) DESC,(SELECT max(g.granted_at) FROM lead.authorization_grants g WHERE g.target_id=t.id AND g.revoked_at IS NULL) DESC NULLS LAST,t.id DESC LIMIT 1) t ON true ORDER BY s.created_at",
        )
        a["activity"] = _activity_rows(c)
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


def _activity_rows(c, platform=None, limit=50, offset=0):
    platform = platform or None
    if offset < 0:
        raise ValueError("记录起点不能小于零")
    if platform and platform not in [p[0] for p in PLATFORMS] + ["lab", "system"]:
        raise ValueError("社媒不存在")
    return all_rows(
        c,
        """WITH events AS (
      SELECT activity.*,coalesce(task.input->>'platform',s.platform,setup.platform,CASE WHEN activity.detail=ANY(%s) THEN activity.detail END) AS platform,task.state AS task_state
      FROM lead.activity activity LEFT JOIN lead.tasks task ON task.id=activity.task_id
      LEFT JOIN lead.account_targets target ON target.id::text=task.input->>'target_id'
      LEFT JOIN lead.social_accounts s ON s.id=target.account_id
      LEFT JOIN lead.social_accounts setup ON setup.id::text=task.input->>'account_id'
    ) SELECT * FROM events WHERE (%s::text IS NULL OR platform=%s::text OR (%s::text='system' AND platform IS NULL)) ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
        (
            [p[0] for p in PLATFORMS] + ["lab"],
            platform,
            platform,
            platform,
            max(1, min(200, limit)),
            offset,
        ),
    )


def agent_activity(aid, platform=None, limit=50, offset=0):
    with tx(aid) as c:
        require_agent(c, aid)
        return _activity_rows(c, platform, limit, offset)


def metrics(c):
    from .admin_controls import attribution

    policy, attribution_version = attribution(c)
    conn = one(
        c,
        "SELECT count(*) FILTER(WHERE last_received_at>now()-make_interval(hours=>%s)) AS n,max(last_received_at) AS last FROM lead.analytics_connections WHERE provider='lead_site' AND state='verified'",
        (policy["freshness_hours"],),
    )
    fresh = conn["n"]
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
        "SELECT count(*) FILTER(WHERE last_received_at>now()-make_interval(hours=>%s)) AS fresh,max(last_received_at) AS last FROM lead.analytics_credentials WHERE state='active'",
        (policy["freshness_hours"],),
    )
    conversions = one(
        c,
        "SELECT count(*) AS n FROM lead.conversion_events WHERE trust_level='server_verified' AND occurred_at>now()-interval '30 days' AND type=%s",
        (policy["objective"],),
    )["n"]
    return {
        "clicks": clicks,
        "visits": visits["visits"] if fresh else None,
        "qualified_visits": visits["qualified"] if fresh else None,
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
        "coverage": "connected"
        if fresh
        else "stale"
        if conn["last"]
        else "not_connected",
        "last_received_at": conn["last"].isoformat() if conn["last"] else None,
        "period": "30d",
        "rule": "engaged-v1",
        "conversion_objective": policy["objective"],
        "attribution_version": attribution_version,
        "freshness_hours": policy["freshness_hours"],
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
            # Preserve the owner's actual words, never an invented visual caption.
            if text.strip():
                c.execute(
                    "UPDATE lead.assets SET metadata=metadata || %s WHERE id=ANY(%s::uuid[])",
                    (
                        j(
                            {
                                "owner_context": {
                                    "text": text[:6000],
                                    "message_id": str(msg["id"]),
                                    "source": "owner_message",
                                }
                            }
                        ),
                        asset_ids,
                    ),
                )
                snapshot_preferences(c, aid)
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


def set_control(aid, action, platform=None, only_setup=False):
    with tx(aid) as c:
        return control_in_tx(c, aid, action, platform, only_setup)


def control_in_tx(c, aid, action, platform=None, only_setup=False):
    if platform is not None and platform not in [p[0] for p in PLATFORMS]:
        raise ValueError("社媒不存在")
    state = {
        "pause": "paused",
        "resume": "running",
        "start": "running",
        "archive": "archived",
    }[action]
    if (
        one(c, "SELECT current_setting('app.agent_id',true) AS aid")["aid"] or ""
    ) != str(aid):
        raise ValueError("控制操作需要对应 Agent 的数据范围")
    current = (
        one(c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
        if action == "archive"
        else require_agent(c, aid, True)
    )
    if not current:
        raise LookupError("Agent 不存在")
    if only_setup and not current["onboarding_state"].get("setup_pending"):
        return False
    if state == "running" and current["onboarding_state"].get("setup_pending"):
        raise ValueError("请先完成渠道连接和 Agent 创建流程")
    if current["desired_state"] == state:
        if state in ("paused", "archived"):
            c.execute(
                "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
            )
        return
    if state == "running":
        from .admin_operations import reset_failures

        reset_failures(c, aid)
        from .admin_settings import task_connection

        if not task_connection(c, aid)[0]["key"]:
            raise ValueError("运营模型尚未配置凭据，请让所有者在后台检查模型服务")
        if not one(
            c,
            "SELECT 1 FROM lead.social_accounts WHERE state='connected'",
        ):
            raise ValueError("请先连接并登录一个社媒账号，运营准备将由 Agent 完成")
        if platform:
            if not one(
                c,
                "SELECT 1 FROM lead.social_accounts WHERE platform=%s AND state='connected'",
                (platform,),
            ):
                raise ValueError("请先连接并登录此社媒账号")
            c.execute(
                "UPDATE lead.social_accounts SET automation_enabled=(platform=%s) WHERE platform<>'lab'",
                (platform,),
            )
        from .account_setup import delegate_accounts

        delegate_accounts(c, aid, platform)
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
        c.execute("SELECT lead.release_proxy(%s)", (aid,))
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
    return True


def channel_control(aid, platform, enabled):
    if platform not in [p[0] for p in PLATFORMS]:
        raise ValueError("社媒不存在")
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if enabled and agent["onboarding_state"].get("setup_pending"):
            raise ValueError("请先完成渠道连接和 Agent 创建流程")
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE platform=%s ORDER BY created_at DESC LIMIT 1",
            (platform,),
        )
        if not account or account["state"] != "connected":
            raise ValueError("请先连接并登录此社媒账号")
        if enabled and agent["desired_state"] != "running":
            from .admin_operations import reset_failures

            reset_failures(c, aid)
            from .admin_settings import task_connection

            if not task_connection(c, aid)[0]["key"]:
                raise ValueError("运营模型尚未配置凭据，请让所有者在后台检查模型服务")
            c.execute(
                "UPDATE lead.social_accounts SET automation_enabled=(platform=%s) WHERE platform<>'lab'",
                (platform,),
            )
            c.execute(
                "UPDATE lead.agents SET desired_state='running',health_state='queued',control_version=control_version+1,version=version+1,updated_at=now() WHERE id=%s",
                (aid,),
            )
            c.execute(
                "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
            )
        c.execute(
            "UPDATE lead.social_accounts SET automation_enabled=%s WHERE platform=%s",
            (enabled, platform),
        )
        if not enabled:
            c.execute(
                "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE state IN ('planned','queued') AND (input->>'platform'=%s OR input->>'account_id'=%s OR input->>'target_id' IN (SELECT id::text FROM lead.account_targets WHERE account_id=%s))",
                (platform, str(account["id"]), account["id"]),
            )
        else:
            from .account_setup import delegate_accounts

            delegate_accounts(c, aid, platform)
        audit(
            c,
            aid,
            "channel.resumed" if enabled else "channel.paused",
            {"platform": platform},
        )
        record(c, aid, "已启动此社媒运营" if enabled else "已暂停此社媒运营", platform)
        emit(c, aid, "supervisor.wake")
    return {"ok": True}


def update_identity(aid, name, avatar_seed, avatar_asset_id=None):
    with tx(aid) as c:
        require_agent(c, aid, True)
        if avatar_asset_id and not one(
            c,
            "SELECT id FROM lead.assets WHERE id=%s AND state='ready' AND metadata->>'kind'='image'",
            (avatar_asset_id,),
        ):
            raise ValueError("请选择此 Agent 的图片作为头像")
        c.execute(
            "UPDATE lead.agents SET name=%s,onboarding_state=onboarding_state || %s,version=version+1,updated_at=now() WHERE id=%s",
            (
                name,
                j(
                    {
                        "custom_name": True,
                        "avatar_seed": avatar_seed,
                        "avatar_asset_id": avatar_asset_id,
                    }
                ),
                aid,
            ),
        )
        audit(c, aid, "agent.identity.updated")
        emit(c, aid, "agent.updated")
    return {"ok": True}


def update_business(aid, expected_revision, product_url, description, audience):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if a["config_revision"] != expected_revision:
            raise ValueError("资料刚有更新，请刷新后再保存")
        old = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        facts = {**(old["facts"] if old else {}), "audience": audience}
        facts["_provenance"] = {
            **facts.get("_provenance", {}),
            **{
                key: {"source": "owner_edit"}
                for key in ("audience", "description", "product_url")
            },
        }
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                current_tenant(),
                aid,
                old["revision"] + 1 if old else 1,
                product_url or None,
                description,
                j(facts),
                j(old["source_refs"] if old else []),
            ),
        )
        rev = snapshot_preferences(c, aid)
        record(c, aid, "已更新业务偏好")
        audit(c, aid, "business.updated", {"revision": rev})
    return {"ok": True, "revision": rev}


def update_preferences(aid, expected_revision, entries):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if a["config_revision"] != expected_revision:
            raise ValueError("偏好刚有更新，请刷新后再保存")
        existing = all_rows(
            c,
            "SELECT id,body FROM lead.memory_entries WHERE kind='preference' AND deleted_at IS NULL",
        )
        wanted = list(dict.fromkeys(text.strip() for text in entries if text.strip()))
        for item in existing:
            if item["body"]["text"] not in wanted:
                c.execute(
                    "UPDATE lead.memory_entries SET deleted_at=now() WHERE id=%s",
                    (item["id"],),
                )
        previous = {item["body"]["text"] for item in existing}
        for text in wanted:
            if text not in previous:
                c.execute(
                    "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'preference',%s,'[]','user_explicit')",
                    (current_tenant(), aid, j({"text": text})),
                )
        rev = snapshot_preferences(c, aid)
        record(c, aid, "已更新运营偏好")
        audit(c, aid, "preferences.updated", {"revision": rev})
    return {"ok": True, "revision": rev}


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
