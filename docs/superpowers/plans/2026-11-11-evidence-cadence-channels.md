# 渠道接入与工作台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development after implementation authorization. Track each step with checkboxes.

**Goal:** 补齐 Facebook 的授权和执行归属，保持 Reddit 获准内部准备，并准确显示真实进展与等待。

**Architecture:** 扩展已有账号级托管授权与内部筹备入口，复用渠道时钟和局部故障记录；工作台只消费结构化状态，不承担运行控制判定。

**Tech Stack:** 现有 FastAPI、PostgreSQL、Temporal、Next.js 与 TypeScript。

**Spec:** [已确认设计](../specs/2026-11-11-channel-recovery-evidence-cadence-design.md)。

## Global Constraints

继承[总计划约束](2026-11-11-channel-recovery-evidence-cadence.md)。Facebook 仅已有管理目标能力，不开放普通成员投稿；Reddit 平台／来源／目标门槛保留。前端编码前阅读 `apps/web/AGENTS.md` 和安装版本内相关 Next 文档。

## Review Focus

已登录但未授权、同平台多个账号、来源许可缺失、先入群后跳过回复、未知状态／旧 API，分别由 M1、M2、U1 覆盖；未知测量不得显示为零成果。

## M1 Facebook 账号级授权和独立任务

**Files:** Modify `channels.py::authorize_managed_discovery/managed_scope`、`domain.py::channel_control`、`atomic_dispatch.py::refresh_connected/connected_inputs`、`channel_delivery.py::owns/current`、`channel_research.py::unbound_source`、`api.py` 账号入口；Create `services/api/lead_app/channel_authority.py`、`tests/test_channel_authority.py`；Extend `tests/test_channel_delivery.py`。

**Interfaces:** `managed_accounts(c, aid: str, platform: str) -> list[dict]` 只返回当前绑定且持有效 `managed_target_binding` 委托的账号。`has_driver(agent: dict, platform: str) -> bool` 读取 `onboarding_state.managed_channel_delivery` 的启用资格，资格不替代账号授权；保留旧 `owns(agent,platform)` 兼容入口和 Reddit 原成员范围。新已有账号启用入口 `POST /api/agents/{aid}/accounts/{account_id}/managed-scope`，请求 `binding_version,expected_control_version`，响应沿用 `{ok,account_id}`；不得从普通登录自动调用。

- [ ] 添加 `test_login_does_not_authorize_managed_scope`、`test_managed_scope_does_not_enroll_sibling`、`test_no_double_owner_with_campaign_kernel`、`test_rebinding_revokes_old_managed_scope`、`test_facebook_member_target_is_rejected`；运行 `python -m pytest -q tests/test_channel_authority.py` 确认失败。

同平台建立账号 A 和 B，仅明确授权 A。断言 `managed_accounts()` 的 ID 集合恰好是 `{account_a_id}`，B 的 `automation_enabled` 及绑定版本不变；仅登录 A 时该集合为空，提交前撤销 A 授权则不存在可提交动作。
- [ ] 沿用已有 `managed_target_binding` 委托，去除“只能靠全项目试点白名单接入”的缺口：仅明确启用时核对账号／控制版本、记录原范围的托管授权、登记 Facebook 执行资格并唤醒。旧的绑定并启动入口复用同一服务，不重复创建授权。
- [ ] 所有账号筛选复用 `managed_accounts`，不要只查“该平台任一账号有授权”；内部准备、研究和目标验证仍核对本账号绑定。渠道时钟与旧实验内核只保留一个执行负责人。
- [ ] 运行 `python -m pytest -q tests/test_channel_authority.py tests/test_channel_delivery.py tests/test_account_disconnect.py tests/test_channels.py tests/test_fault_isolation.py`；本地合成 Facebook 和 Reddit 目标覆盖首次登记、暂停／恢复、重复启用、换号与提交前撤权。

**Rollback:** 停止新增托管任务而不撤销原有有效授权；原有目标、已确认发布和未知回执保留。旧版本不具备新登记能力时新工作保持等待，不以重新登录或重新授权替代技术回退。

## M2 来源受阻时继续内部准备

**Files:** Modify `atomic_dispatch.py::enqueue_connected/execute_connected`、`operating_workspace.py::draft_pack/persist_workspace`、`channel_research.py::select`；Create `tests/test_channel_preparation_isolation.py`。不新增队列，不修改来源许可环境值或平台能力记录。

**Interfaces:** 复用 `dispatch.channel` 和 `lead-offline`；内部产物增加 `evidence_status="internal_only"`、`business_profile_id,config_revision,account_binding`。不能将此产物当作 `channel_delivery.opportunity()` 的已观察社区证据。

- [ ] 添加 `test_preparation_continues_without_source_permission`、`test_internal_pack_cannot_become_validated_opportunity`、`test_source_wait_never_calls_browser`、`test_business_change_invalidates_prepared_pack`；运行 `python -m pytest -q tests/test_channel_preparation_isolation.py` 确认失败。
- [ ] 把内部准备登记放在研究许可检查之外，使用已保存且有效的业务资料；没有获准的新外读。来源受阻改为 C1 的条件等待，许可配置变化通过 C3 唤醒，不以固定 30 分钟重新读同一受阻来源。
- [ ] 内部产物与真实目标正文／规则核验分开；即使草稿准备完成，缺目标或许可仍不能提交。多个 tick 只保存同一配置的一份准备产物。
- [ ] 运行 `python -m pytest -q tests/test_channel_preparation_isolation.py tests/test_channel_atom_pilot.py tests/test_channel_delivery.py tests/test_operating_playbooks.py`；确认原先有许可的路径仍可按合成目标完成发布和回执。

**Rollback:** 内部产物保留；撤回准备入口不把已生成内部稿件升级为可发布稿。来源许可不因回退改变，未知外部任务继续原路径。

## U1 可解释的状态与成果

**Files:** Modify `operating_plans.py::status`、`telegram_group_board.py::participation_status/execution`、`community_participation.py::business_outcome`、`runtime_issues.py::active` 的标签映射；前端 `apps/web/app/api-client.ts`、`operating-plans.tsx`、`social-accounts.tsx`、`telegram-group-card.tsx`；同步 `apps/web/public/workflow-guide.html`；Create `tests/test_cadence_views.py`、`scripts/verify_cadence_ui.py`。

**Interfaces:** 视图新增 `execution_cadence={state,reason_label,evidence_source,scope_label,next_at,resume_event_label,needs_user,policy_version,compatible}`，直接投影 C1/C2，缺字段显示待核实。`community_participation.outcome_facts(task: dict) -> dict[str,bool]` 返回 `membership_confirmed,message_confirmed,pending_receipt,skipped,blocked,failed` 多事实；汇总视图对每项求计数。保留旧单值 `business_outcome()` 供历史消费者，不用一次最终跳过覆盖已入群事实。

- [ ] 添加 `test_join_fact_survives_skipped_reply`、`test_unknown_reply_does_not_count_confirmed`、`test_waiting_event_has_no_fabricated_countdown`、`test_legacy_payload_displays_unverified`、`test_view_failure_does_not_block_executor`；运行 `python -m pytest -q tests/test_cadence_views.py` 确认失败。

为同一任务设置确认入群及随后跳过回复，断言 `facts["membership_confirmed"] is True`、`facts["message_confirmed"] is False`、`facts["skipped"] is True`；确认发言须另有外部消息 ID。条件等待接口断言 `next_at is None`，界面不得为它生成倒计时。
- [ ] 通过已有只读接口返回结构化状态；展示最近实际动作、当前原因和下一步。沿用新主线 `runtime_issues` 的局部故障机制，不另建报警表，不恢复全局暂停。仅用户可补齐的项目汇总提示，正常工作不逐条审批。
- [ ] 账号页面增加 M1 的明确托管入口和原范围说明；连接成功不展示成已具备外发条件。前端遇旧 API 或旧设备回报不能猜测额度、期限或版本兼容。
- [ ] `verify_cadence_ui.py` 只启动本地合成页面和模拟 API，覆盖桌面／窄屏、时间等待、条件等待、缺许可、兼容待核实、真实零机会及“已入群但未回复”；不访问生产服务或社媒账号。
- [ ] 运行 `python -m pytest -q tests/test_cadence_views.py tests/test_community_outcome_loop.py tests/test_fault_isolation.py`；在 `apps/web` 运行 `npm run typecheck`、`npm run build`，再运行 `python scripts/verify_cadence_ui.py`，查看截图和页面错误。更新流程图后核对文字与运行分支一致，单独审阅 UI／投影变更。

**Rollback:** 回退视图不回写策略或任务。旧界面缺少新字段时展示待核实，不把等待显示成运行成功；执行器始终以服务端和设备证据为准。
