# 当前接口和事件契约

具体字段以服务内 Pydantic 模型为准。Agent 不获得任意 SQL、CDP 或跨环境脚本执行端点。

## 范围和角色

`/api/*` 使用 HttpOnly 会话。修改请求必须同源并带 `X-Lead-Request: 1`。OIDC 会话每次核对有效成员。`X-Lead-Workspace` 与当前会话不符返回 409 `workspace_changed`，防止旧页面写到新工作室。其他工作室/Agent 的资源返回 404，仅查看成员不能修改业务或读取实时接管画面。

无会话返回 401，无权限返回 403，版本/状态失效返回 409，创建返回 201，已持久化的异步任务返回 202。SSE 只发送刷新摘要，完整资源接口是实际状态来源，撤销会话或切换工作室后旧流终止。

## 主要接口

| 资源 | 当前接口与契约 |
|---|---|
| 会话 | POST `/api/session`；本地建立所有者会话，OIDC 返回登录入口，不允许绕过登录 |
| 登录 | GET `/api/auth/login`、`/api/auth/callback`；Code + PKCE、浏览器绑定的单次 state |
| 身份 | GET `/api/auth/me`、POST `/api/auth/logout` |
| 工作室 | GET `/api/workspaces`、POST `/api/workspace/switch`；只允许自己的活动工作室 |
| 成员 | GET/POST `/api/workspace/members`；仅所有者，保护最后所有者 |
| Agent | GET/POST `/api/agents`、GET `/api/agents/{aid}`；创建的 `client_id` 幂等 |
| 对话 | POST `/api/agents/{aid}/messages`；`text`、UUID `client_message_id`、可选 `asset_ids`，先保存再异步处理 |
| 重试 | POST `/api/agents/{aid}/retry`；不能重发未知外部提交 |
| 素材 | GET/POST `/api/agents/{aid}/assets`、GET `.../assets/{id}`；真实解码、清洗、限额和范围归属 |
| 控制 | POST `/api/agents/{aid}/control`；`pause/resume/start/archive`，统一控制版本和唤醒逻辑 |
| 社群运营 | GET/PUT `/api/agents/{aid}/community-growth`；返回策略和 `schedule` 中的下次找群、回访、重试与检查时间；POST `.../community-growth/research` 可即时请求一轮，日常重复由产品工作流定时执行 |
| 偏好 | POST `/api/agents/{aid}/memory/{id}/remove`；核对预期版本，软删除并生成新配置 |
| 登录接管 | POST `/api/agents/{aid}/connect`、`.../takeover/end`；画面打开不能等于已验证账号 |
| 浏览器 | GET `.../browser/screenshot`、POST `.../browser/input`；接管会话、租约和控制版本共同检查 |
| 账号/目标 | POST `.../accounts/verify-login`、`.../accounts/{id}/targets` |
| 连接验证 | POST `.../targets/{id}/verification`；一次明确范围的发布验证，不提升全平台状态 |
| 授权卡 | POST `.../cards/{id}/respond`；核对卡片版本和当前授权，不接受过期卡扩大范围 |
| 身份测量 | POST `.../identity/check`；真实设备与双出口探针，无固定上游不提升验证 |
| 浏览证据 | GET `.../browser-runs/{id}`、`.../browser-evidence/{id}`；限当前 Agent |
| 获客 | GET/POST `.../analytics`、POST `.../tracking-links`、POST `.../analytics/server-key` |
| 实时状态 | GET `/api/events`；鉴权且不缓冲的 SSE |
| 后台 Agent | GET `/api/admin/agents`；当前工作室 Agent 的有效额度、当日消耗、预留与配置版本 |
| 后台配置 | GET/POST `/api/admin/config`；键为 `budgets/default`、`budgets/{aid}`、`onboarding/default`、`strategies/{platform}[/{aid}[/{target}]]` |
| 配置发布 | POST 配置携带 `expected_version`、`body`、`action=draft/publish/rollback`、可选 `note`；回滚需 `rollback_version`，恢复策略继承可设 `reset_to_default=true` |
| 批量额度 | POST `/api/admin/budgets/bulk`；`agents`、`daily_token_limit` 和每个 Agent 的 `expected_versions`，任一冲突整批撤回 |
| 变更记录 | GET `/api/admin/history`；可按 `key` 筛选，使用 `before` 时间与 `before_id` 联合游标分页；版本记录只追加 |
| 渠道范围 | GET `/api/admin/strategy-options`、`/api/admin/strategy-evidence/{aid}`；渠道模板选项、所属账号目标与现有策略观察 |
| 创建引导 | GET `/api/onboarding-template`；仅返回生效版本与模板。POST `/api/agents/setup` 可带开始填写时的 `template_version`，模板快照随创建草稿保留 |
| 运行设置 | GET/POST `/api/admin/config` 新增 `models/default`、`brand/default`、`runtime/default`、`notifications/default` 及对应 `{aid}` 覆盖；继承、草稿、发布、回滚沿用同一版本契约 |
| 模型状态 | GET `/api/admin/model-options` 返回服务状态与已发布候选，不返回凭据；POST `/api/admin/model-check?provider=deepseek\|openai&aid=...` 仅所有者，接受可选 `models` 数组，核对当前所选模型是否在服务端列表 |
| OpenAI 候选 | GET `/api/admin/openai-models` 仅所有者，使用同一服务端密钥读取文本模型；配置键 `model-catalog/default` 的 `openai_models` 发布后成为工作室候选。运行模型仍在使用的候选不可移除 |
| 模型用量 | GET `/api/admin/model-usage?aid=...&include_archived=false`；按实际服务与模型显示近 30 天/今日 token、输入输出与缓存、调用中和未知预留。未标识历史不归入当前所选模型 |
| Agent 管理 | GET `/api/admin/agent-directory?query=&state=active&platform=&before=&before_id=`；每页 50。POST `/api/admin/agents/control` 仅所有者，接受 `agents`、`action=pause\|archive`、完整 `expected_control_versions` 和 `note`，任一冲突整批撤回 |
| 渠道规则 | 配置键 `channel-rules/{platform}[/{aid}[/{target}]]`，各层只可收紧；GET `/api/admin/channel-rules/effective?platform=&aid=&target=` 查看生效值及来源版本 |
| 数据与归因 | 配置键 `attribution/default\|{aid}`，维护目标、归因天数、有效活动时长、新鲜度小时数；GET `/api/admin/attribution-status?aid=` 查看回传状态。访问与转化保存规则版本，后续修改不改写历史 |
| 策略经验 | GET `/api/admin/strategy-experiences?aid=&platform=`、`/evidence?aid=&platform=&target=`；配置键 `experiences/{aid}/{uuid}`。仅已发布且审核通过、业务与目标匹配的经验进入规划 |
| 经验提炼 | POST `/api/admin/strategy-experiences/capture` 仅所有者，接受 `agent_id,target_id` 创建观察草稿；POST `/promote` 接受来源与模板版本及明确的 `reusable_practice,applicable_conditions`，生成同渠道通用模板草稿，保留已有人工草稿 |
| 资料预览 | GET `/api/admin/content-library` 返回本工作室适用的资料包、来源、内容、配置版本和 `content_digest`；业务专属资料只匹配本工作室已有产品 |
| 资料初始化 | POST `/api/admin/content-library/initialize` 仅所有者，携带 `content_digest` 和所选项的 `expected_versions`；只发布未编辑项，已有草稿和版本保留，正常追加变更历史 |
| 配置就绪 | GET `/api/admin/readiness` 只读检查业务、模型凭据、品牌知识、账号授权与已验证格式及各 Agent 归因规则内的回传新鲜度；不返回模型凭据或网站回传密钥 |
| 调度状态 | GET `/api/admin/runtime-status?aid=...` 返回当前允许时段、正在占用的工作名额及失败计数 |
| 通知成员 | GET `/api/admin/notification-recipients` 仅所有者，列出当前工作室的有效接收成员 |
| 站内通知 | GET `/api/admin/notifications` 只返回发给当前成员且已到展示时间的通知；`before`、`before_id` 联合分页 |
| 通知已读 | POST `/api/admin/notifications/{id}/read`；包括查看成员在内可标记自己的通知，保持来源校验；不能标记未发送给自己的通知 |

品牌资料发布增加受影响 Agent 的配置版本，用现有控制边界撤回陈旧排队内容；已确认与结果未知的动作账本不改写。调度规则在接纳新自动工作时读取，正在执行的发布工作流保留重试策略快照。运行名额有过期时间并在活动期间续期，进程故障不会永久占用名额。站内通知按异常发生次数去重，接收范围及可展示时间随通知保存；通知已读与配置权限分开。

后台配置写入、批量额度及 POST `/api/agents/{aid}/budget` 仅限所有者。旧额度接口需要 `expected_version`，读取额度返回 `config_version`。后台只维护业务配置，不提供密钥或底层环境文件编辑。保存配置、运行时额度调整、版本记录和审计在同一事务提交。

公开 `/r/{code}`、`/lead.js`、`/collect/{id}` 和后端转化凭据见 CONVERSIONS.md。公开引用通过受限函数解析租户，再按 RLS 写入，不相信客户端传入的归属。

## 事件和执行

业务事务与 outbox 原子提交。`chat.enqueue`、`task.enqueue`、`supervisor.wake` 携带持久业务引用，不带凭据；包括事件 ID、租户/Agent、schema_version、trace_id、trace_parent。重复投递经业务幂等键及 Temporal workflow ID 去重。单工作室故障不阻塞其他工作室。

创建、消息、偏好命令、授权响应、排期、外部动作和转化各有业务幂等键。同消息键变更正文/素材会拒绝。配置、授权和控制分别有不可变版本；旧卡片、排队内容和旧执行器不能越过新范围。

监督及对话工作流耗尽暂时重试后保留原 cycle/job，通过持久计时继续。归档唤醒已有监督流程并结束排期。工作流改动使用 Temporal patch marker，旧历史可重放。

发布经过持久准备和不可变动作账本：`prepared` → `dispatching` → `confirmed/unknown`。最终点击前核对账号、目标、正文、素材及授权。进入 dispatching 后异常只核对，不盲目重发。Profile 恢复不回滚动作账本；恢复库全局保护优先于动作授权。

内部网关只监听回环地址并使用独立 HMAC。系统网络边界、备份和内部指标是维护者能力，见 OPERATIONS.md。渠道格式和实际开放条件见 CHANNELS.md。
