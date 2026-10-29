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

公开 `/r/{code}`、`/lead.js`、`/collect/{id}` 和后端转化凭据见 CONVERSIONS.md。公开引用通过受限函数解析租户，再按 RLS 写入，不相信客户端传入的归属。

## 事件和执行

业务事务与 outbox 原子提交。`chat.enqueue`、`task.enqueue`、`supervisor.wake` 携带持久业务引用，不带凭据；包括事件 ID、租户/Agent、schema_version、trace_id、trace_parent。重复投递经业务幂等键及 Temporal workflow ID 去重。单工作室故障不阻塞其他工作室。

创建、消息、偏好命令、授权响应、排期、外部动作和转化各有业务幂等键。同消息键变更正文/素材会拒绝。配置、授权和控制分别有不可变版本；旧卡片、排队内容和旧执行器不能越过新范围。

监督及对话工作流耗尽暂时重试后保留原 cycle/job，通过持久计时继续。归档唤醒已有监督流程并结束排期。工作流改动使用 Temporal patch marker，旧历史可重放。

发布经过持久准备和不可变动作账本：`prepared` → `dispatching` → `confirmed/unknown`。最终点击前核对账号、目标、正文、素材及授权。进入 dispatching 后异常只核对，不盲目重发。Profile 恢复不回滚动作账本；恢复库全局保护优先于动作授权。

内部网关只监听回环地址并使用独立 HMAC。系统网络边界、备份和内部指标是维护者能力，见 OPERATIONS.md。渠道格式和实际开放条件见 CHANNELS.md。
