# 多渠道恢复与证据节奏实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task after the user authorizes implementation and chooses an execution method. Original checkboxes retain the approved task brief; actual completion and evidence are recorded in the linked requirement record.

**Goal:** 恢复三个渠道各自获准的工作，让等待有依据、可恢复，不再由无来源的共用日额度阻断运营。

**Architecture:** 复用现有任务、动作账本、账号隔离和持久排期。增加小型证据判定与状态适配模块，分别接入通用应用和 Telegram 专用执行器；渠道准备不跨越来源或发布许可。

**Tech Stack:** 仓库固定的 Python、Pydantic、PostgreSQL、Temporal、Camoufox、Next.js 和 TypeScript；不新增依赖。

**Spec:** [已确认设计](../specs/2026-11-11-channel-recovery-evidence-cadence-design.md)。

## 当前范围与基线

用户已认可设计与本实施计划，并授权依顺序完成本地开发和隔离验证，明确暂不修改线上。完成状态、候选版本和实际测试证据见需求记录；本轮不推送、合并或发布。

设计起点为 `b05e842`。本轮从 GitHub 核实的 main 为 `355a87bf046852f661da014b0a8f203e2000392a`，已只读同步引用、阅读同版规则和相关代码。未来产品任务从该主线或开工时重新核实的兼容主线创建独立分支；不能把旧目录源码覆盖到新主线。本地实施开工重新核实并采用 `7614e602973644d09e4c648d28e83deda576f43f` 为基线。新的局部故障隔离已在主线，复用其 `runtime_issues`、账号换绑归档及队列放行保护，不重复建设、不假定它已经部署。

## Global Constraints

- 用户选择“先采用规则与明确反馈驱动，积累真实样本后再考虑自适应调整”。本阶段不按效果自动调频。
- 不设置新的入群或回复频率数字；不启用此前提出的“每天 2 次入群、6 次回复、统一 30 分钟回复间隔”。
- 明确的用户限额、授权条件、暂停、工作时段和预算继续有效；未确认来源的旧限制不得静默移除。
- 答疑保持当前有效范围；不自动放宽 FAQ，不启用推广、主动发帖、私信、离群或 Facebook 普通成员投稿。
- 账号页面串行；操作唯一性、未知回执、提交屏障和身份核验保持。未知结果只核对，不重发。
- 默认用现有 JSON 和隔离观察账本持久化，不新增数据库迁移、队列或依赖。确有必要时先更新计划并确认新增范围。
- 一个故障一个 PR；独立任务本地定向验证，PR 快检，完整应用验证集中在合并后的精确 main。应用与专用执行器分别交付。
- 本计划获认可不等于取得生产权限；“暂时不修改线上”持续有效，直到用户另有明确指令。

## Review Focus

| 容易遗漏的输入或故障 | 所属任务和必须添加的测试 |
| --- | --- |
| 旧配置值恰好等于默认值，但来自用户授权 | C2 `test_explicit_one_is_not_inferred_as_default` |
| 多个时间限制与一个条件等待并存，或时间戳无时区 | C1 `test_event_wait_survives_expired_time`、`test_naive_timestamp_rejected` |
| 部分候选来自尚未结束的发现任务，委托仅允许原批次 | T3 `test_partial_batch_requires_continue_discovery_grant` |
| 旧活动历史、重复唤醒或混合设备版本 | C3、T3 `test_old_history_replays`、`test_mixed_version_fences_only_new_writes` |
| 同平台两个账号只有一个得到运营授权 | M1 `test_managed_scope_does_not_enroll_sibling` |

## 任务顺序与交付边界

| 顺序 | 任务 | 产物及依赖 | 暂不触及的范围 |
| --- | --- | --- | --- |
| 0 | P0 核验入口、参数来源及复现条件 | 锁与补读诊断、来源清单；所有产品任务的依据 | 不操作生产账号、不扩大期限或读取条数 |
| 1 | T1 两个确定的判定缺陷 | 两项独立修复，分别有失败用例 | 不改额度、不改答疑方式 |
| 2 | K1 锁争用与 T2 补读缺口 | 按 P0 证实的根因各自修复 | 未证实的分支不猜测改造 |
| 3 | C1 统一证据判定、C2 来源与恢复状态 | 节奏规则及旧限制预览；依赖 P0 | 默认不激活、不迁移用户限额 |
| 4 | C3 持久等待与 T3 设备和候选接入 | 事件唤醒、部分批次交付、版本互认；依赖 C1/C2 | 不更换未知回执的原控制者 |
| 5 | M1 Facebook 接入及 M2 内部准备 | 在真实授权范围内登记任务；依赖 C1/C2 | 来源和平台放行门槛不变 |
| 6 | U1 状态与成果呈现 | 用户读得懂原因和下一步；依赖相应后端任务 | 不新增逐条审批或虚构成果 |
| 7 | V1 联合验收和交付准备 | 对照设计验收、冻结兼容表与回退证据 | 本轮不合并、不切换生产 |

执行分为三个专项，[节奏核心](2026-11-11-evidence-cadence-core.md)、[Telegram 执行器](2026-11-11-evidence-cadence-telegram.md)、[渠道与工作台](2026-11-11-evidence-cadence-channels.md)。各自按上述依赖进入；拆分依据是验收与回退边界，不是要求同时执行。专项中未写目录前缀的应用 `.py` 文件均指 `services/api/lead_app/` 下同名文件；私有包路径由 P0 的 `private_source_root` 确定。

## P0 证据和环境准备

**Files:** 更新 `docs/requirements/20261111-channel-recovery-evidence-cadence.md` 的去敏结论；原始证据仅写受控的 `.state/channel-cadence-plan/`，不进入 Git。读取新 main、当前应用、设备模块、策略历史和既有任务；不得输出凭据或群聊正文。

- [ ] 核对当时线上版本、在途发布、未决动作、接管、数据库版本和执行器模块摘要。只读取得并记录真实安装入口与调用者；现有应用、设备分别建立来源图。
- [ ] 记录私有 `context.json`：`app_revision`、`device_revision`、`device_module_digests`、`private_source_root`、`test_database_host_port`、`test_temporal_address`；其他任务只从此处取私有路径。没有可读设备源码时，只有设备任务受阻，不改用陈旧副本。
- [ ] 将旧每日额度、回复间隔、搜索冷却、用户排期和工程退避逐一归类为 `verified_system_default`、`owner_explicit`、`platform_observed`、`engineering` 或 `unresolved`。包含 `channel_calendar.default_schedule` 与 `operating_workspace.channel_config` 的继承规则，不能让另一处未审查的默认周期继续造成隐性停摆。仅值相同、配置未显式填写或没有看到审计，均不足以证明是系统默认；模型补齐字段或发布快照含默认值也不单独证明是用户明确设置。
- [ ] 用当前实际模块在隔离 PostgreSQL 和合成账号上复现历史回访与渠道工作竞争，保留阻塞 PID、锁范围、事务时长和触发入口；未能复现则保留 K1 诊断任务，不提交猜测性锁修改。
- [ ] 用受控 Telegram 页面覆盖前台轮读和备用完整列表入口，确认超时、列表不完整、身份变化和附件外链各自返回；至少取得一个导致实际补读受阻分支的可复现样本，才进入 T2 的行为修复。

隔离环境使用专属 PostgreSQL 实例与 Temporal 开发服务，测试按现有 `isolated_database` fixture 建立 `lead_test_<uuid>`；`LEAD_STATE_DIR` 指向该任务专属目录。`config.json` 用测试所有者 UUID，清除真实模型凭据、代理和生产地址。导入应用前核实配置，因为 `lead_app.config` 在导入时就读取状态文件，且会合并根目录 `.env`。涉及浏览器的测试仅访问本地合成站点。

## 工程参数清单

以下数字是最新已读源码的工程默认值，不是平台要求，也不是本轮新定的日常运营频率。实施时取有效配置及版本；任何调整均需说明来源和对应边界用例。

| 来源 | 现有值 | 本轮处理 |
| --- | --- | --- |
| `admin_settings.RuntimeSettings` | `retry_attempts=3`、`retry_interval_seconds=30`、`max_concurrent_agents=3`；`pause_after_failures=5` 仍在配置模型中 | 复用尝试数、退避和容量；遵循新主线的局部故障行为，不恢复全项目暂停 |
| `workflows.RETRY` | 初始 2 秒、上限 30 秒、最多 3 次 Activity 尝试 | 作为持久活动故障恢复；外部动作尝试仍受账本约束，二者不能叠乘为重复提交 |
| `ChannelPreparationWorkflow` | 控制锁忙最多 8 轮，等待 `min(60, 2**(attempt+1))` 秒 | 仅内部筹备有限让出，耗尽后明确结束，不循环重建同一任务 |
| `channel_delivery.current` / `atomic_dispatch.connected_inputs` | `lock_timeout=500ms` | 不扩大超时掩盖长锁；K1 处理实际持锁路径 |
| `community_schedule` | 心跳 300 秒、常规复查 3600 秒；回执按原独立退避 | 心跳和回执维护与新外部操作分开；不把心跳当工作进展 |
| `community_health.transition` | 首次故障至少 120 秒、后续至少 7200 秒的既有范围退避 | 归类为工程恢复；迁移时保留已有有效期限，后续新故障走可解释分类，不称平台强制限流 |
| 现有参与默认及设备源码 | 共用 1 次／24 小时、回复间隔 30 分钟、搜索冷却 30 分钟 | 逐项审查来源；仅已证实的系统默认可在获准激活时替换；明确用户限制继续执行 |
| `channel_calendar.default_schedule` | Facebook 默认每周一／三／五 19 点、每 7 天最多 3 次、间隔 20 小时；其他渠道各有默认 | 纳入来源清单；明确接受的内容排期保留，未完成来源审查的值不能悄悄移除或冒称平台限制 |

## V1 联合验收和分批回退

- [ ] 对每个任务先运行新增失败用例，确认因目标行为失败；修复后原用例与直接消费者通过，再做一次同版交叉场景验证。跳过必验浏览器或 Temporal 用例不算通过。
- [ ] 组合场景必须含：同日入群后回答、旧用户额度、来源未放行、同平台双账号、慢 Telegram／快 Reddit、重启与重复事件、半提交与撤权、零合格机会。
- [ ] 对设计第八节逐项填写证据位置和结果；保留应用 SHA、设备摘要、环境和命令。更新需求记录、交付待办及 `apps/web/public/workflow-guide.html`。
- [ ] 若以后取得交付授权：每个故障正常 PR，同一 head 必需快检成功后再合并；对合并后的精确 main 执行一次匹配范围的完整应用验证，并复用同轮产物。文档绿色不能放行应用，设备包另核验。

| 故障发生阶段 | 回退方式 | 不能做的事 |
| --- | --- | --- |
| 本地或隔离验证失败 | 停止该任务，保留失败证据和原分支；其他无依赖任务可继续 | 不改变生产、不清空任务使测试变绿 |
| 新策略尚未激活 | 保持原生产状态，修复候选；已验证的独立判定修复可以单独交付 | 不因代码部署自动删旧额度 |
| 新规则已激活后异常 | 暂停受影响范围的新外部操作，保持回执和纯内部准备；修复前滚优先 | 不静默回到旧共用日额度，不重新认领未知动作 |
| 应用或设备需换回旧版本 | 先冻结新写入，核对持久历史与模块兼容；旧版本不识别新策略时继续冻结相应写入 | 不直接恢复旧策略快照，不降低已记录的平台期限 |
| 已产生新队列历史 | 保留新主线所需的队列消费者，按故障隔离发布记录回退 | 不把停止消费者当作安全回退 |

冻结兼容表时至少演练：旧应用／旧设备基线、新应用／旧设备、旧应用／新设备、新应用／新设备。只有后者经回读确认才激活新节奏。升级一半时只核对原回执和执行明确兼容的工作；设备安装、应用切换、策略激活分别留证。新策略默认处于影子判定状态，任何生产安装和激活仍等待用户另行授权。

## 本轮交付状态

计划文档已编写，产品任务均未执行；后续结果只能由对应命令和真实证据填写。建议由当前实施者按依赖逐项完成，再做一次独立整体验收，减少共享接口和设备基线在多次交接中失配；执行方式由用户在开始开发前选择。
