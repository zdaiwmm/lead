"""Read-only, defensive labels for persisted evidence; never schedules work."""

from .execution_cadence import Decision

REASONS = {
    "source_permission_required": "来源访问尚未获准",
    "no_available_verified_target": "还没有已核验的发布目标",
    "awaiting_validated_opportunity": "等待合格机会",
    "business_required": "需要补齐业务资料",
    "public_web_disabled": "公开来源读取尚未启用",
    "target_schedule_unverified": "目标的时间依据待核实",
    "cadence_version_mismatch": "执行版本不一致，等待核对",
    "cadence_paused": "新操作已暂停",
    "authentication_required": "账号需要重新登录",
    "authorization_required": "需要补齐当前账号授权",
    "combined_per_24h": "等待用户设置的合计额度恢复",
    "joins_per_24h": "等待用户设置的入群额度恢复",
    "replies_per_24h": "等待用户设置的回复额度恢复",
    "reply_interval_minutes": "等待已记录的回复间隔",
    "duplicate_operation": "同一操作已处理",
}
SOURCES = {
    "platform": "平台明确反馈",
    "group_rule": "群规核验",
    "owner": "用户设置",
    "engineering": "运行恢复",
    "receipt": "原操作回执",
    "capability": "执行条件",
}
EVENTS = {
    "source_config_changed": "来源许可变化后重查",
    "configuration_changed": "配置补齐后重查",
    "target_verified": "目标核验通过后重查",
    "candidate_validated": "有新合格机会后重查",
    "execution_capability_changed": "执行条件恢复后重查",
    "cadence_policy_changed": "运营设置变化后重查",
    "evidence_refresh": "重新取得有效依据后重查",
}


def _unverified():
    return {
        "state": "unverified",
        "reason_label": "等待依据待核实",
        "evidence_source": "待核实",
        "scope_label": "适用范围待核实",
        "next_at": None,
        "resume_event_label": "条件变化后重查",
        "needs_user": False,
        "policy_version": None,
        "compatible": None,
    }


def project(raw, *, policy=None, device=None):
    if not isinstance(raw, dict) or "execution_cadence_protocol" not in raw:
        return _unverified()
    decision = Decision.model_validate(raw)
    items = decision.constraints
    scope = "适用范围待核实"
    if items:
        if all((item.scope.target_id or "").startswith("platform:") for item in items):
            scope = "当前渠道"
        elif any(item.scope.account_id is None for item in items):
            scope = "当前项目"
        elif len({item.scope.account_id for item in items}) > 1:
            scope = "相关账号"
        elif len({item.scope.target_id for item in items}) > 1:
            scope = "相关目标"
        elif all(item.scope.target_id is not None for item in items):
            scope = "当前目标"
        else:
            scope = "当前账号"
    labels = []
    for item in items:
        if item.reason_code.startswith("legacy_unverified_"):
            label = "原有限制的来源待核实，继续保留"
        elif item.reason_code.startswith("legacy_system_default_"):
            label = "新规则正在试算，当前继续沿用原有约束"
        elif (
            item.reason_code in {"combined_per_24h", "joins_per_24h", "replies_per_24h"}
            and item.source != "owner"
        ):
            label = "原有限制的来源待核实，继续保留"
        else:
            label = REASONS.get(item.reason_code, "等待依据待核实")
        if label not in labels:
            labels.append(label)
    state_labels = {
        "ready": "节奏条件已满足",
        "waiting_time": "等待已记录的期限",
        "waiting_event": "等待条件变化",
        "receipt_only": "只核对原操作结果",
        "terminal": "本项工作已结束",
    }
    version = max((item.policy_version for item in items), default=None)
    compatible = None
    if isinstance(policy, dict):
        version = policy.get("policy_version", version)
        if (
            isinstance(device, dict)
            and device.get("execution_cadence_protocol") is not None
            and device.get("cadence_policy_version") is not None
        ):
            compatible = (
                device["execution_cadence_protocol"] == 1
                and device["cadence_policy_version"] == version
            )
    if any(item.reason_code == "cadence_version_mismatch" for item in items):
        compatible = False
    next_at = decision.next_at if decision.state == "waiting_time" else None
    if next_at and (next_at.tzinfo is None or next_at.utcoffset() is None):
        next_at = None
    return {
        "state": decision.state,
        "reason_label": "；".join(labels) or state_labels[decision.state],
        "evidence_source": "、".join(
            dict.fromkeys(SOURCES[item.source] for item in items)
        )
        or "待核实",
        "scope_label": scope,
        "next_at": next_at.isoformat() if next_at else None,
        "resume_event_label": "；".join(
            EVENTS.get(event, "条件变化后重查") for event in decision.resume_events
        )
        or ("到期后重新核验" if next_at else None),
        "needs_user": any(
            item.reason_code
            in {
                "business_required",
                "authentication_required",
                "authorization_required",
            }
            for item in items
        ),
        "policy_version": version,
        "compatible": compatible,
    }


def safe_project(raw, **kwargs):
    try:
        return project(raw, **kwargs)
    except Exception:
        # A presentation failure neither changes policy nor enters executor code.
        return _unverified()


def for_task(task, **kwargs):
    from .community_participation import outcome_facts

    view = safe_project((task.get("result") or {}).get("execution_cadence"), **kwargs)
    if outcome_facts(task)["pending_receipt"]:
        view.update(
            state="receipt_only",
            reason_label="只核对原操作结果",
            evidence_source="原操作回执",
            next_at=None,
            resume_event_label="取得原操作回执后更新",
            needs_user=False,
        )
        if (task.get("input") or {}).get("account_id"):
            view["scope_label"] = "原操作所属账号"
    return view
