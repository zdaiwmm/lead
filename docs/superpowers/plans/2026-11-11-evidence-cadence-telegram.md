# Telegram 判定修复与设备接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development after implementation authorization. Track each step with checkboxes.

**Goal:** 修复误拒、解释置顶缺口，让已合格候选及时交付，并保证专用执行器与通用应用执行同一节奏契约。

**Architecture:** 公开纯判定模块和合成页面测试独立验证；私有设备适配绑定当时真实模块摘要。当前 public main 没有专用执行器的 `rule_verdict` 和完整置顶补读实现，不能假装只改 public `community_runtime.py` 就会修复线上设备。

**Tech Stack:** Python、PostgreSQL、现有 Camoufox／Playwright、设备既有签名发布入口。

**Spec:** [已确认设计](../specs/2026-11-11-channel-recovery-evidence-cadence-design.md)。

## Global Constraints

继承[总计划约束](2026-11-11-channel-recovery-evidence-cadence.md)。私有源码、身份、模块路径和签署资料不进入公开仓库；通过 P0 的受控 `context.json` 获取。设备更新与应用发布分别授权和核验。本轮不操作生产设备。

## Review Focus

中文无空格、已读置顶仍被简介引用、完整列表失败、多账号身份变化、部分批次与委托边界，分别纳入 T1–T3。真实平台零机会是允许的业务结果。

## T1 两个已复现的误拒分别修复

**Files:** Create `services/api/lead_app/community_rule_evidence.py`、`tests/test_community_rule_evidence.py`；私有包中的 `adapter.py` 接入对应 helper，`test_device.py` 覆盖实际 `rule_verdict`。只有公共调用者确实使用时才改 public `community_runtime.py`，不移植设备整文件。

**Interfaces:** `description_kind(text: str, *, topic_matches: bool) -> Literal["substantive","title_only","empty"]`；`rules_status(*, description: str, pinned_complete: bool, external_rules_unresolved: bool) -> Literal["complete","unresolved"]`。两者不授予加入／回复许可；原主题、受众、显式禁令及授权核验仍在调用方。

- [ ] 为中文与英文等义完整简介、URL／账号名／纯标题添加 `test_substantive_description_is_not_space_dependent`、`test_title_or_link_is_not_substantive`，先运行 `python -m pytest --noconftest -q tests/test_community_rule_evidence.py -k description` 确认失败。

```python
assert description_kind("本群供成年人交流语言学习经验，请友善讨论并尊重彼此隐私。", topic_matches=True) == "substantive"
assert description_kind("https://t.me/example", topic_matches=True) == "empty"
assert rules_status(description="Please read pinned rules.", pinned_complete=True, external_rules_unresolved=False) == "complete"
assert rules_status(description="Read the external rules.", pinned_complete=True, external_rules_unresolved=True) == "unresolved"
```

- [ ] 实现 Unicode 内容分类并接入设备当前调用方；不靠英文五词判断，也不把任意短标题当完整规则。验证原主题与受众拒绝仍有效，作为第一项独立修复提交。
- [ ] 添加 `test_read_pins_reference_does_not_invalidate_complete_evidence`、`test_external_link_or_missing_pin_stays_unresolved`，只改变“已完整读取”这个输入验证对照；运行同文件 `-k 'pins or external'`，确认失败再修引用与完成状态的关系，作为第二项独立修复提交。
- [ ] 在受控私有包运行 `python -m pytest -q test_device.py -k rule_verdict`，必须通过真实装配后的函数，不只通过公共 helper。用新鲜摘要验证包实际加载的字节。

**Rollback:** 可独立撤回单项判定修复；不修改已有群规证据和已确认动作。回退遇到新证据结构无法识别时只暂停对应新参与，不宣称规则已经读完。

## K1 先定位并消除跨渠道长锁

**Files:** Create `tests/test_channel_lock_isolation.py`；按 P0 实际阻塞链修改 public `atomic_dispatch.py::connected_inputs/execute_connected`、`channel_delivery.py::current` 或受控设备中的 `record_patrol_schedule/select/membership_review`。这些是候选入口，P0 未证实的入口不修改。

**Interfaces:** 不改变 `current(c, aid, args)` 和 `connected_inputs(c, aid, task_id, generation, pack=None)` 的对外约定。若需拆分设备选择，新增私有 `select_snapshot(c, agent, config) -> dict`，只读候选数据；`commit_selection(c, aid, snapshot: dict) -> dict | None` 只做短事务内最终核验和领取。

- [ ] 在当前模块建立 `test_slow_patrol_does_not_hold_project_control_lock`，通过同步屏障让慢历史查询停住，同时启动另一个账号的内部准备；记录阻塞链，不用任意 sleep 伪造吞吐结论。
- [ ] 运行 `python -m pytest -q tests/test_channel_lock_isolation.py`，确认失败来自已定位的事务持锁，或在私有 `test_device.py` 中取得等价失败证据。若新版公共代码已经不复现，只修设备实际路径，不做无效公共重构。
- [ ] 将历史扫描／纯计算移到领取前或独立事务；状态写入放在选择完成后，领取前比较控制、绑定、配置和任务版本。保留真实点击的提交屏障，不扩大 500ms 超时，也不移除最终锁。
- [ ] 加入 `test_pause_between_snapshot_and_claim_prevents_commit`、`test_two_claimers_keep_one_successor`、`test_revoke_before_click_preserves_unknown_receipt`，验证竞争安全；重跑 `tests/test_channel_atom_pilot.py tests/test_channel_delivery.py` 及设备锁测试，独立提交。

**Rollback:** 查询优化可单独回退；出现领取不确定时停止新外发并查询原账本。不能回放旧候选以弥补“少执行”的任务数。

## T2 备用置顶读取的诊断与修复关口

**Files:** 私有包 `adapter.py`、`test_device.py`；公开新增 `tests/test_community_pin_evidence.py` 和 T1 的 `community_rule_evidence.py`。若行为仅存在私有设备，公开测试固定去敏接口，真实设备装配验证留在私有包。

**Interfaces:** `PinReadResult` 字段为 `status,reader,reason,peer_binding,message_ids,content_refs,total_count,next_cursor`；`status=complete|partial|failed`，`reader=carousel|native_list`。`reason` 固定区分 `identity_changed,incomplete_list,timeout,unreadable_body,external_rules_required`。正文留在原受控证据存储，不进入公共日志。

- [ ] 为 P0 实际失败分支建立一个本地原生浏览器失败用例，并覆盖现有 8 条轮读与最多 32 条完整列表；8／32 是已有读取边界，不是平台节奏上限。
- [ ] 先实现失败阶段记录，再按已证实原因修复；完整性由实际列表总数、消息唯一性和当前群身份验证，不由“已读取 N 条”推断。
- [ ] 若列表确有分页，采用 `peer_binding+证据版本+next_cursor` 保存读取位置；身份、规则或列表版本改变时作废续读。若是附件／外链未覆盖，保持部分状态并说明缺口，不增加条数冒充读完。
- [ ] 运行 `LEAD_BROWSER_TESTS=1 python -m pytest -q tests/test_community_pin_evidence.py` 及私有 `test_device.py -k pin`，覆盖切群、超时、重复页、总数不一致、外链和恢复后的完整列表。不能用缺少环境导致的跳过放行此任务。

**Rollback:** 撤回新读器时保留已有证据和游标版本；旧读器不认识的部分证据仍为未完成，不直接提升通过状态。

## T3 部分批次交付与设备协议

**Files:** Create `services/api/lead_app/community_handoff.py`、`tests/test_community_handoff.py`；Modify `community_discovery.py::advance`、`community_runtime.py::next_candidate`、`community_opportunities.py::next_rule_review`，私有 `adapter.py/test_device.py/manifest.json` 接入 C1–C3。不新增 task type 或 Temporal 队列。

**Interfaces:** `publish_candidate(c, aid: str, discovery_id: str, candidate: dict, snapshot: dict) -> str` 写现有隔离观察账本 `type=community_candidate_handoff`；稳定键为发现任务＋候选规范地址＋绑定／配置版本。`next_handoff(c, aid: str, account: dict, revision: int) -> dict | None` 返回候选及来源引用；只允许经过合格评估的条目，不能从元数据列表直接发布。

- [ ] 添加 `test_eligible_candidate_precedes_batch_completion`、`test_partial_batch_requires_continue_discovery_grant`、`test_duplicate_checkpoint_hands_off_once`、`test_changed_rules_or_binding_invalidate_handoff`；运行 `python -m pytest -q tests/test_community_handoff.py` 确认失败。
- [ ] 接入候选交付：发现任务仍运行时，必须有同账号、同配置且允许继续发现的有效委托，并保持已获准原始批次的锚点。原任务不伪改为成功；后继引用来源和规则证据，C2 控制重入与去重。
- [ ] 设备包加入 `execution_cadence_protocol=1`、共享 helper 摘要、原设备及模块摘要，装配后先影子判定。设备与应用两端都回报同一策略版本后，后续获准维护操作才可激活；沿用既有签署入口，不写通用脚本热替换设备。
- [ ] `test_mixed_version_fences_only_new_writes` 覆盖四种新旧组合；不匹配只停不兼容的新操作，原回执不换执行者。重跑 `tests/test_community_handoff.py tests/test_community_outcome_loop.py tests/test_community_discovery.py` 与私有全量设备专项，冻结兼容表和包摘要。

`test_duplicate_checkpoint_hands_off_once` 同一批次和地址写入两次，断言 `first_handoff_id == second_handoff_id`、对应新参与任务数量为 1；关闭 `continue_discovery` 后，对尚在运行的后续批次断言 `next_handoff(...) is None`，不能为了提前交付放宽委托。

**Rollback:** 保留 handoff 与后继关系；旧版本不理解新模式时冻结该范围新写入。私有包回退从已验证原字节恢复，独立回读；不复用失效签名或假定应用回退会同步设备回退。
