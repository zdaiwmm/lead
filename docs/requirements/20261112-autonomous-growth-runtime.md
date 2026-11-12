# Lead 自主经营：审计修订后的详细方案与实施计划

版本 V2，2026-11-12；实施与交付中。本文件同时承担需求、规范、任务、测试和交接；替换 V1 的冲突条款，不并存两套规范。

## 1. 目标、范围与审计结论

### 1.1 已确认目标

用户提供产品、连接渠道后，产品内部的经营 Agent 自主识别需求、选择并推进获客实验，根据真实产品结果调整。用户不负责日常编排，不依赖 Codex 在产品外持续制定策略。

一个项目一个经营决策归属；渠道、研究、素材、账号和结果观察独立并行。复用必须满足新契约，不符合则改造/替换。用户最新明确：删除日计划、周计划，以最小功能实现目标。

产品保留目标/产品资料、账号连接与主动控制、实际工作、真实结果、必要本人事项。实验是内部验证记录，不增加用户必须维护的实验管理流程。单任务时间、平台等待与观察窗口仍为必要运行机制。

本轮仅审计和修订本地设计，不实施业务、安装依赖、修改生产或执行社媒动作。未来交付引用 [交付规则](../SMALL_FIX_RELEASE.md)，不继承其他任务发布权限。

后续实施授权：用户已明确“按照方案实施并完成验证合并发布”。本需求现获本地实现与验证、提交/推送、一个 PR、检查后正常合并和现有生产环境发布授权；由 Agent 固定记录候选 SHA 并完成独立回读，不重复询问同一范围。前述仅设计边界作为历史阶段记录保留。实号效果不能用测试垃圾内容制造，平台规则和实际用户范围仍须核验。

实施中记录：已建立任务专用 PostgreSQL/Temporal 环境，业务实现和本地集成闭环已完成。固定任务、独立命令组、账号意图、工具边界、结果与费用、最小界面和迁移均已接入。正式交付证据随第 14 节更新。

已发现并修正的实施约束：复用草稿不能受原 content_items 的必填计划/目标阻塞；actions 改用项目内内容外键及目标适用性触发器，保留跨项目与特定目标限制。只读运行角色无法直接 FOR SHARE 锁意图/路由，改用仅返回本项目行的受控读锁函数，仍不给运行角色写权限。终提交按控制、账号、意图、路由、资源、动作顺序取得兼容读锁，避免与账号命令的锁顺序相反。两个独立本地浏览器环境已证明提交区间重叠及单账号暂停顺序；外部平台发送为零。

模型调用在实际费用账本中保存同任务调用身份与结构化答复，未知计费不以重投重新调用。恢复探测仅验证原失败条件；模型最小探测累计上限暂定 70,000 token，仍受项目预算限制，属于工程恢复初值，不是营销配额。该值及所对应错误类须在完整恢复验收中再次核对。

文档查询补充：React 19.3.1 已通过 Context7 核对；Playwright 1.62.0 的精确索引不可用，已核对实际安装签名和 [官方浏览器接口](https://playwright.dev/python/docs/api/class-browsertype)、[隔离上下文接口](https://playwright.dev/python/docs/api/class-browsercontext)，未升级依赖。

### 1.2 已核实依据

- 仓库 zdaiwmm/lead；目录 /Users/zhouding/ss/lead-autonomous-growth-design/lead agent；分支 codex/autonomous-growth-design-20261112。
- GitHub 实时 main、origin/main、任务 HEAD 均为 ccc5da3078d7dd2e6ca783cbb8c3e76fabf4bf9c；规则取自该提交 AGENTS.md 与 SMALL_FIX_RELEASE.md。共享目录其他任务内容不变。
- V1 为 826 行，SHA256 709f8b58e410bad5b76deec82683b9c5bba723a00e68dd23951819fd278b46e9；原文留本任务 .state/design-audit-20261112/pre-audit.md，仅供审计对照。
- 账号专项输入 docs/requirements/20261112-all-channel-auto-start.md，522 行，SHA256 95a2cbc9e0c7d988d2123ba7f32494d79f3d3102cd13ad4aa98deaf8d9cebfb5。已整合设计，不修改另一工作树；代码开工前核对最新差异与唯一实现归属。
- 依赖不升级：Temporal 1.34.0、Pydantic 2.14.1、Psycopg 3.3.6、FastAPI 0.162.0、Camoufox 0.5.8、Playwright 1.62.0；前端 lock 为 Next 16.4.3、React 19.3.1、TypeScript 5.9.3。
- 本轮发布前独立回读：应用/Telegram/Reddit 均为 84f6184fd647e83ce353c97ba4e5c2d558507e7a，schema 0030，未决 actions 为 0；这是发布前状态，不能作为候选版本证明。

### 1.3 审计发现：V1 不能原样实施

以下是设计缺陷/缺失契约，不是本轮复现的生产故障。对应规范、任务和测试已同时修订。

| ID | V1 允许发生的反例及根因 | V2 修正 | 验证 |
| --- | --- | --- | --- |
| R01 | 全局版本/证据游标使 A 换号导致 B 的有效提案一起过期 | 独立命令组按实际作用域接收；全局暂停才整体阻止新写入 | C01 |
| R02 | claim_work(scope) 与固定 TaskRef 工作流可各自选择任务 | 工作流只领取自身 TaskRef，outbox 唯一投递，不再选业务 | C02 |
| R03 | 14 类任务、准备/提交/回查逐层串联，状态与事件过多 | 七类顶层任务；渠道操作同任务内检查点，原动作身份不变 | C03 |
| R04 | 全部实验先要求激活/入口/分发字段，无样本时无法研究 | 最小实验记录＋按实际动作校验必需字段 | C04 |
| R05 | 无效提案后等 provider_recovered，但该事件未必发生 | 输出错误/服务故障分别处理，等待明确生产者、检查和出口 | C05 |
| R06 | 每次投递三次，换任务/进程后重新计数，仍可无限重试 | 同故障链持久累计，只有真实条件变化才开新周期 | C06 |
| R07 | resume_on 没有生产者，或补偿只查不推进 | 每个等待有恢复动作/责任、check_at、升级或终止 | C07 |
| R08 | 租约过期后拒绝旧结果，也可能丢弃真实迟到回执 | 拒绝迟到控制写入，但接收原动作可信证据 | C08 |
| R09 | 任意字段哈希依赖和快照升级复杂，且冲突于不可变动作 | 明确产物/事实引用；未提交重验，已建动作不改意图 | C09 |
| R10 | 统一 Agent 排他锁可能串行化所有完成/终提交 | 接受决定短锁；普通完成局部锁；终提交控制兼容读锁 | C10 |
| R11 | 结束实验、费用预留、取消与资源释放未闭合 | 实际调用前预留；明确未花费用释放，未知费用保留 | C11 |
| R12 | 单任务有界，改写问题/换来源却可无限花钱产稿 | 同问题累计投入和无进展轮次，达到边界结束/转向 | C12 |
| R13 | 日周计划兼容视图继续产生成本；工程范围又捆绑所有渠道 | 删除产品功能；兼容只服务具体旧消费者，逐范围独立就绪 | C13 |
| R14 | 自修复只写重试，模型评估无明确判分/失败出口 | 有限恢复、自纠正与能力缺口分清；结果分支可检查评估 | C14 |

## 2. 最小架构与职责

| 部分 | 唯一职责 | 不负责 |
| --- | --- | --- |
| 经营 Agent | 产品理解、机会比较、实验决定、业务复盘 | 授权、技术放行、任务执行终态 |
| 通用运行器 | 固定任务投递/执行/等待/恢复、资源预算准入 | 另选受众、轮换内容、经营排期 |
| 工具 | 有界研究、内容/素材、账号/目标、渠道动作、产品检测 | 建用户意图、改目标、独立派活 |
| 事实账本 | 保存项目/实验/任务与证据、回执、业务事件、费用 | 另起调度、根据模型自述制造成果 |
| 用户入口 | 同一命令服务与只读结果 | 再维护启用/授权/日周计划 |

一项业务工作一份任务状态、一条当前等待，外部动作只有原 operation_key。不为每次页面点击建任务。经营模型与工具在事务外运行；一个决策归属不等于一条串行执行队列。

保持 Web/API/Worker/浏览器/数据库部署，不新增微服务、事件总线、多 Agent 协商或通用工作流语言。物理渠道执行器可独立，但没有独立经营循环。

## 3. 数据与接口：权威来源和最小状态

### 3.1 持久数据

复用 agents/business_profiles/agent_revisions、tasks、actions/action_attempts/publish_preparations、knowledge_documents/chunks、assets/content_items、analytics/pixel/conversion、outbox/inbox、budget/usage、audit_events。

只新增三类实体；账号专项不能再建平行版本：

| 数据 | 字段及用途 |
| --- | --- |
| growth_experiments | id/tenant/agent、revision、state(active/observing/closed/superseded)、business_profile_id、objective_revision、question_key、variation_key、specification、conclusion、时间 |
| account_operating_intents | account_id 唯一当前记录，tenant/agent、revision、desired(running/paused/revoked)、source_kind/source_event_ref、scope（含排除项）、时间 |
| profile_execution_routes | profile_id 唯一当前记录，tenant/agent、generation、executor_ref、transport_ref、时间；无启用/审批开关 |

实验不存第二份任务状态和费用镜像，运行进展从原账本查询。实验 specification 业务修订才递增 revision；普通回执/心跳不使提案过期。实验唯一键为 (tenant,agent,question_key,objective_revision,variation_key)，服务器派生，不使用模型标题。

tasks 增加可空 experiment_id 复合外键。input 固定任务意图与 task_generation；result 分 runtime（租约/等待/恢复/检查点）和 output（产物/证据），条件 patch 更新。tasks.version 仅作行 CAS，不等于身份/任务执行代次。

新表同时完成复合 FK、FORCE RLS、角色权限、新安装 SQL 和增量迁移。意图仅由用户命令或已验证连接完成函数派生；runtime 不能伪造源事件。目标 grant 引用意图，是执行证据，没有另一个用户开关。

### 3.2 版本与不可变事实

目标/产品事实按实际引用重验；不相关 UI 设置/别名和其他账号事件不使所有任务失效。intent_revision 只影响该账号/目标；binding_version 仅真实主体/环境关系变化，同号重登不变；route_generation/session/lease_epoch 分别是路由、启动和持有者。

task_generation 为任务执行归属代次，Activity 重试不变；attempt_token 是一次领取。迁移 engine generation 只防新旧双写，不成为正常用户门槛。

actions 的 operation_key、账号/目标、正文/素材、grant 和快照不可变。未建动作可重验任务；不适用的 prepared 取消并由明确 supersedes 关系替代；dispatching/unknown 只查原动作，不升级快照后重发。迟到真实回执永远归原动作。

### 3.3 模块与公共接口

新 growth 包仅 contracts.py、store.py、agent.py、runtime.py、tools.py、outcomes.py、views.py、migration.py。工作流定义/注册放现有 workflows.py/worker.py。纯校验属于 contracts，存储变更属于 store，经营判断属于 agent，不加 rules/repository 多层转发。

账号专项保留 channel_contracts.py、account_intents.py、execution_routes.py、account_connection.py；若已合入复用同一定义。没有 account_work 协调器或第二个账号任务源。

    Scope = {tenant_id, agent_id}
    TaskRef = {scope, task_id, task_generation}
    ArtifactRef = {kind, id, revision, scope}
    EvidenceRef = {kind, id, scope, observed_at, trust, producer_version,
                   source_ref, expires_at?}
    Command = {command_key, group_key, kind, scope_ref, expected_revision,
               goal_ref, required_refs[], payload}
    DecisionProposal = {context_key, rationale, commands[]}
    DecisionAcceptance = {decision_task_id, accepted[], rejected[], consumed_triggers[]}
    TaskSpec = {kind, purpose, experiment_id?, scope_ref, required_refs[],
                payload, completion_rule, budget_limit_ref, review_at, dedupe_key}
    Wait = {reason, scope_ref, producer, resume_on, check_at,
            recovery_action, episode_root, escalation}
    WorkClaim = {ref, attempt_token, lease_until, operation_ref?}
    WorkResult = {outcome:completed|waiting|failed|unknown,
                  artifacts[], evidence_refs[], wait?, checkpoint?, costs}

    build_context(scope:Scope, trigger_refs:list) -> DecisionContext
    decide(context:DecisionContext) -> DecisionProposal
    accept_commands(scope:Scope, proposal:DecisionProposal) -> DecisionAcceptance
    submit_work(c, spec:TaskSpec) -> TaskRef
    advance(ref:TaskRef, attempt_token:str|None) -> WorkStep
    execute(claim:WorkClaim) -> WorkResult
    finish(claim:WorkClaim, result:WorkResult) -> FinishResult
    summarize(scope:Scope, experiment_id, as_of) -> OutcomeSummary
    recover_due(scope:Scope, now, cursor) -> RecoveryPage
    render_growth(scope:Scope) -> GrowthView

DecisionContext 包含 goal_ref、引用事实、当前实验、相关新证据、能力/资源和 triggers；不传凭据或全部历史。WorkStep={ready_claim|wait|terminal}；FinishResult={accepted_control,evidence_recorded,next_check,wake_refs}；RecoveryPage={checked_refs,next_cursor,next_run}。required_refs 仅明确产物/事实，不是任意字段哈希依赖语言。时间有时区；scope/路由从服务器引用解析。

Command.kind 固定为 experiment.create/revise/close、task.create、target.propose。group_key 只作关联，服务器以同一实验及其子任务归组，禁止模型把存在依赖的命令谎称独立；创建实验使用提案内临时 key，接收事务分配实际 UUID 并解析子任务引用。target.propose 是有依据的指标建议，不直接改用户目标。跨组依赖必须等其引用真实存在，否则只拒绝该依赖组；独立组不回滚。

## 4. 经营 Agent：能启动、能纠正、不会无限规划

### 4.1 启动与提案接收

项目创建即产生唯一 product_research（research 类）任务，只需用户已给网址/介绍；账号连接并行。这是明确的初始事实获取，不预制营销文案。可研究缺口自行补齐；只有确需用户决定的关键歧义集中提问。

每项目最多一个运行中的经营模型调用，超时/失败结束原领取并记录恢复，不能留永久 pending。已有任务不等模型结束。

提案按命令组接收：同一实验变更及子任务原子，独立组分别校验。A 换号仅拒绝 A 组；B 有效组接受。全局暂停、跨租户、结构无法解析才整体拒绝。一次定向修正仅处理 rejected，不能重做 accepted。

证据游标表示读过事实，不作全局版本锁。按 goal_ref、实验 expected_revision、账号/目标引用检查实际变化。普通访问和别的账号心跳不使它失效；关键新结果最多触发一次合并后的后续判断，不递归全量规划。

### 4.2 产品理解和激活

事实分别标 owner_asserted、observed、server_verified、hypothesis；至少说明服务谁、触发场景、替代办法、产品承担什么及依据。

目标引用 {connection_id?,event_name?,definition_revision,unit,newness_rule,trust}，支持自定义事件与 user/pair/order/organization。缺激活定义可先做需求/分发学习，不伪造 definition 或改最终目标为点击。临时学习口径标 unknown；宣称激活须满足已版本化的成功条件及可信事件。

用户已有口径优先，目标变更经统一项目命令落库，不逐实验增加指标审批。产品入口失败只阻止导流该入口，独立研究、账号及其他健康入口继续。网站声明不等于实测证明。

### 4.3 实验字段按阶段要求

通用必填：question_key、当前问题、假设及来源、下一可验证行动、学习信号、累计投入边界、复查/结束条件。直接 active，无 proposed 审批状态。

- 产品/需求研究不要求登录、发布位置、健康入口或激活事件。
- 外部分发才要求实际身份/目标、规则、内容、入口和动作能力。
- 转化结论才要求目标版本、新客口径、可信事件及归因窗口。
- 测量未接通时报告学习进展与缺口，有限探索，不能宣称获客或无限引流。

一次实验改一个主要变量，多个独立实验可并行。观察锚点为实际公开/到站/邀请等事实；未公开不启动转化时钟。无历史依据期限标临时假设，不当平台定律。

Agent 必须给断点、依据、最接近替代项、行动与反证/停止条件。结束结论 supported/refuted/inconclusive/superseded；取消失去目的的未提交工作，原回执和费用继续。

### 4.4 机会和业务自纠正

工具获取原文，Agent 按具体未解需求、适配、可分发/测量、时效、成本比较。群名/粉丝数/搜索摘要不单独构成机会。需求来源与发布位置可不同；可读不等于可参与。

复盘：缺需求→改场景；缺位置→验证分发；待审/覆盖未知→核对等待；有覆盖无到站→查受众/表达/入口；到站无激活→查体验；成功无回传→修测量；激活无复用→查持续价值。每轮改一个主要变量。

新研究说明相对已有事实的新问题/条件。重复内容、重命名、心跳、重开任务、改写结论不算进展。

### 4.5 模型故障

结构错误最多一次定向修正，同 decision_task 与预算记录；仍失败记 invalid_proposal，释放领取，进入有限恢复。业务反证是正常结果，不作技术重试。

区分网络/限流、凭据/余额和输出质量；无合法决定时不拿模板继续发帖。仅可使用已登记且符合现有预算/数据边界的备用模型配置，不自动新开供应商或绕过费用。

## 5. 通用运行：少任务、短依赖、真并行

### 5.1 七类顶层任务

| kind | 内容与内部检查点 |
| --- | --- |
| decision | 相关事实→模型→独立命令组接收 |
| research | product/demand/source 的读取归纳，可释放资源续做 |
| content | 草稿/质量/所需素材，独立可复用素材才分任务 |
| account | connect/observe/discover；目标发现可独立 |
| channel | prepare→commit→pending/reconcile，同任务与原 operation_key |
| observe | 到期摘要和复盘信号，不为每条 PageView 建任务 |
| recover | 限定资源/故障检查，不创造业务方向 |

点击、上传、回查不是新顶层任务；长任务以检查点释放资源。仅独立产物或独立生命周期才拆分。

复用 tasks 的 planned、queued、running、waiting_user、reconciling、succeeded、failed、canceled。failed 为当前恢复周期终止，重复投递不能自动复活。succeeded 是任务产物完成，不等于获客。

### 5.2 投递与执行只有一个归属

tasks/outbox 同事务。Worker 把 work.ready 启动为固定 ID growth-work:<task_id>:<task_generation>；重复启动使用当前执行，终态不重跑。经营协调 ID growth-decision:<agent_id>:<generation> 只合并经营触发，不调度其他任务。

WorkItemWorkflow(ref) 只 advance(ref)，不得领取同项目其他任务。advance 短事务检查本任务条件/资源并返回 ready/wait/terminal；ready 后经 Activity execute(claim) 调用对应工具处理器，再经 finish 收结果；wait 用持久计时/信号，不占执行槽；terminal 结束。不把每个步骤再建工作流，没有另一候选扫描器重做策略。

经营协调器仅合并触发并保证一个未终结的 growth.decision 任务；不直接再调用一次模型。decision 也是上述普通任务，唯一开放工作键防重。其租约/失败由同一运行器恢复；任务终态后仍保留未消费关键触发，再建立必要后续任务，不能因一次失败留下永久忙标志。

数据库为事实源，工作流保存等待/恢复；全部 SQL/模型/浏览器 IO 在 Activity。ack 在服务接收启动/信号后；不能先写 inbox 已见再永久丢掉启动。信号提示重读事实，长历史 continue-as-new 携带任务引用/未消费触发并做重放测试。

### 5.3 依赖、资源和锁

依赖仅必需的明确产物，参考证据不成为硬屏障；拒绝环。禁止“全部渠道研究→全部素材→全部账号→统一发布”。

公开研究不等登录，文字不等可选视频，目标发现不等发布验证，观察不等所有发布，原回执不等模型。前置失败只影响实际依赖者；替代产物匹配契约，不重跑全部项目。

只保留真实互斥：Profile 租约、Worker 的模型/浏览器/渲染容量、预算原子准入。不建通用多资源抢锁框架。页面读取→释放可释放资源→模型归纳→重取并重验，使模型思考不霸占账号。

连接/控制、回执有保留容量；其他任务按优先级队列轮转、队列内先到期先执行。首项资源冲突不阻塞后项，等待不占活动线程，不跨渠道长锁。

经营接收短锁项目；普通完成锁本任务/动作。终提交控制/意图用兼容读锁（用户修改用排他锁），本 Profile/动作排他。禁止持业务锁等浏览器网络；最终点击屏障短且有界，超时为 unknown。不同 Profile 的提交可以重叠，并在测试证明暂停竞争顺序。

锁顺序固定为控制行→意图/账号（ID 排序）→本资源/动作→任务。控制读锁采用与其他控制读锁兼容、与修改冲突的行锁；终提交同事务不得再写 Agent heartbeat/汇总造成锁升级，进度在独立短事务更新。导航、上传、模型和网络回执全部在最终点击屏障外；已进入提交的动作可能先于暂停完成，暂停回执必须保留进行中/unknown，不能声称已撤回平台动作。超时不等于未提交。

### 5.4 迟到、取消与费用

领取保存 attempt_token、lease_until、last_progress_at；heartbeat 仅存活，不算进展。旧 token 不改新尝试控制状态，但原动作真实迟到回执仍经事实接收器入原账本并触发核对。取消/暂停后发现平台已生效也记真实事实，不复活新动作。

暂停/撤销取消本范围未提交工作；核对/结算继续。实际调用前才预留费用，等待依赖不先占满预算。取消释放已证明未花费用，未知费用保留，不能当可用额度重用。

## 6. 防停摆、防空转与有限自修复

### 6.1 等待必须有实际去向

非终态任务必须有有效执行租约或持久 Wait。Wait 必填 check_at、生产者/恢复动作和终止/升级出口；仅 resume_on 字符串不合法。本人/平台等待也有状态检查，但不能自动恢复用户暂停。

| 原因 | 实际恢复生产者/动作 | 到检查点后 |
| --- | --- | --- |
| 依赖产物 | 前置完成＋本任务查询 | 失败则终止该下游或请求替代，不等已失败任务 |
| 模型暂不可用 | provider 到期检查，每故障一个探测 | 尊重 Retry-After，无反馈用恢复退避 |
| 凭据/余额 | 配置/余额变更或必要本人操作 | 无变化不高频试 API，明确阻断 |
| 无效提案 | 一次定向修正；提示词/配置变化或一次受限重建 | 不重做已接受部分，无变化停止相同提案 |
| 页面/浏览器瞬时错误 | 本 Profile 有界重载/重新观察 | 无法确认则能力故障，经营选已有替代 |
| 缺适配/路由 | 安装/配置变更＋查询 | 无能力不反复调用，产品方处理 |
| 本人挑战 | 原 takeover 完成与会话观察 | 保现场和期限，仅影响该 Profile |
| 平台审核/冷却 | 原发布/回执核对 | 不重复提交，不猜冷却 |
| 预算/时段 | 当前周期/时段确定性到期 | 重验原限制，不提高额度 |
| 未知发送 | 原动作 reconcile | 无证据仍 unknown；有限核对后降低频率并明确责任 |

### 6.2 累计恢复，不按任务重置

RecoveryEpisode 存根任务 runtime.recovery，后续同因任务只引用 episode_root：
{fingerprint,root_task_id,attempts,repair_attempts,probes,next_check_at,last_condition_revision,status}。fingerprint 由工具/资源/错误/相关条件版本派生；换任务名、重启或新工作流不重置。

工程初值：无副作用业务尝试累计 3 次（2 秒起、30 秒封顶），输出定向修正累计 1 次；耗尽后 blocked，不再重跑业务。资源健康探测同作用域同时最多一个，退避从 5 分钟至最多 1 小时，真实 Retry-After 优先；通过同一 recover_due 推进，不新建业务任务。这些是工程参数，不是每日营销额度。

健康探测优先读已有执行器/网关状态或实际可用的服务健康接口，不假设模型商都有健康端点。缺少非付费检测时，最小 canary 必须从预先有限的 recovery_budget 预留；预算耗尽停止付费探测并明确等待配置/余额/人工修复。失败不重置业务次数或重开实验；探测真正成功才产生 resource.recovered 并恢复原任务一次。无效凭据/缺适配器没有高频探测。允许低频无副作用检查，禁止无限业务重试和无预算模型调用。

配置修复、身份恢复、适配版本更新或真实平台期限到期才能开新周期，保留历史计数。业务假设被否定由 Agent 改实验，不伪装成技术重试。

resource.recovered 必须证明原失败条件已解除，带 fingerprint、condition_revision 与证据；“浏览器在线”不能清除 layout_changed，“provider HTTP 正常”不能清除 invalid_proposal 或凭据错误。无关心跳不能重新解锁故障。

恢复限于本环境重连、原检查点续做、只读观察、已允许的备用来源、原动作核对。不能自行改权限/生产代码/账号、复制凭据或绕过挑战。未知结果不通过恢复重发。

### 6.3 两种进展与防空转

knowledge_progress：新且相关的来源/反证、有效产物、可执行位置。business_progress：确认触达、有效到站、可信激活、再次使用。均须证据；心跳、同文不同时间、改写结论不计。

同问题的任务/variation 累计投入。question_key 根据问题类型、目标和上游实验派生，变化措辞不重置；服务器拒绝无新事实或明确新变量的重复命令。

问题类型固定 product/audience/distribution/value/conversion/retention；根 key 由项目、目标版本、类型和上游根问题派生。受众/内容变体放 variation_key，不另开免费根预算。自动重开须引用未消费的相关新事实或真实条件版本；同内容只改变时间不合格。用户明确改变方向可建新根并保留原投入，模型不能自造目标版本。

实验必须有有限工作预算与观察窗口；无成本样本使用项目预算内的小试验上限并标 provisional，不能“未设上限”。无现金预算不投放/付费合作。

默认启动工作预算为当前模型配置单次最大预留的 4 倍，并取项目可用余额的较小者；项目总预算为 unlimited 也不能使单问题恢复/试验无限。额度按原始问题树累计，不因变体重置；已有明确用户实验限制优先取更严格者。这是可配置的初始工作预算，不是固定发帖数量。无付费模型条件仍可推进已允许的确定性读取/核对，不伪造智能决定。

初始 no_progress_round_limit=2：两轮成熟复盘都无指定学习或业务进展，关闭该路径，只能在原问题总预算内选已提出的可行替代。可行路径耗尽则明确“当前条件无可执行验证路径”和缺口，停止重复研究/产稿；新事实/条件/用户方向变化才重开。它是防空转工程初值，不是统计显著性或每日动作配额。

### 6.4 一个补偿入口

recover_due 复用现有 Worker 定时维护，每 60 秒检查 due 等待、过期租约、漏启动和进展逾期，每批最多 20、持久游标轮转；正常通知即时推进。用到期索引，不全表重建任务。

补偿仅推进原状态，不重置 episode/unknown 或全量重规划。自身失败保留进度，后续继续；进程存活由现有部署守护，不增加产品内“守护守护者”。

进展逾期先核对持有者/外部阶段；读任务可有限恢复，提交仅查原动作。不能保证任何条件都有客户，但必须保证有条件工作推进、无效路径结束、阻断清楚，不能以不停调用模型冒充自主性。

## 7. 浏览器、账号、权限和渠道工具的落地

### 7.1 连接自动启动，不额外串审批

    ConnectionRef={scope,profile_id,takeover_id,account_id?}
    SubjectRef={namespace,external_id,id_kind,aliases[]}
    ExecutionRef={executor_ref,transport_ref,route_generation}
    OperationContext={TaskRef,account_id,profile_id,binding_version,
                      intent_revision,control_version,ExecutionRef}
    SessionObservation={status:authenticated|signed_out|challenge|ambiguous|
                        temporarily_unavailable,operating_subject?,
                        login_principal?,acting_subject?,evidence_refs[],challenge?}
    TargetObservation={canonical_ref,acting_subject,role,operations,evidence}
    AdapterContext={restricted_page,expected_subject,target,OperationContext}

AdapterContext 留浏览器进程内，不跨 HTTP 传页面对象/数据库连接。适配器仅提供：
observe_session(ctx)、discover_targets(ctx)、inspect_target(ctx,target_ref)、prepare(ctx,content_ref)、submit(ctx,preparation_ref)、reconcile(ctx,original_action_ref)。
返回规范化观察/准备检查点/SubmissionObservation，后者为 confirmed|pending|unknown|not_applied 及证据，不自行写授权。

PreparationResult={preparation_ref,expected_hashes,target_snapshot,checkpoint,evidence_refs}；
SubmissionObservation={state,external_id?,external_url?,visibility:public|restricted|pending|unknown,evidence_refs}。
subject 字段类型为 SubjectRef。confirmed 只证明与回执匹配的操作已被接受，可见/审核状态单记；实际可见性证据成立才启动相应分发观察。

具体链路：
1. owner connect 命令为该渠道/账号分配持久 Profile 与明确路由，保存 purpose=login takeover 和 account 任务，写 outbox。不得正常登录就全局暂停其他 Profile。
2. 后端短活动观察身份；前端关闭不影响持久任务。导航/切换主体须等人工输入交还控制，读取当前 input_seq 并处理 IME 最后输入。
3. 观察在事务外，完成时复核 takeover/session/Profile/路由/绑定/输入序号。login-complete:<takeover_id> 唯一完成键，事务写身份、意图、交还控制及后续事件。
4. 新连接自动运行；同号重登只恢复认证，不覆盖后来的暂停/撤销/排除。不同主体新建关系，不能继承旧号权限；跨项目冲突不抢占。
5. 直接产生目标发现任务，同时唤醒经营决策，互不等待。角色/目标核验不依赖“已经可以发布”；内部接入缺口不能说用户缺授权。
6. 首次初始化未启动与用户主动全局暂停要有来源区别；资料和首账号就绪可自动完成 setup，不能再要求一次“免费启动”。

普通连接界面明确“连接后自动运营，可随时暂停”。只有验证码/身份确认、真实退出或无法自行消除的主体歧义才求助本人；UI 布局失败、安装缺失、路由错误由产品处理。

当前登录身份来自当前会话自身证据，不能用被浏览的公开主页、帖子作者或同名昵称代替。handle 无稳定 ID 证明时明确 id_kind，不自动继承更名前权限。Cookie/token/完整认证存储不进入模型、普通日志或仓库。

### 7.2 技术路由与能力

resolve_execution(ConnectionRef|TaskRef)->ExecutionRef 解析部署登记 transport_ref，不接受客户端/模型任意地址。API 登录/截图/输入/接管、Worker 研究/提交/回查全部走它；不能残留固定端口/default 隐性回退。

一个账号环境可由公共多 Profile 网关或现有独立执行器持有，不强制每账号部署服务。新连接采用可独立控制的环境；旧共享 Profile 按实际资源串行并列迁移范围，不能复制凭据或假装已实现独立并行。

Capability={operation,target_type,formats,adapter_version,installed,
access_basis_refs,target_role_refs,missing_conditions,checked_at}。
当前是否可执行是派生判断，不是第二个 enabled。用户意图、产品安装/接入、账号真实角色、平台挑战分别保存；内部 pending 不冒充平台对账号的审批回执。

十一渠道都接相同协议，各自保留身份/目标语义：Telegram 自身 ID 与频道 peer；Facebook 当前发言主体与 Page 角色；Reddit 管理目标/普通成员两类角色；Instagram 自身主页；YouTube 当前频道；TikTok 当前主体与 UID/handle；X 自身身份及接入依据；LinkedIn 个人/公司 Page；Pinterest 画板；Threads 本平台会话；WhatsApp Channel 与普通聊天不同动作。不能因共用 fake Adapter 宣布十一平台实号通过。

### 7.3 授权与首次真实业务执行

意图由 owner 连接/命令产生；目标角色由工具核验；grant 由系统在意图内派生并保留明确排除。适用平台规则/访问方式由产品方核实，缺接入能力只阻止该动作，不增加用户层层点授权。

首次格式：适配器已装配、本地合约通过、适用接入和目标角色满足但缺目标实号回执时，可让合格正常业务任务携 verification_required=true 首次执行。不能先要求“已有该格式真实成功”再允许第一次，形成循环依赖。失败不提升技术状态。

publication_purpose=business|diagnostic 与 verification_required 独立。正常首条业务帖可计真实成果，诊断/测试排除；不伪造 accepted 点击，不发垃圾连接测试帖。受控 SQL 比对任务、意图、账号/署名、目标、正文/素材哈希、适配/格式版本与真实回执后才提升对应能力。

终提交重验当前项目控制、意图/排除、身份/角色、规则、路由/租约、内容、预算。prepare 可重用纯检查函数，但之前 ready 不能免除最终事实核对。

### 7.4 渠道操作不重新经营

channel 一个任务保存 preparation→dispatching→pending/unknown→confirmed/not_applied 的检查点，复用 actions 原转换与 operation_key。Activity 重入必须先看账本；只有证明 not_applied 且原条件仍有效才按原限定路径恢复，不按 Activity 自动重试重复点提交。

恢复 failed 任务必须有真实 condition revision 变化，由 runtime 的 resume_task 原子递增 task_generation、记录原因并写新投递；不改 operation_key/意图。unknown 在没有证据前不能走该“重新提交”路径。

待审与 unknown 区分；待审有原外部 ID，观察原帖，不能再发。布局不兼容只有限只读观察/重载，适配代码缺失是产品故障，不能由业务 Agent 自动修改生产程序。

## 8. 内容、方法库、结果与预算

### 8.1 研究与内容工具

ToolDefinition={name,input_schema,output_schema,resources,
side_effect_class,timeout,retry_class,capability_ref}。
只允许声明的只读/本地产物/外部准备/提交/回查，模型无任意 SQL/Shell/内部地址或绕过账本的网页写工具。

研究 URL 来自用户登记、工具受控检索和实际发现链接；模型 URL 只是候选，不是已观察证据。公网地址/重定向/只读限制沿统一网关。来源正文中的指令不改变权限。

内容输入：实验问题、承诺与证明、受众语言、实际位置、格式、下一步、披露。产物：content_id/revision/hash、source_refs、claim_checks、entry、media_refs、quality_status。草稿不等实际素材，素材不等已发布。

无依据的效果数据/客户故事拒绝；真实演示须实跑，自制说明不包装成实测；入口与承诺一致。已确认事实/格式兼容可复用，不因无关偏好重制一切。可选媒体失败不阻止事实充分的文字路径，但替代必须仍能验证本实验目的。

ChannelMethod 是可选知识：受众场景、分发机制、所需动作、适用条件、内容选项、指标限制、来源/复查日期。无自动轮换/固定默认发布节奏、无第二套策略排序。

Facebook 按产品选需求参与、原创分发或合作；Page 只是身份/位置。thread.read、comment.submit、group.submit、reel.publish、insights.read 各自声明；当前仅有 Page 发布时不能冒充外部答疑已完成。没有分发/覆盖证据时先验证分发，不把发帖次数称获客。

### 8.2 业务事实与归因

OutcomeDefinition={event_ref,rule_revision,unit,newness_rule,quality_predicate,
trusted_producer,identity_scope,exclusions,attribution_window,delay_allowance}。
主目标保留产品价值行为；点击/访问/注册只作相应漏斗信号。measurement=missing|testing|healthy|stale，未知不当零。

服务端稳定事件 ID、发生时间、connection、受限业务化名 key、规则版本、test_scope、冻结来源；同事件重试幂等、冲突拒绝。项目新客须历史基线或可信首次业务证据，没有历史是 unknown。聊天 pair 可由业务端回传双向成功，不采消息正文/联系人；无法证明就补能力，不编造服务端验证。

每次实验链接串起 task→action→发布→触点→业务事件。订单/聊天/邀请创建时冻结来源，异步成功读原快照。首个合格获客触点作主来源，其他辅助；跨渠道同业务主体项目去重，被邀请者记产品邀请，不冒充第二名直接渠道新客。来源缺失保留未归因。

窗口、规则、测试排除在实验前冻结；旧客、团队、合作方验收及诊断排除；旧口径不证明新口径。迟到事件仍入账，封账报告生成新修订不覆盖旧结论，补齐统计可唤醒后续判断，但不会重复执行已结束外部动作。

OutcomeSummary={definition_ref,measurement,window_status,exposure,
visits,new_units,repeat_units,unknown_newness,unattributed,costs,
exclusions,evidence_refs,as_of}。展示分母/覆盖；一个激活只证明可能性，不证明规模化或因果增量。

### 8.3 费用与实验停止

区分模型、素材、广告、合作费用；token 配置不是广告预算。实际调用前 budget.reserve；结果确认后 settle，未知保持预留。实验限额取其全部后续任务/variation 的累计费用，项目总上限由程序保证，Agent 不可重置额度。

实验期限/预算耗尽无需等待模型，runtime 立即停止新投入、取消失去目的的未提交工作并请求复盘；已有回执/结算继续。历史用户/平台限制保留来源，不因统一框架扩大；产品名特例转显式配置，新项目走通用目标，不能为复用继续绕过新口径。

## 9. 最小界面、旧能力退出和迁移

### 9.1 日周计划删除

删除日计划/周计划的生成、保存、模板、模型上下文、自动 focus/排序、页面/日期导航和正常 API 消费；不能仅改名“增长计划”继续。新 UI 只保留目标、当前验证、正在做/等待、真实结果、必要本人事项；无需另建实验管理控制台。

GET /api/agents/{aid}/growth -> GrowthView={objective,current_work,
current_learning,results,waiting,owner_actions,as_of}，纯查询，不派活。
POST 同路径 /commands={command_id,expected_revision,kind,payload} 只支持目标更新和账号主动控制，沿 owner 权限/来源/幂等。连接沿原入口转统一服务。

修改 apps/web/app/page.tsx、operating-panel.tsx、account-browser.tsx、social-accounts.tsx、agent-results.tsx、api-client.ts；新增 growth-status.tsx 复用已存在状态样式。移除 operating-plans.tsx、operating-plan-model.ts、plan-task-chains.tsx、plan-work-records.tsx、operating-plans.module.css 的生产引用；其他文件如有需要的小型时间/记录函数移入原公共工具，不能为保留计划组件留下整套模块。

/plans、/plans/history、/plans/preferences 退出正常产品。仅仍有明确旧生产消费者时保留限期兼容，列消费者/截止版本/升级步骤；没有消费者则直接移除。任务证据/活动查询迁到 /work/{task_id}/basis|activity。旧历史数据留审计，不继续生成日周计划。

### 9.2 复用映射

| 旧能力 | 目标处理 |
| --- | --- |
| operating_kernel/work/feedback/followups、growth_experiments/acquisition_strategy | 保留符合契约的读取/统计；业务选择只进入 growth.agent，后续任务只经 store/runtime |
| operating_plans/operating_plan_views | 日周计划退出；必要事实/任务记录迁新只读视图，读接口无副作用 |
| channel_research/delivery/calendar、atomic_dispatch/workspace/backlog | 去掉独立经营时钟、模板轮换和默认日历；迁原产物/等待为固定任务 |
| community_growth/runtime/schedule/selection、owned_audience、telegram_inbox、私有驻留 | 原生操作转工具/任务，原用户范围与额度保留；不保留影子经营循环 |
| activities._publication_cycle、social_publish、publication_delivery/recovery | 操作任务消费已接受内容，不再次全项目选题；保留真实账本与核对 |
| account_profiles/channels/browser/managed_network/api.browser_rpc | 统一意图、身份、路由和局部控制，去掉普通流程名单/端口猜测 |
| budget/model/analytics/pixel | 按新结果/费用契约验证；产品名分支迁有来源配置，不能降级目标 |

“已符合”必须有新契约行为证据；接口适配仍留旧经营副作用则不合格。复用有违目标就替换，不允许因旧测试绿而免验。

### 9.3 新旧归属与切换

技术 owner 保存在 agents.onboarding_state.growth_runtime，只有 decision owner 与按实际执行资源划分的 execution owners，值 {engine:legacy|growth,generation}。不再叠加全局 mode＋各阶段 enabled；迁移进度是维护任务记录，不是新用户门槛。

preview_migration(scope)->MigrationReport，包含源版本、真实消费者、账号/Profile/路由、明确意图及后续暂停、实验、prepared/unknown、原限制、待转任务、冲突。无写预览，正式 apply 必须期望 report_hash/旧 generation；不能扫描到另一任务/生产库。

切换：停止该范围旧入口新建→原动作到检查点→短事务变 owner/导入可转任务/发 outbox→新消费者恢复原计时→独立回读应用、真实执行器、租约、任务与未知账本。共享 Profile 同 owner；一个资源切换不取消其他 owner。

旧 connected/false 不推断运行/暂停，按有来源的意图和更晚撤销迁移。原动作/费用/回执/历史不改写；原 prepared 仅兼容才转，unknown 沿原核对。模型/经营代码、私有设备都列清单，不能主应用升级便宣称全部退出。

回退先冻结新写入并对账；不回滚账本/意图/成果。旧运行器不能理解新意图时不能重新派活，保留必要核对消费者或向前修复。迁移后旧派活被实际拒绝，临时兼容有退出条件。

## 10. 自修复验收范围与工程参数

自修复分为：运行故障恢复（程序）、业务假设纠正（Agent）、缺能力或本人条件（明确责任并使用其他可行路径）。不声称 Agent 能自行安装未实现动作、突破权限或修好所有外部服务。

初始工程参数仅用于可测可靠性：outbox 正常轮询沿当前默认 1 秒；恢复扫描 60 秒/批 20；读取一步最长 45 秒；模型/工具复合活动最长 8 分钟并有进度检查点；任务不因长超时占住其他任务启动。真实平台限制及用户值优先。

并行基准：隔离环境、outbox=1 秒、至少两个空闲执行槽及独立 Profile；阻塞 A 后 B 从就绪到开始不超过 5 秒，且区间实际重叠。漏事件恢复在一次 60 秒检查加调度裕量内被发现；测试批次超过 20 时必须轮转覆盖全部，不能只测第一批。大规模延迟记录为容量问题，不以 SLO 作为假性能承诺。

外部未知状态可以持续，但不占其他资源、不无限核对/产生新稿、不宣称成功。恢复链必须至少证明：重复失败不会无限投入、条件修复后能继续、无法自动解决时准确等待/结束、其他范围持续前进。

## 11. 实施任务：直接按本版执行，不再生成平行方案

T0–T8 的实现与本地验证已完成，T9 的本地迁移/私有装配已验证，T10 的最终模型及正式交付继续执行。默认一个实施者持续推进；产品工作并行要求不等于增加开发子 Agent。一个需求一个 PR、精确提交检查与生产按仓库现行规则，设计交付不扩大授权。

开工先核对账号专项/主线最新改动。已有相符功能用新契约验收后复用；分工必须唯一，不能两个会话分别造意图表、路由与账号协调器。迁移编号按实际 Alembic heads 分配，整合为同一可验证图。

### T0：安全隔离和验证入口

新增 scripts/verify_growth_runtime.py、tests/test_growth_verification_scope.py。CLI 为 --prepare、--suite <名称>、--stop、--state-dir <绝对目录>、--model-evaluation。prepare 创建未占用环回端口、专属数据库/namespace/测试 Profile，生成自己的 env/config/manifest。所有 suite 在 import 前断言实际数据库及 CONFIG.temporal_address 匹配 manifest，禁止使用共享 .env、默认用户库/Profile、生产地址和现有开发队列。

- [x] 先测默认目录/用户 Profile/占用服务被拒、stop 不能关闭非本任务服务，再实现装配。
- [x] 空库及时间跳跃 Temporal 最小测试通过；报告只记端点和版本，不打印秘密。
- [x] 只有 --model-evaluation 显式调用真实模型，普通 suite 不静默产生费用。

依赖：无。完成条件：后续验证能独立重跑，不污染用户状态。

### T1：最小状态、命令和存储

新增 growth/__init__.py、contracts.py、store.py；修改 infra/runtime.sql、provision.py、迁移及目标配置消费者。tests/test_growth_contracts.py、test_growth_store.py、test_growth_migrations.py。

实现第 3 节三表、tasks.experiment_id、类型与命令组原子接收。意图/技术证明经受控函数验证来源，复合 FK/RLS 阻止跨项目和伪造来源；并发 patch 不整列覆盖。

- [x] 先测 C01/C04/C08/C09/C11 的存储部分：部分接受、无完整激活可研究、迟到事实、动作不可变、费用不重置。
- [x] 实现 submit_work/accept_commands 与 outbox 同事务；重复 command_key 返回原结果，某组失败不撤销独立成功组。
- [x] contracts suite 验证空库/基线迁移、跨租户/项目、并发 CAS、重复事件和 rollback 后不丢任务。

完成条件：唯一事实与写入权成立；没有引入逐阶段审批/启用状态。

### T2：经营推理与业务纠正

新增 growth/agent.py；修改 operating_contract.py、commands.py、model.py 适配边界。tests/test_growth_decision.py、test_growth_product_understanding.py、test_growth_progress.py。

实现 build_context/decide/accept_commands、初始产品理解、最小实验、真实断点选择、问题级投入与无进展规则；只传相关事实，提示词版本化。

- [x] 先测 C01/C04/C05/C12/C14/C21：局部变化不全拒、冷启动、非法输出终止、重复结论不算进展、不同反馈有不同动作、关键新事实保留后续判断。
- [x] 输出只有已声明工具命令；结构修正只处理 rejected，服务器生成 question/variation key，不能靠改标题重置累计。
- [x] decision suite 用合成输入验证接受/拒绝规则；真实模型能力另由 T10 评估，不能拿 stub 冒充。

依赖：T1。完成条件：新产品无需名称分支，能主动调查、学习、转向和停止。

### T3：固定任务运行和活性恢复

新增 growth/runtime.py；修改 workflows.py、worker.py 和现有资源/事件入口。tests/test_growth_runtime.py、test_growth_workflows.py、test_growth_concurrency.py、test_growth_recovery.py。

实现 advance/finish/recover_due/resume_task；七类任务及两种工作流，固定 TaskRef，无额外业务选择循环。派发、租约、等待、episode、补偿和暂停按第 5、6 节。

- [x] 先测 C02/C03/C06/C07/C10/C11/C15/C16/C17/C18/C20：重复投递、长期挂起、有限恢复、漏事件、实际并行、预算、饥饿、多页补偿、共用服务中断。
- [x] 实现每类超时/检查点/重试阶段；recover 不重置业务次数，资源健康成功后恢复原任务一次。
- [x] runtime suite 必须开启真实 Temporal 时间跳跃、重放、Worker 重启；并行区间、claim、数据库锁和任务起始延迟实际留证，不允许必验项跳过。

依赖：T1；与经营模型接口独立。完成条件：单个工作永久挂起不会拖停其他就绪资源，恢复无需外部 Codex 干预。

### T4：账号、浏览器与动作范围

新增或整合 channel_contracts.py、account_intents.py、execution_routes.py、account_connection.py；修改 api.py、channels.py、account_profiles.py、browser.py、managed_network.py、identity.py、browser_challenges.py、domain.py、activities.browser_rpc。tests/test_growth_account_connection.py、test_growth_execution_routes.py、test_growth_controls.py。

- [x] 先测 C01/C07/C10/C19/C22：同号重登不复活暂停、换号/别名/歧义、关前端、IME 最后输入、单 Profile 挑战和路由缺失。
- [x] 所有消费者使用 resolve_execution，账号专项不另建 AccountConnectionWorkflow/account_work；连接/发现是 account 任务，独立于经营模型。
- [x] 账号意图、真实角色、产品接入与本人挑战分开；目标发现不依赖发布已 verified。
- [x] account suite 包含十一平台各自身份页面合约；一个通用 fake 只验证扩展接口，不能替代平台证据。

依赖：T1/T3。完成条件：连接自动产生具体工作，其他渠道不断，能力缺口归产品而非假称用户缺授权。

### T5：工具与最短任务链

新增 growth/tools.py；改造 operating_work.py 的来源读取、creative_pipeline.py、content_backlog.py、operating_playbooks.py、playbooks/channels.json、community_discovery/runtime/selection、telegram_inbox 的操作边界。tests/test_growth_tools.py、test_growth_content.py、test_growth_dependencies.py。

- [x] 先测 C03/C04/C12/C15/C23：研究无需登录/激活，视频失败不阻塞文字，重复证据终止，工具不能授权，读取/归纳释放资源。
- [x] 从旧方法移除轮换、默认日历/目标和自动派活；只保留方法知识和当前工具。
- [x] content 宏任务内部调用媒体工具，共享/独立素材才单独拆任务；输入输出来源/claim 检查按第 8 节。
- [x] tools suite 包含原文/提示注入、真实本地渲染、格式/入口与复用边界；不以产稿数证明获客。

依赖：T1/T3；不等待全部账号。完成条件：能力可复用，旧隐藏经营循环不存在。

### T6：业务事件、归因与费用结算

新增 growth/outcomes.py；修改 pixel_events.py、analytics_ingest.py、analytics_reporting.py、promotion.py、outbound_links.py、现有 conversion 消费者及 budget/model 相关接口。tests/test_growth_outcomes.py、test_growth_attribution.py、test_growth_costs.py。

- [x] 先测 C11/C14/C24：两渠道同人项目去重、邀请链、pair/user/order、新客未知、重复/晚到/测试、窗口与定义变化、未知费用。
- [x] 移除产品名导致点击代替激活/隐含无限费用的代码路径；有效旧限制以来源配置保留，不私改额度。
- [x] outcomes suite 对来源、业务事件和费用核对，观测缺失不当零；合成事件不得发正式采集。

依赖：T1，可与 T2–T5 独立。完成条件：真实/未知成果、累计投入都可勾稽。

### T7：外部动作的可靠闭环

改造 activities._publication_cycle、social_publish.py、publication_delivery/recovery.py、adapters/core.py/web.py/catalog.py、channel_authority.py、受控 SQL 验证函数及真实私有消费者。tests/test_growth_channel_operation.py、test_growth_verification_proof.py、test_growth_unknown_receipts.py。

- [x] 先测 C03/C08/C09/C10/C19/C22：提交后断网、取消后迟到真实回执、不可变快照、暂停竞争、换号、首条业务验证。
- [x] submit 消费已接受内容，不能再选题；同 task 检查点贯通准备/提交/审核/核对。
- [x] 首次格式不依赖先有真实成功，仍完整验证所有其他条件；正常首帖与诊断区分，不伪造 accepted。
- [x] submission suite 用真实本地页面操作/SQL/装配候选，公共 fake 不替代私有执行器证明。

依赖：T3/T4/T5，效果计数接 T6。完成条件：最小本地端到端可以运行，无实号发送。

### T8：最小产品界面，删除日周计划

新增 growth/views.py 和前端 growth-status.tsx；修改 api.py、page.tsx、operating-panel.tsx、account-browser.tsx、social-accounts.tsx、agent-results.tsx、api-client.ts；退出第 9.1 节计划组件和计划接口，更新 workflow-guide.html 单一内容源。

tests/test_growth_views.py、scripts/verify_growth_ui.py。前端先读 apps/web/AGENTS.md、实际安装 Next 文档并查询匹配接口；不升级依赖。

- [x] 先测 C13：正常导航、网络请求、模型上下文、存储写入都无日周计划；读视图不派活。
- [x] 必要工作证据改独立 /work 查询；聊天/按钮修改同一意图，没有重复启用/授权门槛。
- [x] ui suite 启动合成数据页，检查桌面/窄屏/键盘、必要本人事项和并行工作。类型/构建通过，截图实际检查。
- [x] 更新 PRODUCT、ACCEPTANCE、ARCHITECTURE、CHANNEL_OPERATING_PLAYBOOKS、PIXEL_EVENTS 和 DELIVERY_BACKLOG；历史记录标历史，不生成第二张流程图。

依赖：T1/T3，账号/成果视图消费 T4/T6；可按稳定 schema 先做，不等全渠道。

### T9：迁移与旧入口实际退出

新增 growth/migration.py；改第 9.2 节所有旧生产派活入口、resident_ownership 和私有执行器。tests/test_growth_migration.py、test_growth_legacy_exit.py。

preview_migration(scope)->MigrationReport；apply_migration(scope,report_hash,expected_generation)->MigrationResult。普通模型不能调用迁移命令。

- [x] 先测 C09/C13/C19：旧 prepared/unknown、暂停、共享 Profile、未确定来源额度、迟到旧进程和原计时。
- [x] 新旧切换按资源原子，原动作身份/费用不重建；旧代码不能继续派活；回退不兼容时明确保留核对或向前修复。
- [ ] migration suite 用实际 Linux/装配消费者证明归属，日周计划兼容按具体消费者退出，不保留永久别名。

依赖：T1/T3/T4/T7 的契约，预览可提前；不要求全十一平台实号同时就绪。

### T10：端到端、模型质量和交付证据

tests/test_growth_e2e.py、test_growth_generalization.py、test_growth_liveness.py，补验证入口报告。

- [x] 首先用无外部网络测试渠道跑：产品输入/连接并行→自主实验→内容→一次渠道动作→触点/业务事件→复盘/新决定。不得人工往运行库塞策略。
- [x] 按第 12 节 24 个场景验证；故障至少跨两个独立 Profile、三种资源和两个项目。
- [x] 按第 13 节冻结评估集/阈值测试真实模型，费用/提示版本/证据分别记录；合成结构测试不冒充智能。
- [x] 完成本地与装配证据后按风险一次整体审阅和受影响修正；不重复逐阶段全量审计。
- [ ] 真实渠道可执行、触达陌生人、产品新增激活、自主重复执行分别验收；其他渠道缺口独立可见，不能用零粉丝发帖冒充获客。

依赖：首条纵向链 T1+T2+T3+T4+T5+T6+T7 即可提前 e2e；范围交付再收尾 T8/T9。工程依赖不成为运行时全阶段屏障。

实施顺序：T0→T1；T2、T3、T6 根据固定契约可独立；T3 后 T4/T5；T4/T5 后 T7；T8/T9 按消费者接入；T10 从首条闭环开始持续验证。十一渠道各自验收，不捆绑成统一 ready 开关。

## 12. 失败场景：可直接转成测试的输入与断言

以下均为计划，尚未执行；测试名与 owning suite 固定。每个场景必须先证实错误行为被用例揭示，再实现，保留实际报告。不能仅检查“任务被创建”。

| ID / 测试名 | Given → When | 必须断言 | 任务 |
| --- | --- | --- | --- |
| C01 partial_acceptance | A/B 独立命令，生成后 A 换号/暂停 | B 只接收一次，A 被拒；修正不重建 B | T1/T2/T4 |
| C02 fixed_ref_delivery | DB 已提交 outbox，启动已接收后进程崩溃，事件重投 | 固定 TaskRef 只有一个执行；inbox 不吃掉未完成启动，不领其他任务 | T3 |
| C03 macro_operation | 同渠道动作上传/提交中断，重复唤醒 | 同任务同 operation_key，恢复检查点；点击提交计数最多一次，unknown 只核对 | T3/T7 |
| C04 cold_start_no_gates | 新产品无登录/激活/发布位置 | research 开始；不伪造必填事实，不要求新增审批 | T1/T2/T5 |
| C05 invalid_proposal_exit | 模型两次返回非法结构/不存在工具 | 一次修正后关闭原调用、有限恢复；无永久 pending，无 provider 空等 | T2/T3 |
| C06 retry_episode | 同故障重启/换任务名/重复事件十次 | 业务尝试总计不超过 3；修正不超过 1；探测同时最多一个且不重置费用，无关健康信号不解锁 | T3 |
| C07 missing_wake | 条件已变，故意丢恢复事件；另给无生产者 Wait | 有效等待到 check_at 被补偿；无生产者/出口的 Wait 被拒 | T3/T4 |
| C08 late_receipt | 提交实际成功，租约过期且本地取消，旧 Activity 后返回 | 原动作真实 confirmed 入账一次；不控制新尝试/账号，不重发 | T1/T7 |
| C09 immutable_snapshot | prepared 后无关配置变化；另有真实内容/身份变化 | 无关变化不批量取消；相关变化取消未提交；旧意图不原地升级，unknown 不替换 | T1/T7/T9 |
| C10 parallel_pause_order | 两 Profile 同时提交，另一个事务暂停其中一个 | 提交区间可重叠；暂停与已进入点击有确定顺序；不持 Agent 排他长锁 | T3/T4/T7 |
| C11 budget_cancel | 两实验并发预留，取消一个，另有未知计费 | 不超项目/实验上限；确定未花释放，未知不释放，等待任务不占预算 | T1/T3/T6 |
| C12 no_progress | 同源同文改标题/时间，换 variation 并产稿，两轮无指定进展 | 不计新证据；原问题费用累计；结束/选有限替代，不继续复制任务 | T2/T5/T6 |
| C13 no_period_plans | 打开项目、对话、状态页并运行多个唤醒 | 无日周页面/请求/生成/持久写；读视图无派活；兼容仅明确旧消费者 | T8/T9 |
| C14 feedback_branching | 同产品分别注入缺需求/分发/覆盖未知/访问无激活/回传坏/低复用 | 下一步属于相应断点，不一律多发帖或永久研究；目标不改点击 | T2/T6/T10 |
| C15 blocked_independence | A 研究永久阻塞；B Profile、文字产稿和回执有空闲资源 | B 在基准 5 秒内开始，与 A 重叠；可选视频失败不阻文字 | T3/T5 |
| C16 lease_and_progress | heartbeat 一直来但产物检查点不变 | heartbeat 不延长有意义进展；达到期限核对/恢复/终止，不假称进展 | T3 |
| C17 fairness_and_paging | 45 个到期等待、持续新高优先级任务 | 补偿跨三批覆盖旧任务；可运行低优先级最终执行，无首项冲突阻塞 | T3 |
| C18 provider_recovers | provider 失败耗尽业务次数，后在低频探测时恢复 | 只恢复原任务一次，预算/计数不重置；确定性回执/连接全程继续 | T3 |
| C19 migration_identity | legacy/growth 同时存活、换号、同号重登、原 unknown | 单 owner；同号不复活暂停；新号不继承；旧 unknown 仍查原回执 | T4/T7/T9 |
| C20 shared_outage | 断共享 DB/队列再恢复，另模拟仅单 renderer 故障 | 共享故障准确说明；恢复按原任务；局部故障不扩项目；不丢/重复动作 | T3 |
| C21 event_storm | 决策中多次 PageView/心跳，加一条关键业务结果 | 普通事件不反复作废；关键事实未丢，合并后只增加必要判断 | T2/T3 |
| C22 account_first_use | 不点完成关前端、IME 输入、真人挑战、首次格式 | 后台完成正确身份；挑战局部；业务首发真实证明后才提升格式且统计一次 | T4/T7 |
| C23 untrusted_tools | 原文提示注入、公开作者同名、缺动作、模型生成 URL | 不授权/不串号；无依据 URL 非证据；只阻该动作，真实替代继续 | T4/T5 |
| C24 outcome_semantics | 跨渠道同人、pair/order、老客未知、重复/迟到/测试事件 | 单位正确；项目去重；未知保留；迟到更新报告修订，不重发旧内容 | T6 |

测试代码文件归第 11 节，函数统一以 test_<测试名> 命名。C10 必须用真实数据库并发与实际本地页面，不用顺序调用伪装竞争；C15/C17/C18 记录时钟/租约/费用，C02/C07 用真实持久工作流，C08 查看原账本和本地提交计数。

## 13. 验证命令、模型质量与交付边界

### 13.1 未来可执行命令

T0 实现后，在本任务隔离目录运行：

    .venv/bin/python scripts/verify_growth_runtime.py --prepare --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite contracts --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite decision --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite runtime --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite account --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite tools --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite outcomes --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite submission --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite ui --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite migration --state-dir /absolute/task-state
    .venv/bin/python scripts/verify_growth_runtime.py --suite e2e --state-dir /absolute/task-state
    npm --prefix apps/web run typecheck
    npm --prefix apps/web run build
    .venv/bin/ruff check .
    git diff --check
    .venv/bin/python scripts/verify_growth_runtime.py --stop --state-dir /absolute/task-state

suite 精确映射第 11 节 tests/test_growth_* 文件和 C01–C24，report 保存源码 SHA/未提交 diff hash、实际端点、依赖、用例/失败/跳过、并行区间、状态/费用与截图。必验 Temporal/浏览器项不得跳过；外部发送为零。测试服务 stop 只关闭本任务 manifest 的资源。

上述脚本及测试已实现；实际通过范围、输入版本和交付阶段见第 14 节。

### 13.2 真实模型评估

额外显式 --model-evaluation，不能普通测试静默收费。冻结 3 类未做产品专用配置的合成产品（双人聊天/单人报价/商品交易），每产品 3 个保留结果分支，共 9 个输入；每个运行 3 次，共 27 份独立输出。分支覆盖 C14，来源采用实际提供的证据，禁止向模型给参考答案；记录模型/提示词/输入版本和总费用。

硬性判定：编造证据/权限、跨项目、假激活、无边界无限投入为零。行为判定：每输入至少 2/3 次正确定位断点与行动类别，27 次至少 24 次通过；阈值在运行前固定，是工程接收标准，不是商业效果/所有产品泛化证明。证据引用、必要动作前置和停止条件由确定性规则核对，语义争议由审阅者核对原始资料，不让模型自评自过。

未达标仅修提示/上下文/工具契约或程序缺口，补反例回归；不能为过测硬编码产品名/泄漏答案。费用到界即停，结果不够则待验，不宣布“智能已完成”。

### 13.3 完成条件

工程：C01–C24、真实模型评估、类型/构建/原有关键回归、私有装配与迁移退出均覆盖；日周功能不再生成/呈现；模型/权限/外部事实边界无阻断问题。各渠道独立记录已实现动作/目标合约/实号状态，不捆绑为全局 ready。

真实业务试点另验：路径可执行、接触陌生受众、可信可归因新增激活、能自主根据结果开展下轮。零真实激活如实为零，不用发帖/播放/测试数量替代。一个用户不能证明规模化或因果增量。

未来授权交付仍是一个需求一个 PR、精确 head 快检、精确 main 完整应用验证与同轮产物；应用/实际执行器/真实平台动作/获客分开回读。没有授权不实施生产或实号动作，旧授权不扩大本次。

## 14. 当前审计证据和交接

本轮依据是 V1 规范反例、主线实际 outbox 投递/重试与不可变 actions 约束、账号专项、依赖版本；不将文档推演称运行验证。

Temporal 1.34.0 已经 Context7 查询，参考 [对应版本官方说明](https://github.com/temporalio/sdk-python/blob/1.34.0/README.md) 的信号、计时、重放及继续运行；投递幂等仍由任务/账本保证。锁设计核对 [PostgreSQL 18 显式锁](https://www.postgresql.org/docs/18/explicit-locking.html)，控制修改与兼容读锁的冲突必须由 C10 实测，不能将普通读取当终提交屏障。前端/浏览器接口实施时再核对实际匹配文档，不升级依赖。

本地实现以原基线为起点，依赖未升级。新增 `0031_growth_runtime`、三张权威表、七种固定任务和一个项目经营决策归属。命令只要求明确的当前目标版本；完整成果口径由服务端冻结，不让模型重填测量权限与身份。产品理解只依赖产品版本，修改成果目标不取消这项独立研究。一次决策的事实与时间窗口持久保存，重放不重复计费；结构修正接收实际校验错误但不回显原始输入。

本地有效证据：

- 完整原有套件曾运行 1122 通过；旧 UI/独立研究 Profile 等相关失败已修正。macOS 缺少 Docker 的三项恢复检查留给必须执行的精确 main Linux CI，不能称本地全量通过。
- 最终受影响范围 180 项通过，覆盖全部当时的 growth 测试及 pixel/budget/model/browser/migration/content/channel 直接消费者，103.57 秒；随后显式命令/目标版本、实际校验反馈和结构化输出工具传输改动再验证决策/任务闭环及模型直接消费者 46 项通过，5.40 秒；发布与范围工具 187 项通过。新增独立性与模型传输契约也有单独证据。后续若再改输入，重验对应范围。
- 真实 Temporal 时间跳跃、104 个长期等待跨继续运行、历史重放，以及 SQLite 持久队列真实停止/重启、两个项目按原引用继续；均实际执行，无必验跳过。
- 两个独立浏览器上下文提交区间重叠，单账号暂停按锁确定顺序；另有公开研究保持阻塞时，内容和结果观察在 0.009 秒内完成，三类资源互不等待。报告 `parallel-pause.json`、`blocked-independence.json`。
- 真实本地 Facebook 提交丢回执、暂停后核对、迟到旧控制返回：提交/原动作均一次；缺少原动作尝试证明不提升格式权限。
- 新 `test_growth_task_loop.py` 经生产创建/连接/发现/决策/内容/渠道/采集接口跑完整闭环，没有直接往数据库插入策略；模型与网站 IO 受控，智能质量由另行真实模型评估负责。
- 异步 pair 仅携带已冻结触点、无浏览器身份时仍保留真实来源；晚到更早访问不改写该来源。真实采集产生的访问/事件/费用进入经营上下文；缺少曝光为未知，核心事件过期不会被新 PageView 掩盖。
- 桌面/390px/账号对话框/多账号控制/能力缺口/键盘/流程图已实际截图检查，类型与正式构建通过。`verify_growth_ui.py` 保留可复现入口。
- 私有 Telegram 候选仅保留五个原始可信原语和公共有界入口，实际装配/导入通过；旧 resident、selection、model 不装配不运行，未冒充实号验收。
- 已完成一次独立整体风险审阅，9 项问题均接受并修正，补覆盖失权决策、偏好保存、全链发布、真实漏斗、冻结归因、研究中的模型恢复、恢复唤醒、Profile 局部接管及决策重放。不增加第二轮整体审阅。

C01–C24 的实际验证入口：

| 场景 | 测试文件（`tests/` 下，函数包含对应语义） |
| --- | --- |
| C01 | test_growth_decision.py、test_growth_account_connection.py |
| C02 | test_growth_store.py、test_growth_workflows.py |
| C03 | test_growth_channel_operation.py、test_growth_unknown_receipts.py |
| C04 | test_growth_product_understanding.py、test_growth_tools.py |
| C05 | test_growth_decision.py、test_growth_recovery.py |
| C06 | test_growth_recovery.py、test_growth_runtime.py |
| C07 | test_growth_runtime.py、test_growth_contracts.py |
| C08 | test_growth_unknown_receipts.py、test_growth_verification_proof.py |
| C09 | test_growth_channel_operation.py、test_growth_migration.py |
| C10 | test_growth_concurrency.py |
| C11 | test_growth_costs.py、test_budget_settings.py |
| C12 | test_growth_progress.py、test_growth_dependencies.py |
| C13 | test_growth_views.py、test_growth_legacy_exit.py、verify_growth_ui.py |
| C14 | test_growth_attribution.py、evaluate_growth_model.py（真实模型另验） |
| C15 | test_growth_concurrency.py、test_growth_content.py |
| C16 | test_growth_liveness.py |
| C17 | test_growth_recovery.py、test_growth_liveness.py |
| C18 | test_growth_research_recovery.py、test_growth_recovery.py |
| C19 | test_growth_migration.py、test_growth_account_connection.py |
| C20 | test_growth_shared_outage.py |
| C21 | test_growth_liveness.py |
| C22 | test_growth_identity_platforms.py、test_growth_account_connection.py、test_growth_verification_proof.py |
| C23 | test_growth_tools.py、test_growth_dependencies.py、test_growth_content.py |
| C24 | test_growth_outcomes.py、test_growth_attribution.py、test_pixel_events.py |

真实模型最终冻结候选 `5c71847464f45b3fefa1b5b0c04ab0bd41b9555c`、提示 `growth-v2.5`、输入 `growth-generalization-v2`，DeepSeek `deepseek-flash` 实跑 27 次，实际 201,804 token。按预设“正确瓶颈、可运行的有界行动、给定证据和停止条件”人工逐份核对，24/27 通过，各输入均至少 2/3，52 个证据引用均经同项目权威库核对，硬性违规为零。三项未通过如实保留：报价需求场景一份过早偏分发场所；报价测量一份后备 PageView 验证未充分说明核心价值口径边界；商品转化一份引用不存在的实验占位符，任务被安全拒绝、只留下有界实验。不能把该份算已执行。问题类别字符串的启发式匹配仅 22/27，原记录保留；它不替代对实际问题和行动的逐份语义核对。原始报告 SHA256 `d942f16e47168c2b4ce7a1b9e1a7575be9340cd73f8c9042537679de502cf5b3`，位于任务隔离报告目录，仅不含密钥的统计进入仓库。未调整预设总分或每输入阈值。

PR #53（同一需求）已创建，首个精确 head 快检成功；合并前还需核对本次文档收尾 head 的必需检查。精确 main 完整 CI 与正式交付证据仍在本节后续更新。未通过的评估与配额失败报告私有保留，不以最终通过覆盖试验历史。测试模型调用使用显式有限预算；所有测试社媒外部发送为零。实际陌生人触达、新增激活和商业增长尚未证明，不能以工程完成替代。

### 正式切换步骤

1. 一个 PR 的精确 head 快检通过后正常合并；等待精确 main 完整 Application verification 与同轮应用/工具产物，不使用旧绿或重建产物。
2. 核对当前应用和两个独立执行器、原账号/角色/授权/预算/浏览器路由；安装同轮验证的 `home_systemd_release.py`。所有脚本、迁移和候选源码按精确 Git blob 核对。
3. 在私有根目录 stage 从实际 PostgreSQL 备份恢复隔离库，执行 0030→0031 与重复升级，核验原控制、账号、授权、动作身份、费用不变；演练通过才准入。
4. 按已有维护入口等待安全点、备份与恢复点，保留原调度状态，关闭入口后迁移；只改技术路由注册与默认端口，不扩大账号/平台权限。持久 pending 标记覆盖 schema、应用和物理执行器切换。
5. 两主机正式签名协议的 `preflight <SHA>` 只允许该 SHA 对应的维护标记，并核对真实 mounted 版本；正式 deploy 内完成应用与 Telegram 交付、注册两个执行器、最终 schema/进程证明后才解除 hold。请求全程 in_progress。
6. 中断后仅在实际应用为同一 SHA 时允许 `finalize-cutover <SHA>` 继续原执行器/释放标记阶段；不重复应用切换、不创建第二个请求。随后由独立命令/SSH 回读 Web/API、原持久工作流和两个实际执行器、原控制与权限，原请求 success 才算交付完成。不能在 schema 已迁移后盲目回退旧写入者。

Pydantic AI 实装版本 2.63.0 的 Context7 未提供精确版本索引，已改核对该版本[官方源码](https://github.com/pydantic/pydantic-ai/blob/v2.63.0/pydantic_ai_slim/pydantic_ai/models/openai.py)及 [DeepSeek JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)，不升级依赖；经营任务改用 SDK 的结构化输出工具通道，避免自由文本 JSON 包装错误；保留既有思考设置和一次修正上限。结构化工具请求、计费与单次付费重放均有实际请求回归。


### 主线完整验证补正（发布前）

PR #53、#54 已合并。#54 修正真实节点的 Telegram 配置入口：从可信核验器登记的 `/etc/lead-home-release` 读取并验证绑定，工具目录不能冒充配置目录，缺失则拒绝。生产尚未变更。

精确主线 `8e382b6952e4dab772cd699ddf3d383a545c8f6f` 的完整 CI `43128910047/1` 为 1172 通过、3 失败；原数据库恢复、媒体、权限、浏览器和 Linux 隔离演练已有实际覆盖，但该候选整体不放行。两项旧 Telegram 解绑测试的假 Actor 缺少新契约必需的实际 Profile；另一个真实持久队列重启测试在没有预装 CLI 的 Linux 环境中，下载目录尚未创建。补正仅测试装配：Actor 引用已绑定 Profile；下载缓存置于该测试自己的临时目录且先创建，真实 SQLite 队列重启/重放仍必验，未跳过。新增发布 preflight 对不匹配旧版本维护日志的拒绝回归，确保补正轮同时保留实际工具验证与产物。

受影响账号、持久队列与 Profile 接管 21 项本地通过；另外实际走 SDK 下载到全新隔离目录并启动真实队列成功，覆盖本机预装 CLI 曾掩盖的路径。发布/范围/工具回归 189 项通过。经营实现和模型输入无变化，冻结 24/27 评估仍适用。最终版本重新核对 PR 快检、精确主线完整 CI 和同轮应用/工具产物后才允许生产演练及切换；不引用失败候选或旧绿放行。


### 真实节点恢复演练的登录约束

精确主线 `7fa59ff3509510ce668077bf17bc22fec7322994` 的 CI `43131356461/1` 已完整通过：1176 项后端回归，以及前端、真实 Linux 隔离/恢复、Application verification、工具包。应用镜像 ZIP `13905527131` 摘要 `354eba2f5fcd2baa57ed579a3e8c7b56506ddae41313051e4f46eee066e76446`，节点只读镜像摘要 `1e06aace229417cc3be8a9904b0c8d5ad8573cbb233c4b81d50a7ac033298326`，尚未切换。该版发布工具已安装并独立回读，应用仍为 `84f6184`。

用实际生产备份恢复隔离库后，迁移演练遇到现有 HBA 规则：`lead_auth`、`lead_runtime` 只允许登录正式 `lead`，不能登录临时恢复库。演练在业务预览时失败并删除了临时库；正式 schema、控制和应用未切换。先前演练路径遗漏了这个真实节点条件，不能把本地/CI 恢复当作节点演练成功。

补正限定在 `rehearse`：管理员连接精确命名的 `lead_growth_drill_<12位提交>`，PostgreSQL 在应用查询前实际切换为请求的 `lead_auth`/`lead_runtime`，每连接核对 `current_user`，清除校验查询事务后才交给原调用方；连接池同样覆盖。只允许同一主机/端口、这一个隔离库和两个固定受限角色。演练结束关闭自有进程池并恢复原连接入口。`apply`/`finish` 不启用此适配，正式 HBA、角色权限与正常密码登录不改变。

新增真实隔离库测试，禁止 PUBLIC 连接，验证直接连接/池连接的实际角色；拥有原管理登录的会话切换到运行角色后仍受强制 RLS 约束，认证角色不能读取未授权表，正式库名称被演练适配拒绝。迁移/RLS 8 项通过，工具回归 189 项通过；生产认证/运行角色正常登录及管理连接只读角色切换另作核对。这里的角色切换只证明隔离演练的 SQL 权限与业务路径，正式认证链路由正常登录及 CI 单独证明，不混称认证验证。

下一候选须经精确 PR/main 验证和同轮产物后再演练；不复用上版镜像切换，也不绕过 HBA 或修改旧备份/失败结果。业务实现与模型输入不变，原冻结模型评估继续适用。

同一核对还确认节点登记的管理员用户名不同于开发默认 `lead_owner`。迁移进程的所有者连接现明确采用已有 site-state 登记的用户名与密码；不让下层业务迁移再落回开发默认身份。配置目标仍校验正式库和既有 socket，并有参数传递及错误库拒绝回归。正式库的管理员只读角色切换已实际验证。
