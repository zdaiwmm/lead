# 多渠道运营恢复与按证据控制节奏

## 范围与验收

- 用户于 2026 年 11 月 11 日要求排查 Facebook、Reddit 停滞及 Telegram 无有效进展，随后明确先提供方案、不修改线上。
- 用户已选择先采用规则与明确反馈驱动，积累真实样本后再考虑自适应调整；已明确认可完整设计，并要求细化任务顺序、改动边界、验收与回退。设计阶段仅编写方案；随后用户已明确授权按计划完成本地开发与隔离验证，仍不修改线上。
- 已确认设计：[Facebook Reddit Telegram 运营恢复与节奏控制方案](../superpowers/specs/2026-11-11-channel-recovery-evidence-cadence-design.md)。[实施计划](../superpowers/plans/2026-11-11-channel-recovery-evidence-cadence.md)及三个专项计划已经用户认可。
- 目标：已获授权且条件满足的工作持续推进；每次等待均有可解释的依据、范围和恢复条件。分开处理渠道接入、程序缺陷和运营策略。
- 排除项：未经授权的外部读取／发布、新答疑模式、主动推广、私信、离群、基于样本自动调频及生产切换。不启用此前建议的固定每日 2 次入群／6 次回复。
- 后续验收包括：授权和暂停保护、外部动作幂等、旧默认额度来源与迁移、中文／完整置顶判定、备用读取缺口、持久等待和跨渠道并发，详见设计验收表。

## 设计阶段记录（历史快照）

- 已核实仓库：`zdaiwmm/lead`；设计起点：`b05e8423965602130b4989aaeea2c52c2c0fcb34`；本轮已同步只读引用并核实最新 main `355a87bf046852f661da014b0a8f203e2000392a`。主线协作及小修复规则未变；实施计划已考虑新合入的局部故障隔离、账号归档和工作流队列兼容。
- 任务分支：`codex/lead-evidence-cadence-design-20261111`。本地独立目录撰写文档，未改动共享目录既有内容；没有产品代码、线上数据或配置变更。
- 前序排查证据：现网状态及日志只读核验、应用与设备来源摘要比对、离线判定逻辑最小复现。不是产品修复验收；设计中的验收用例尚未实施。
- 工程待核验：锁争用的实际阻塞链、备用置顶读取失败阶段、现有限额和冷却的来源。确定后才选择对应修复，不把所有超时归因于同一问题。
- 计划交付：一份总计划和节奏核心、Telegram 执行器、渠道与工作台三个专项，列明接口、任务依赖、失败用例、未来验收命令及分批回退；所有实施步骤仍为未执行状态。已完成设计覆盖、类型与接口一致性自查，修正无天然事件的恢复入口、旧排期默认值来源审查和多事实成果统计。
- 文档检查：6 份文档、24 个相对链接和计划结构／隐私标识检查通过；产品验收、浏览器与 Temporal 回归尚未执行，不将计划用例写成已通过。
- 下一步：用户审阅实施计划；取得后续实施范围和执行方式后才开展开发。线上状态沿用前序排查快照，不假定最新主线已经部署；开工时重新核对。保留用户“暂时不修改线上”的约束。

## 发布记录

本轮无提交推送、PR、合并、部署请求、设备安装、线上重启、额度变更或真实社媒提交。


## 本地实施授权与阶段证据

用户已确认按计划顺序实施，范围仅本地开发和隔离验证；未授权推送、合并、部署、修改线上账号／策略或执行真实社媒动作。基线 GitHub main 为 `7614e602973644d09e4c648d28e83deda576f43f`。P0–T3 的测试日志保存在当前任务隔离目录，最终汇总以 V1 为准。

已完成判定修复、私有驻留循环锁修复、置顶缺口诊断、证据判定、来源迁移、事件等待和部分候选交接。未确定来源的原有限制继续保留。专用设备候选未签署、未安装，真实群规样本的缺口分类仍需未来获准后的观察，不能把合成测试当作线上业务恢复。

### 设备兼容与回退边界（新实现的数据契约）

| 应用状态 | 执行器 | 行为 |
| --- | --- | --- |
| 未设置新模式 | 原基线或新执行器 | 保留既有约束；新执行器只报告能力 |
| shadow | 新协议且同策略版本 | 记录新旧判定差异；执行仍沿用原约束 |
| active | 协议 1 且同策略、绑定及配置版本 | 仅替换有证据的旧系统默认；用户值与未知来源保留 |
| active／paused | 版本不一致或暂停 | 拦住不兼容的新写入；保留原任务与未知回执路径 |

影子版本通过维护者入口冻结，设备回报同一版本后，才可能在未来另获授权的维护操作中激活。普通页面和模型不提供自动激活入口。应用回退不等于设备回退。旧应用／旧设备不识别新模式字段，不能依赖该字段冻结旧代码；换回旧版本之前，必须通过原设备已有的只读执行设置冻结受影响的新写入，独立回读确认，再恢复原字节。保留兼容 Worker、账本和原回执路径，不回滚业务账本或授权快照。

当前本地设备原摘要：`803ae310786594563464548ce4dbd837a79a444124320743d7b53cbcbdfbad2d`；候选摘要：`cd6d9654817eefd3a77b681ae9f7d6eeb90b48cf915b53219e2e9163d36b22d0`。受控本地清单 `.state/cadence-device/manifest.json` 记录各基线模块和共享 helper 摘要。后续若修改 helper，必须重建候选并重跑专项，再在最终交付证据中更新摘要。

## 本地交付验收（V1）

当前范围是本地代码、合成页面和专属数据库／Temporal。没有实号执行、真实效果样本、应用主线 CI、生产设备安装或线上恢复验收。PR 快检和精确 main 的完整应用验证属于未来交付阶段，不能用本地报告替代。

### 设计第八节逐项对应

以下函数名均可在本需求测试中检索；原始输出与专用候选留在受控 `.state/verification/`、`.state/cadence-device/`，不提交账号或群聊资料。

| 设计验收场景 | 本地行为证据 | 边界 |
| --- | --- | --- |
| 合格机会不因旧系统默认停止 | `test_old_default_join_does_not_consume_reply`、设备 `test_mixed_version_fences_only_new_writes` | 仅来源已证实的系统默认；现网旧值尚未证实者保留 |
| 同日入群后答疑、同题防重 | 设备 `test_current_task_join_still_counts_toward_owner_combined_limit`、公共 `test_product_community_join_history_contextual_reply_and_lost_receipt` | 原操作记录不清空；不新增答疑模式 |
| 用户额度、时段和期限重验 | `test_owner_combined_limit_is_preserved`、`test_selector_honors_current_account_deadline`、`test_future_target_uses_absolute_deadline_not_poll`、既有 calendar 合约 | 不提高用户值；时间戳必须有时区 |
| 明确平台冷却及多限制 | `test_max_deadline_keeps_all_constraints`、`test_account_feedback_applies_to_each_target_but_not_old_binding` | 合成结构化平台反馈，不声称取得真实平台限流样本 |
| 普通页面错误不冒称限流 | `test_raw_text_is_not_platform_rate_evidence`、既有故障隔离合约 | 工程错误保持原错误来源 |
| 失效、撤权、暂停和换号 | `test_submit_revalidates_managed_account`、`test_resume_revalidates_full_scope`、原生账号门禁回归 | 恢复必须新核对，不复用旧账号权限 |
| 半提交、重启、重复事件 | 浏览器 `test_channels_deliver_and_credit_only_first_arrival`、Temporal `test_restart_keeps_tick_and_deadline`、重复信号及新旧历史重放 | 原回执确认时原子写入唤醒；不重新创建外部动作 |
| 中文简介及完整置顶引用 | `test_substantive_description_is_not_space_dependent`、`test_read_pins_reference_does_not_invalidate_complete_evidence`、设备真实 `rule_verdict` | 原主题和禁自动化保护保留 |
| 前台轮读、备用列表及外部条款 | 设备 `test_actual_carousel_completes_only_after_returning_to_anchor`、`test_pin_failure_has_specific_stage`、`test_identity_change_during_rpc_never_falls_back` | 前台完整循环与外链缺口、原生完整／缺条／身份变化均用实际读取函数；原生 32 条上限保留，未增加翻页能力 |
| 单目标缺口不拖住其他工作 | `test_one_target_gap_does_not_park_another_target_deadline`、`test_known_action_scope_blocks_only_its_account` | 同一目标的时间和条件限制同时保留 |
| Telegram 慢查询、Reddit 独立准备 | 设备 `test_lock_device.py` 实际驻留循环锁竞争；`test_channel_preparation_isolation.py`、联合双渠道浏览器闭环 | 原行锁失败用例先失败再通过；提交门禁仍重新核对 |
| Facebook 连接、启用、授权分离 | `test_login_does_not_authorize_managed_scope`、`test_managed_scope_does_not_enroll_sibling`、UI 精确账号与冲突回读 | 一个账号的操作不默认扩大到同平台另一个账号 |
| Reddit 来源或目标缺口 | `test_preparation_continues_without_source_permission`、`test_internal_pack_cannot_become_validated_opportunity`、`test_source_and_business_gaps_both_remain_visible` | 内部资料不成为已观察机会，不读取未获准来源 |
| 零合格结果／无可答问题 | 既有发现空候选、答疑无匹配及成果投影测试；UI 零机会／未回复状态 | 入群确认和回复确认分别统计，合成结果不计真实获客 |
| 版本混合与回退 | 设备四种协议状态、未知回执测试；`test_shadow_version_is_the_version_activated`、Temporal 旧历史重放 | 协议数据状态模拟与原基线模块装配均已区分；没有声称运行过每种旧应用二进制组合 |

### 回退执行顺序（仅供未来获准后使用）

1. 本轮候选均未投入使用，本地异常直接保留失败记录和原分支，不修改生产。
2. 未来若新策略仍在 shadow，继续原约束，只替换有独立证据的程序缺陷；安装不自动激活策略。
3. 若 active 后异常，冻结受影响账号的新操作，继续原回执和内部准备；不恢复旧业务账本或默认日额度快照。
4. 必须换回旧应用／旧设备时，先用旧实现认识的只读执行设置冻结写入并独立回读，保留支持已存在 Temporal 历史的消费者，再恢复精确原字节和模块，重新比对摘要。
5. 应用切换、设备安装、策略激活分别登记与回读。部署前重新核对真实应用 SHA、设备摘要、未入库定制与在途发布归属，不覆盖其他需求。

### 首轮冻结证据（最终隔离核验见收尾）

- 基线：GitHub main `7614e602973644d09e4c648d28e83deda576f43f`；验证代码与用例提交 `441b22c4e5a8b83f2096099003297c11395b9710`。最终审查或文档提交另行记录，生产应用仍以设备独立核验为准。
- 专属本地 PostgreSQL `127.0.0.1:51083`，专属 Temporal `127.0.0.1:51084`；测试新建数据库和合成账号。Python 3.12.14、Temporal SDK 1.34.0、Pydantic 2.14.1、Psycopg 3.3.6、Camoufox 0.5.8／原生构建 `official/156.0.1-beta.36`、Playwright 1.62.0，Next 16.4.3／React 19.3.1；无新增项目依赖、表结构或队列。
- 联合回归：浏览器和 Temporal 必验开关均开启，**316 项通过、0 跳过**。清单 `.state/verification/cadence-test-files.json`，报告 `.state/verification/cadence-combined.xml`。首次大套件 314 通过、两项旧用例要求返回字典完全相等；按计划新增字段的契约更新这两项断言后，整套重跑通过，没有移除门禁断言。
- 前端：`npm --prefix apps/web run typecheck` 与 `npm --prefix apps/web run build` 通过。`scripts/verify_cadence_ui.py` 最终 **15 项通过**，桌面、390／320 窄屏和键盘操作已核对；合成账号用量为 0，外部请求为 0。最新报告 `.state/verification/evidence-cadence-ui/caf04eb440/report.json`，桌面及窄屏截图已检查。
- 代码检查：全仓 `ruff check .` 与 `git diff --check` 通过。专用设备独立装配与最终全分支审查的结果在下方收尾记录，不以通用应用通过代替设备证明。

可重跑命令（受控本机目录中的包装器先装载本任务隔离环境，避免误读其他目录的配置）：

```text
LEAD_WORKFLOW_TESTS=1 LEAD_BROWSER_TESTS=1 .venv/bin/python .state/verification/cadence/run.py -m pytest -q <cadence-test-files.json 中列出的文件>
.venv/bin/python .state/cadence-device/build_cadence.py
.venv/bin/python .state/verification/cadence/run.py -m pytest --noconftest -q .state/cadence-device/test_device.py .state/cadence-device/test_cadence_device.py .state/cadence-device/test_lock_device.py .state/cadence-device/test_pin_device.py .state/cadence-device/test_protocol_device.py tests/test_cadence_selection.py tests/test_community_rule_evidence.py
.venv/bin/python scripts/verify_cadence_ui.py
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
.venv/bin/ruff check .
git diff --check
```

不能把上述本地合成测试作为平台实号通过、线上工作恢复或获客效果的证明。当前现网旧合计额度、回复间隔、原生搜索冷却的来源尚未证实者全部保留；未来有可信来源记录并完成版本回读后，维护者才可预览和激活对应迁移。

### 首轮 V1 记录（独立审查前）

设备候选摘要 `4f9e9a6051d4699a2fb056a30d6af82f92908e0bdadd443fb4ead11c0255d4ab`，独立装配测试 **47 项通过、0 跳过**，报告 `.state/verification/cadence-device.xml`；候选、原始设备模块和公共 helper 的字节摘要再次核对一致。真实前台读器补充了结束时账号／目标复核，发生变化时整次读取失败，不把跨身份资料当作已完成群规证据。候选未签署、未安装。

完整日志已保存在 `.state/verification/cadence/`，包括原始失败用例、重跑结果和环境清单；`verification-summary.json` 固定了 316／47／15 的结果。三组存在重叠，不合并为不重复用例数。浏览器 SDK 的代理提示和基线脚本的转义提示保留在报告中，没有通过隐藏告警改变验收结论。最后一次全分支独立审查及后续重要修复另记在本节之后。

## 最终独立审查与本地交付

冻结应用／用例提交：`f6aa9fe96d5d0b5898d7ac91c691077215bf372b`。设备候选：`cd6d9654817eefd3a77b681ae9f7d6eeb90b48cf915b53219e2e9163d36b22d0`。设备仍未签署、未安装；新节奏未在生产激活。

一次全分支独立审查提出四项重要问题和一项轻微问题。账号混用等待原因会直接影响恢复判断，因此将轻微项升为重要，五项均在同一次修复中处理，未再发起第二次审查。每项都先复现失败，再验证通过：

| 修复 | 失败到通过的证据 |
| --- | --- |
| 最初单条 DOM、manager 历史和旧完整列表均检查附件／隐藏链接 | 设备 `test_initial_pin_paths_preserve_media_gaps` 四项失败后五项通过；`test_legacy_full_list_cannot_clear_external_gap` 两项失败后三项通过。完整列表、轮读和结束时身份复核仍有效 |
| 原回执的 action 和 task 屏障均落盘后才唤醒 | 浏览器 `consume_before_task_completion` 先在旧事务间隙重查并挂起，再断言任务终态同事务发出新的唤醒；原代码失败，修复后通过 |
| 原发现批次累计上限不因续页／重启重置 | 公共及真实装配的私有 `test_partial_assessment_preserves_cumulative_limit_after_restart`，累计四个不得突破配置三个；清空暂存列表的重启场景也受已核验检查点约束 |
| 今日确认事实与当前未知回执分开统计 | 完整 `/plans` 返回的 `test_api_execution_includes_live_confirmation_and_pending_stock` 覆盖 running／reconciling、今日／昨日确认和跨日未决存量；结束评估继续按完成时间统计 |
| 账号只显示自己的目标条件，真正平台级约束才共享 | `test_account_waits_do_not_inherit_another_account_deadline`，两个均获准账号分别显示条件等待和时间等待 |

最终结果：**应用联合回归 321 项通过、设备独立装配 57 项通过、界面检查 15 项通过，必验项无跳过。** 三组有重叠，不将数量相加冒充独立用例数。类型检查、前端构建、全仓 Ruff 和补丁空白检查通过。最新界面报告 `.state/verification/evidence-cadence-ui/1fbe8cca1a/report.json`，320 窄屏截图已检查。完整日志、XML、环境和审查处置保存在 `.state/verification/cadence/`。

### 本地队列配置纠正

最终核对发现，任务自有配置副本中的 `temporal_address` 仍指向已有的本地开发队列，项目不支持此前假定的环境变量覆盖。初版旧工作流用例在该本地队列产生过测试记录；C3 专项则一直使用独立地址。首轮 316 项报告不能作为“所有工作流均隔离”的最终证据。

只修正了本任务自有的长／短状态目录配置，没有停止或清理已有开发服务及其数据。容量超时在旧地址可独立复现，改为本任务队列后通过。最终 321 项命令在导入测试前断言实际 `CONFIG` 和数据库地址分别为 `127.0.0.1:51084`、`127.0.0.1:51083`，在该独立环境完整重跑通过。重跑包装器也增加了配置一致性断言。生产应用、设备和账号未修改。

### 复核、保留与未来发布边界

- 只保留本地分支和独立目录；本需求未推送、创建 PR、合并或部署。
- 本任务的测试服务在验证后关闭，数据和报告保留。需要重跑时先执行 `.venv/bin/python .state/cadence-local/services.py start`；该脚本只管理本任务路径与端口，已演练停止／重启并核对。结束后用同脚本的 `stop` 关闭。
- 设备重建仅使用 `build_cadence.py`，离线摘要验证使用 `.venv/bin/python .state/cadence-device/verify_cadence.py`；不要使用该目录中前序任务的旧构建／核验脚本。
- 未来交付仍须按每个故障的正常 PR、精确 main 的 CI 和同轮产物执行。设备适配／构建变更需去敏后进入正常 PR 和独立设备证明，不从当前私有文件直接覆盖生产；真实版本、定制、来源许可和未决动作须再对账。
- 真实社媒恢复、业务效果、生产签署／安装／实际回退、最新 GitHub main 放行及全部旧应用二进制组合仍未验收。不得由本地通过推断可激活混合版本，旧版本回退先用其已认识的只读设置冻结。

### 实施判断留档

完整十四项判断及误判成本保存在受控 `.state/verification/cadence/execution-decisions.md`，原审查范围与五项处置见 `final-review.md`。没有延期轻微项；原轻微项已升级修复。以上范围结论不授予未来发布权限。

## 后续生产交付授权

用户随后明确授权：“按已确认方案合并部署，并在条件满足后激活策略。”本次授权涵盖该需求 PR、检查、正常合并、现有家庭生产环境部署、设备适配交付及条件满足后的策略激活。原“仅本地”约束作为历史记录保留，不再阻止本次交付；来源许可、账号授权和未明来源限制仍按原门槛处理。

交付开工已核实任务 HEAD `6d4424b31c829284b50b43abc65ba7f15d2b6762`、工作树干净、GitHub main 与生产应用均为 `16e1551bf832ddcaa95fea0768c928e814c0f213`。主线和现网已完成独立渠道框架迁移，因此将本需求适配到现行框架，保留两个渠道各自进程、资料、出口和受控私有适配器；此前独立驻留候选不会直接覆盖现有新框架。

### 现行框架整合与发布候选（2026-11-12）

本轮实际整合基线为 `fd4d6ac0b31ae56c8919892b8b6b77dda0e4386d`（运行代码与已交付 `16e1551` 相同，另含上一需求的交付记录），整合提交 `6d200b29df808087c514b5c4aa31ceeb00872c9b`。保留双方流程说明与待办，未覆盖其他任务工作树。当前规则以一个需求一个 PR 交付，原故障拆分提交保留。

- 整合公共针对性回归 **156 项通过**；真实 Temporal 历史与新结果接入、旧历史重放、并发隔离 **14 项通过**；仓库检查及凭据扫描通过，PR 契约 **31 项通过**，前端类型检查通过。
- 对仍在运行的旧渠道时钟，新增协议化 Activity 结果可让同一工作流采用持久等待；旧历史没有该字段时继续原命令。实际旧工作流升级用例先复现继续轮询，再修复通过，避免只能等到整轮 continue-as-new 才生效。
- 原独立驻留候选不安装。已按正式执行器回执核对现行 Telegram 适配器全部 11 个文件，再把本需求行为接入其私有命名空间；公共模块、账号绑定、出口、本地模型限制和业务策略桥接保持原归属。
- 新一轮聚焦兼容审查指出：私有候选构造器未同步公共实现中的精确委托选择与入队前复核。已在**真实私有** `lead_channel_adapter.community_runtime.community_cycle` 上复现两个有效委托选错、撤权、规则变化三个失败，补齐同一检查后通过。不是用公共构造器代替设备验证；修复后私有完整组 **33 项通过**。
- 冻结的私有适配器文件清单摘要为 `6195d7bfd754e9b8b9f89b60b7aecce736237971332d2b9a3e4474ebe26e4250`；源码、文件级摘要和测试日志仅留受控 `.state/cadence-release/`。公共 helper 必须来自本轮精确 main 的同轮镜像，不安装公共模块副本或恢复旧解释器覆盖。
- 只读条件盘点：Facebook 连接但未启用、没有当前账号有效运营范围；Facebook 和 Reddit 发布能力仍为未核验／许可等待。Telegram 与 Reddit 现有账号运行设置保留。没有未知提交、社区发送或接管需要清账。
- 当前原始策略没有新参与额度配置，设备报告的 1 次／30 分钟及旧规范化审计不足以证明参数来源。不会基于缺字段、默认值相同或缺少审计直接登记默认值来源；满足应用、执行器、绑定和策略版本核对后，激活也继续保留全部未明来源限制。平台许可不会因交付自动改变。

后续放行仅接受合并后的精确 main 完整应用 CI、同轮产物及应用和两个实际执行器独立回读；以上本地整合结果不替代它们。
