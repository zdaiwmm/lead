# lead 本地实现

## 运行边界

当前是一台电脑、一个本地所有者的开发部署。Next.js 负责界面；FastAPI 负责身份、业务命令、查询、接管和统计；Temporal Worker 负责独立 Agent 工作流；浏览器网关单独运行并持有浏览器进程。PostgreSQL 18 是业务事实源。没有每个 Agent 一台容器或虚拟机。

端口均绑定 127.0.0.1：前端 3100、API 8088、内部浏览器 8089、数据库 55432、Temporal 17233、Temporal 开发 UI 18233。内部网关使用独立 HMAC 凭据，API 使用 HttpOnly / SameSite Cookie、来源检查及 Host 白名单。该模式信任本机 OS 用户，不代替生产 OIDC 和租户授权。

`scripts/run.py` 启动并监护应用子进程；Docker 持久卷保存数据库和 Temporal 开发历史。Temporal 当前使用带持久文件的开发服务，尚不是生产高可用集群。

## 数据和事务

`infra/schema.sql` 为目标模型的 39 张表，`infra/runtime.sql` 增加版本、工作记录、聊天任务、本地测试站记录、浏览任务/步骤与交互授权卡。`scripts/migrate.py` 是当前本地安装器，尚不是完整的跨版本生产迁移工具。

运行时是 `lead_runtime` 非所有者、非超级用户、NOBYPASSRLS。每个事务显式绑定 `tenant_id`，Agent 执行事务还绑定 `agent_id`。表级 FORCE RLS 限制数据访问，复合外键阻止把另一个 Agent 的资料、目标、授权或内容挂入动作。运行时无 DDL、TRUNCATE、DELETE 权限；审计只能追加，配置和内容版本不能覆盖。

所有者列表可使用同一租户下的跨 Agent 查询；Agent 的记忆、历史、上下文和动作查询均使用单 Agent 事务。Agent 不接触数据库连接、SQL、Shell 或任意内部 HTTP 端点。

主要链路：

1. `agents` + `agent_revisions` + `browser_profiles` + `network_identities` 在一次事务内创建。
2. 用户消息按会话单调序号存入 `conversation_messages`，客户端消息 UUID 去重；同一 UUID 不允许改内容。
3. `chat_jobs` 与 `outbox_events` 和消息同一事务提交。服务暂不可用时不丢任务。
4. 模型输出受 Pydantic 结构约束，业务资料追加到 `business_profiles`，偏好追加到 `memory_entries` 并生成配置修订。
5. 自主周期生成 `plans`、`content_items`、`tasks` 和投递事件；发布动作保留目标、授权、配置版本和控制版本快照。
6. 工作记录和对话保存在数据库；执行结果必须来自浏览器/目标页面的实际证据。

实际浏览结果已写入 knowledge_documents / knowledge_chunks，带来源、哈希、未受信标记和全文索引；完整语义检索服务尚未实现。OIDC 成员体系、资产审核、服务端转化等表已预留，相关完整服务尚未交付。

## Agent 调度

每个 Agent 有自己的长期 ChatWorkflow 和 SupervisorWorkflow。聊天按 Agent 串行处理，避免同一会话乱序；不同 Agent 的模型请求和任务并行。SupervisorWorkflow 使用持久化计时器安排下一轮，在启动、恢复或暂停时接收 wake 信号。历史达到阈值后 continue-as-new。

发布使用独立 PublishWorkflow，任务 UUID 是工作流标识，重复投递不会再次新建发布工作流。当前 Worker 有 12 个 Activity 并发位，浏览器网关另有限制，默认 3 个活动浏览器。

模型负责提取业务、沟通、提出运营内容和下次检查间隔。程序负责账号/目标授权、控制版本、配置修订、浏览器租约、操作提交和结果核对，模型不能授予自己新权限。

模型调用有每 Agent 每日 token 预留和结算。未知计费结果不直接释放额度；实际用量超过估计仍会记账，并阻止继续自动执行。当前没有完整商业计费、账单核对和多币种成本服务。

## 浏览器所有权与控制

每个 Agent 对应唯一配置目录、浏览器进程和设备配置文件。文件锁由 OS 保证进程退出时释放；数据库租约记录 actor、epoch、期限；浏览器 session 与 profile 有复合绑定。同一个环境一次只执行一项浏览器操作，不同环境不共享 Page、Cookie 或全局鼠标控制。

暂停/接管增加 `control_version`。提交前检查授权、控制版本、配置修订、租约和接管状态。提交点击通过 Agent 行锁与暂停形成顺序屏障：已经进入提交的点击可能先完成，之后不能再发新动作。不会承诺撤销平台已接收的发布。

浏览器退出后重新打开同一个 profile，保留网站存储和固定设备配置。进程失联后的 `dispatching` 动作先变成 `unknown`，不直接重新点击。

## 发布账本

动作意图不可覆盖，数据库触发器限制状态转换：

```text
prepared → dispatching → confirmed
                     ↘ unknown → confirmed / not_applied
prepared → canceled
not_applied → prepared / canceled
```

点击前先持久化 dispatching 与 attempt，点击后根据实际页面和目标记录确认。确认失败进入 unknown，后续只核对，不盲目重试。确认必须携带实际外部 ID、URL 或证据。未知状态不能自行制造新 operation_key 再发一次。

本地测试站有独立提交去重，是外部目标的替身测试环境；不能据此推断社媒也提供相同幂等性。真实渠道必须实现按账号、目标、内容、时间和平台内容 ID 的核对，否则未知状态保持待处理。

## 浏览器出口

每个 Actor 有自己的认证 HTTP CONNECT 转发端口。真实浏览器的主上下文与只读研究上下文都使用该转发器；主上下文保存社媒登录，研究上下文不继承账号 Cookie，拒绝 POST/写入和 WebSocket。研究工具不提供任意 JS、SQL、Shell 或选择器执行，元素引用由受控快照生成。

转发器用同一上游中的固定公网 DoH 解析目标，再连接已校验的公网 IP，TLS 仍由浏览器端到端处理。支持 HTTP/HTTPS CONNECT 与 SOCKS5 上游，错误路径没有直连回退。只有本 Agent 的本地测试路径可访问环回地址。真实浏览器故障测试验证了上游不可用时连接失败，转发字节数为零。

高级环境检查由浏览器采集实际设备特征，并访问 ipify、Cloudflare 两个公网探针；固定代理和预设 IP 缺失时不提升为独立出口。不同 Agent 的已验证固定 IP 有数据库唯一约束，检查有效期 15 分钟。生产模式每次 Telegram 提交前重测。当前本机没有各 Agent 的独立代理，所以独立出口实号验收仍未通过。

以上不代替 OS 网络强制隔离、WebSocket/IPv6/DNS 的系统级泄漏采集和地区一致性检查。现有地区和浏览器语言配置不等于已经测得一致；生产要求仍保留。

## 平台适配状态

11 个入口：Telegram、Facebook、Instagram、YouTube、TikTok、X、LinkedIn、Reddit、Pinterest、Threads、WhatsApp。当前可在各自 profile 登录、接管，登录状态由浏览器持久化；`pending` 不会自动变成 `connected`。

`lab` 已完成真实浏览器、三环境并行、重启和丢失确认测试。Telegram 自有频道已实现 Web K 适配：当前账号槽位 → 稳定账号 ID → 频道 ID/可写状态 → 版本化授权卡 → 可审阅的完整验证消息 → 一次提交 → 真实 DOM 消息 ID 和截图。普通运行只有该目标通过验证后才开放，每天最多 3 条。适配器不读取第三方消息给模型，回执只在浏览器里匹配本次已知内容与唯一标记。

提交前持久化 baseline_mid、账号/目标/配置/控制/租约快照；确认丢失后只查找已知标记对应的唯一服务器消息。首次发布验证通过需 SQL 受控函数检查 accepted 卡、实际 ID、证据与目标/授权全部一致，模型没有提升能力的入口。

Telegram DOM 合约测试已验证账号 2 不跳回账号 1、真实填写与点击、确认丢失后仅有一条消息。该测试不等于 Telegram 实号测试。真实自有频道尚待用户在专属环境登录和授权；当前平台 technical_state 仍为 unverified。其他十个平台只有登录入口，发布适配及多媒体上传仍未实现。

自主周期读取真实指标、近期执行内容和偏好，必要时浏览公开产品事实，生成有依据的计划和下一执行时间；公开业务链接携带唯一 lead_ref，网站 SDK 归因后才计访问。当前没有成熟的多目标策略实验或对胜出策略的统计证明。

## 可观测性与统计

默认界面以对话、状态、工作记录和真实归因数据为中心。高级入口显示环境状态及本地测试工具。模型未配置/认证失败/限流/余额不足等状态不会生成假回复或假工作记录。

推广重定向与网站 SDK 是两种独立证据。点击不是访问；网站直接访问不归功于 Agent。SDK 的 client_observed 数据有来源校验、会话去重和活跃间隔规则，但不能代替服务端成交回传和反作弊服务。

## 生产发布前仍需完成

- 逐平台真实适配和自有账号实测，权限与行为范围的明确授权。
- 出口隔离与泄漏测试、稳定账号 ID 检测、设备/IP 一致性验收。
- 公网 OIDC、完整多租户授权、CSRF/CORS 和公网接管防护评审。
- PostgreSQL/Temporal 生产部署、版本迁移、备份恢复演练及监控告警。
- 账号凭据/浏览器文件的加密托管与密钥轮换，远程节点互信与 fencing。
- 用量计费对账、知识检索、资产流程、统计去重/反作弊和服务端转化。

这些是未完成的软件工作，不应包装成只差用户登录或输入密钥。
