# 证据节奏核心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development after implementation authorization. Track each step with checkboxes.

**Goal:** 将等待依据、来源、适用范围和恢复方式变成统一且可重放的判定，安全处理旧系统默认限制。

**Architecture:** 无数据库依赖的判定模块加现有 JSON 账本适配；影子判定先行。旧外部动作保留原任务和回执；恢复通过幂等后继记录完成。

**Tech Stack:** Python、Pydantic、PostgreSQL、Temporal；不新增依赖或结构迁移。

**Spec:** [已确认设计](../specs/2026-11-11-channel-recovery-evidence-cadence-design.md)。

## Global Constraints

继承[总计划 Global Constraints 和参数清单](2026-11-11-channel-recovery-evidence-cadence.md)。不改变严格 FAQ，不新增日次数，不将设计认可解释为上线授权。协议独立命名为 `execution_cadence_protocol=1`，不覆盖现有 Supervisor 的 `cadence_version`。

## Review Focus

显式用户值等于默认值、证据过期、多种等待叠加、UTC／带偏移时间混用、旧工作流历史；分别由 C1–C3 的指定测试覆盖。

## C1 证据模型与纯判定

**Files:** Create `services/api/lead_app/execution_cadence.py`、`tests/test_execution_cadence.py`；Modify `services/api/lead_app/community_health.py` 的反馈适配，不重写其持久健康账本。

**Interfaces:** `Scope` 字段固定为 `agent_id, account_id, binding_version, target_id, operation`；`WaitEvidence` 包含 `scope, source, reason_code, evidence_ref, observed_at, valid_until, not_before, resume_event, policy_version`。`source` 只取 `platform,group_rule,owner,engineering,receipt,capability`。`Decision` 包含 `state,constraints,next_at,resume_events,execution_cadence_protocol`；状态为 `ready,waiting_time,waiting_event,receipt_only,terminal`。`decide(evidence: list[WaitEvidence], now: datetime, *, subject: Scope, uncertain_write: bool) -> Decision` 不联网、不读数据库；只选适用于 subject 的限制。账号级限制覆盖该账号各目标，范围不明不得缩窄；授权撤销由调用者先拦截新写入，仍保留原回执核对路径。

- [ ] 添加失败测试 `test_max_deadline_keeps_all_constraints`、`test_event_wait_survives_expired_time`、`test_naive_timestamp_rejected`、`test_expired_rule_evidence_requires_revalidation`、`test_uncertain_write_is_receipt_only`、`test_unrelated_target_is_not_blocked`；时钟固定为带时区样本。

其中 `test_event_wait_survives_expired_time` 构造一条已到期时间约束和一条仍缺规则证据的约束，断言：

```python
assert decision.state == "waiting_event"
assert decision.next_at is None
assert len(decision.constraints) == 2
assert decision.execution_cadence_protocol == 1
```

- [ ] 运行 `python -m pytest --noconftest -q tests/test_execution_cadence.py`，确认因缺少目标接口／行为失败。
- [ ] 实现上述接口：同一操作时间限制取最晚值；有条件等待则 `state=waiting_event,next_at=None`，时间依据仍留在 `constraints`；原提交未知时仅返回 `receipt_only`。过期证据要求重验，不能变成自动许可。
- [ ] 在 `community_health.fault()` 适配处区分可信 RPC／响应反馈与普通错误文本；仅正文含“429”或“flood_wait”不能推断平台限流，来源不明则记录工程故障。添加 `test_raw_text_is_not_platform_rate_evidence`，保留已有瞬态错误恢复测试。
- [ ] 重跑新增用例及 `tests/test_community_outcome_loop.py` 中权限与故障用例；纯模型不得导入应用配置或私有设备模块。结果和差异审阅后，在本任务分支提交一项独立证据判定变更。

**Rollback:** C1 未接入时无运行影响；接入故障后停止受影响的新写入，不删除原健康记录或期限。

## C2 限制来源与旧任务恢复

**Files:** Create `services/api/lead_app/cadence_state.py`、`tests/test_cadence_state.py`；Modify `community_participation.py::quota_due`、`community_growth.py::set_strategy`、`community_opportunities.py`。保持 `ParticipationPolicy` 旧字段和旧读取兼容。

**Interfaces:** `preview_migration(c, aid: str, *, expected_control: int) -> dict` 只读，返回 `preview_digest,source_revision,items`；每项含 `parameter,origin,evidence_ref,value,disposition`。`activate(c, aid: str, preview_digest: str, expected_control: int) -> dict` 只供现有维护者服务上下文在获准后调用，重新核对来源、版本和执行器协议；不向模型或普通页面暴露自动启用入口。`record_decision(c, aid: str, task_id: str, decision: Decision) -> None` 写 `tasks.result.execution_cadence`。`resume_eligible(c, aid: str, *, policy_version: int) -> list[str]` 返回新后继任务 ID。

- [ ] 添加失败用例 `test_explicit_one_is_not_inferred_as_default`、`test_missing_history_preserves_unknown_limit`、`test_activate_rejects_changed_preview`、`test_old_default_join_does_not_consume_reply`、`test_owner_combined_limit_is_preserved`，使用独立数据库 fixture。

对 `combined_per_24h=1` 分别构造已展示并接受的用户约束和可追溯旧代码默认。前者预览断言 `item["origin"] == "owner_explicit"`、`item["disposition"] == "preserve"`；后者才允许 `item["disposition"] == "replace_on_activation"`。未找到充分证据时断言 `item["origin"] == "unresolved"`，且原 `quota_due()` 行为未改变。

- [ ] 运行 `python -m pytest -q tests/test_cadence_state.py`，先确认失败，再实现预览与版本核验。来源必须能与既有发布记录、用户授权内容或策略版本对应；“没填字段”不等于已证实默认。
- [ ] 用 `agents.onboarding_state.execution_cadence` 存 `mode=shadow|active|paused,policy_version,preview_digest,parameter_origins,account_bindings`；影子模式只记录差异。每次 `activate()` 持项目控制锁、核对当前账号及能力后写审计；配置变化导致旧预览失效，不新增数据表。
- [ ] 在 `quota_due` 调用前使用来源裁决：只对已证明的旧系统默认取消共用日额度和无依据间隔；显式限制保持原精确语义，包括用户主动选择的共用额度。应用未激活或设备不匹配时不执行新规则。
- [ ] 添加恢复用例：`test_resume_is_idempotent`、`test_unknown_send_never_gets_successor`、`test_changed_binding_or_revoked_grant_cannot_resume`、`test_original_result_is_unchanged`。后继键固定为原任务 ID＋策略版本；通过现有任务创建入口和授权核验创建，不能仅用 SQL 批量改旧任务状态。
- [ ] 重跑 `tests/test_cadence_state.py tests/test_community_outcome_loop.py tests/test_account_disconnect.py tests/test_fault_isolation.py`；核对同一条件的恢复只生成一个任务。来源适配与恢复迁移作为同一状态契约审阅，未激活前不改变生产策略。

**Rollback:** 保留预览、来源记录、原任务和后继链。已激活后若退出新模式，只暂停新外部操作；不得将旧共用日额度重新当默认生效，也不得恢复过期或已撤销的授权快照。

## C3 持久等待及事件唤醒

**Files:** Modify `community_schedule.py::deadlines/completed`、`community_selection.py::choose`、`channel_research.py::channel_research_tick`、`channel_delivery.py::next_target`、`workflows.py::ChannelDeliveryWorkflow`、`worker.py::deliver_workspace`；Create `tests/test_cadence_workflows.py`。

**Interfaces:** 在既有 Activity 结果增加 `execution_cadence` 决定，不删除 `delay,stop` 兼容字段。`ChannelDeliveryWorkflow.wake(policy_version: int) -> None` 为新增 signal；事件名复用已有 outbox `channel.clock.wake`，负载固定为 `task_id,generation,policy_version`，Worker 仅定位对应既有工作流，不创建新工作流或新队列。

- [ ] 添加失败测试 `test_event_wait_does_not_poll_research`、`test_signal_rechecks_all_constraints`、`test_duplicate_signal_has_one_operation`、`test_restart_keeps_tick_and_deadline`、`test_old_history_replays`。时间等待到期后必须重验条件，不能从“到期”直接提交。
- [ ] 运行 `python -m pytest -q tests/test_cadence_workflows.py`；必须使用专属 Temporal 地址，未提供时本任务验收不通过，不能把跳过写成成功。
- [ ] 新路径以 `workflow.patched("execution-cadence-waits-v1")` 与旧历史分开：明确期限持久等待；条件等待依持久条件事件唤醒，工程心跳不触发来源研究。Supervisor 原 `wake()` 保持不变；渠道时钟通过新 signal 提前重验。
- [ ] 给账号恢复、授权／来源配置版本变更、规则证据更新、资源恢复和新候选接入点发送对应唤醒；先提交状态与 outbox，再消费。任何信号都不表示许可已经满足。
- [ ] 补足没有天然事件的来源：Worker 启动时比较获准来源名单等非敏感配置指纹，仅为变化范围补发一次唤醒；仍可获准读取的群规缺口按既有 `read_after` 做定向工程复查，不重新执行整批研究。未获来源许可时复查只看许可状态，不访问外部页面。
- [ ] 加入 `test_startup_reconciles_changed_source_config_once`、`test_rule_gap_has_bounded_read_probe`、`test_source_wait_probe_never_reads_external_page`。前者断言重复启动不增加第二个操作；后两者断言一次只检查对应目标，浏览器受许可保护，读失败沿用有效工程退避。重启／事件丢失不能形成永久等待，也不能形成无期限忙循环。
- [ ] 保留每账号串行和现有公平调度；先核对原回执，再选到期的合格工作。工程重试次数取当前有效 `RuntimeSettings` 和活动协议，不因新建任务而清零故障次数。
- [ ] 运行 `python -m pytest -q tests/test_cadence_workflows.py tests/test_channel_atom_pilot.py tests/test_community_schedule.py tests/test_fault_isolation_workflows.py`，用 Temporal Replayer 验证新旧历史、continue-as-new 和重复事件；独立提交调度接入。

**Rollback:** 老历史继续原代码分支；新历史必须保留支持新 signal／命令的 Worker。回退业务功能时保持兼容消费者和回执核对，不用 terminate 工作流清除未知动作。
