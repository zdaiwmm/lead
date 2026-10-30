"""Workspace directory and atomic, version-checked Agent controls."""

from .db import all_rows, audit, one


def directory(c, query="", state="active", platform=None, before=None, before_id=None):
    if state not in {"active", "all", "running", "paused", "draft", "archived"}:
        raise ValueError("Agent 状态筛选无效")
    return all_rows(
        c,
        """SELECT a.id,a.name,a.desired_state,a.health_state,a.control_version,a.created_at,a.updated_at,
      coalesce((SELECT jsonb_agg(DISTINCT platform) FROM lead.social_accounts WHERE agent_id=a.id AND platform<>'lab'),'[]'::jsonb) AS platforms
      FROM lead.agents a WHERE (%s='all' OR (%s='active' AND a.desired_state<>'archived') OR a.desired_state=%s)
      AND a.name ILIKE %s AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM lead.social_accounts s WHERE s.agent_id=a.id AND s.platform=%s))
      AND (%s::timestamptz IS NULL OR (a.created_at,a.id)<(%s,%s::uuid))
      ORDER BY a.created_at DESC,a.id DESC LIMIT 50""",
        (
            state,
            state,
            state,
            "%"
            + query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            + "%",
            platform,
            platform,
            before,
            before,
            before_id or "00000000-0000-0000-0000-000000000000",
        ),
    )


def control_agents(c, agents, action, expected_control_versions, note=""):
    from .domain import control_in_tx

    ids = sorted(set(str(a) for a in agents))
    if action not in {"pause", "archive"} or set(ids) != set(expected_control_versions):
        raise ValueError("请提供每个所选 Agent 的当前控制版本")
    rows = all_rows(
        c,
        "SELECT id,desired_state,control_version FROM lead.agents WHERE id=ANY(%s::uuid[]) ORDER BY id FOR NO KEY UPDATE",
        (ids,),
    )
    if len(rows) != len(ids):
        raise LookupError("部分 Agent 不属于当前工作室")
    for row in rows:
        if row["control_version"] != expected_control_versions[str(row["id"])]:
            raise ValueError("部分 Agent 的控制状态已变化，请刷新后重新核对")
        if action == "pause" and row["desired_state"] == "archived":
            raise ValueError("已归档 Agent 不能执行暂停操作")
    previous = one(c, "SELECT current_setting('app.agent_id',true) AS aid")["aid"] or ""
    try:
        for aid in ids:
            c.execute("SELECT set_config('app.agent_id',%s,true)", (aid,))
            control_in_tx(c, aid, action)
    finally:
        c.execute("SELECT set_config('app.agent_id',%s,true)", (previous,))
    audit(c, None, "admin.agents." + action, {"agents": ids, "note": note})
    return {"affected": ids, "action": action}
