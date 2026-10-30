"""Source-linked experience drafts, owner review and explicit template distillation."""

import copy
from uuid import NAMESPACE_URL, uuid5

from . import admin_config as configs
from .db import all_rows, audit, tx, uid


def listing(c, aid=None, platform=None):
    return all_rows(
        c,
        """SELECT f.config_key,f.agent_id,a.name AS agent_name,f.version,f.published_version,f.body,f.draft,f.updated_at
      FROM lead.admin_configs f JOIN lead.agents a ON a.id=f.agent_id WHERE f.kind='experience' AND a.desired_state<>'archived'
      AND (%s::uuid IS NULL OR f.agent_id=%s) AND (%s::text IS NULL OR coalesce(f.draft,f.body)->>'platform'=%s)
      ORDER BY f.updated_at DESC,f.id DESC LIMIT 100""",
        (aid, aid, platform, platform),
    )


def evidence_options(c, aid, platform, target=None):
    from .db import require_agent

    require_agent(c, aid)
    return all_rows(
        c,
        """SELECT a.id,a.target_id,a.confirmed_at,t.display_name AS target_name,left(a.intent->>'body',500) AS excerpt,p.business_profile_id,
      p.body->'operating_strategy'->>'plan_id' AS plan_id,r.revision AS config_revision,p.body->'operating_strategy'->>'catalog_version' AS catalog_version
      FROM lead.actions a JOIN lead.tasks task ON task.id=a.task_id JOIN lead.account_targets t ON t.id=a.target_id
      JOIN lead.content_items ci ON ci.id=a.content_id JOIN lead.plans p ON p.id=ci.plan_id JOIN lead.agent_revisions r ON r.id=p.agent_revision_id
      WHERE a.agent_id=%s AND a.state='confirmed' AND a.intent->>'platform'=%s AND (%s::uuid IS NULL OR a.target_id=%s)
      AND coalesce(task.input->>'bootstrap_verification','false')<>'true' ORDER BY a.confirmed_at DESC,a.id DESC LIMIT 30""",
        (aid, platform, target, target),
    )


def approved_for(c, aid, platform, target, business_profile):
    return all_rows(
        c,
        """SELECT config_key,published_version AS version,body FROM lead.admin_configs
      WHERE kind='experience' AND agent_id=%s AND body->>'review_status'='approved' AND body->>'platform'=%s
      AND body->>'business_profile_id'=%s AND (body->>'target_id' IS NULL OR body->>'target_id'=%s)
      ORDER BY updated_at DESC,id DESC LIMIT 8""",
        (aid, platform, str(business_profile), str(target)),
    )


def capture(c, aid, target, automatic=False):
    from .operating_playbooks import get_playbooks

    report = get_playbooks(aid)
    context = report.get("targets", {}).get(str(target))
    if not context:
        raise ValueError("此目标尚无可用的方案观察记录")
    preferred = context.get("preferred_plan_id")
    if automatic and (context["state"] != "provisional_preference" or not preferred):
        return None
    selected = preferred or (context.get("selected_plan") or {}).get("id", "")
    identity = (
        str(
            uuid5(
                NAMESPACE_URL,
                f"lead-experience:{aid}:{target}:{context.get('business_profile_id')}:{context.get('config_revision')}:{context.get('catalog_version')}:{selected}",
            )
        )
        if automatic
        else uid()
    )
    key = f"experiences/{aid}/{identity}"
    if configs.head(c, key):
        return None
    sources = [
        row
        for row in evidence_options(c, aid, context["platform"], target)
        if str(row["business_profile_id"]) == str(context.get("business_profile_id"))
        and row["config_revision"] == context.get("config_revision")
        and row["catalog_version"] == context.get("catalog_version")
        and (not selected or row["plan_id"] == selected)
    ]
    if automatic and not sources:
        return None
    stats = next((s for s in context.get("stats", []) if s["plan_id"] == selected), {})
    observation = (
        f"当前统计：成熟发布 {stats.get('mature_posts', 0)} 篇，归因访问 {stats.get('sessions', 0)} 次，目标完成会话 {stats.get('outcomes', 0)} 次。"
        if context.get("coverage") == "connected"
        else "回传或观察数据尚不齐备，当前不能判断做法有效。"
    )
    body = {
        "title": context["platform"] + " 渠道方案观察",
        "platform": context["platform"],
        "target_id": str(target),
        "business_profile_id": context.get("business_profile_id"),
        "plan_id": selected,
        "applicable_conditions": "仅适用于此业务、账号目标和本轮观察范围；复用前重新核对条件。",
        "observation": observation,
        "proposed_change": "保留当前暂定方案，同时持续轮换挑战方案；这不构成因果提升结论。"
        if preferred
        else "继续收集可比较的成熟样本，先核对承接入口与回传。",
        "evidence_action_ids": [str(s["id"]) for s in sources[:20]],
        "review_status": "pending",
    }
    return configs.write(
        c,
        key,
        body,
        0,
        action="draft",
        note="Agent 根据真实方案观察提出，等待所有者审核。"
        if automatic
        else "从此目标当前真实方案观察创建经验草稿。",
    )


def collect():
    from .operating_playbooks import get_playbooks

    with tx() as c:
        agents = all_rows(
            c,
            "SELECT id FROM lead.agents WHERE desired_state='running' ORDER BY id LIMIT 100",
        )
    for agent in agents:
        aid = str(agent["id"])
        report = get_playbooks(aid)
        for target, context in report.get("targets", {}).items():
            if context.get("state") == "provisional_preference":
                with tx() as c:
                    capture(c, aid, target, automatic=True)


def promote(c, key, expected_version, target_key, target_version, practice, conditions):
    configs.check_scope(c, key, lock=True)
    c.execute("SELECT id FROM lead.admin_configs WHERE config_key=%s FOR UPDATE", (key,))
    source = configs.read(c, key)
    if source["kind"] != "experience" or source["version"] != expected_version:
        raise ValueError("经验已有更新，请刷新后核对")
    if (
        not source["published_version"]
        or source["body"].get("review_status") != "approved"
    ):
        raise ValueError("请先审核并发布这条经验")
    if source["draft"]:
        raise ValueError("经验还有未发布的草稿，请先完成审核")
    if target_key != "strategies/" + source["body"]["platform"]:
        raise ValueError("只能提炼到同一渠道的通用模板")
    if not practice.strip() or not conditions.strip():
        raise ValueError("请明确填写可跨业务复用的做法和适用条件")
    target = configs.read(c, target_key)
    body = copy.deepcopy(
        target["draft"] if target["draft"] is not None else target["body"]
    )
    reference = key + "@" + str(source["published_version"])
    if reference in body["evidence_refs"]:
        raise ValueError("这条经验已提炼到模板，请查看当前模板或草稿")
    body["experience_notes"] = (
        body["experience_notes"]
        + "\n\n"
        + practice.strip()
        + "\n适用条件："
        + conditions.strip()
    ).strip()
    body["evidence_refs"] = [*body["evidence_refs"], reference]
    result = configs.write(
        c,
        target_key,
        body,
        target_version,
        action="draft",
        note="从已审核经验提炼通用做法；仅复制本次明确填写的内容。",
    )
    audit(
        c,
        source["agent_id"],
        "admin.experience.promoted",
        {
            "source": key,
            "source_version": source["published_version"],
            "target": target_key,
            "target_version": result["version"],
        },
    )
    return result
