# lead

本地运行的 AI 社媒运营平台实现，当前验收状态如下。通过对话介绍业务，Agent 保存独立的业务资料与运营偏好，后台独立调度工作，并通过持久化 Camoufox 浏览器执行任务。

本机地址：**http://127.0.0.1:3100**。本轮本地交付与使用范围见 [LOCAL_READINESS](docs/LOCAL_READINESS.md)。

## 当前可用范围

| 能力 | 实现状态 |
| --- | --- |
| DeepSeek 真实对话、逐步了解业务、提取产品信息 | 已接通、已实测 |
| 公开产品网页读取 | 已实测 DeepSeek 原生工具调用真实 Camoufox，读取动态网页、点击 FAQ、保存来源和截图 |
| 独立资料、消息、记忆和不可变配置版本 | 已实现，PostgreSQL 非所有者角色与 RLS |
| 多 Agent 同时运行 | 已实测三个环境同时读取公网网页、同时执行本地发布；达到容量时回收空闲环境 |
| 自主规划 → 生成内容 → 浏览器提交 → 核对结果 | 本地测试站已全链路实测，使用真实 DeepSeek |
| 暂停、接管、退出后恢复、提交结果不确定时核对 | 已实现并验证关键路径 |
| 11 个国际平台的独立浏览器登录入口，含 Telegram | 已提供入口；不会把打开登录页等同于账号已验证 |
| 真实社媒无人值守发布 | Telegram 自有广播频道已实现账号识别、目标授权、一次发布验证卡、提交与核对；实号验收待用户登录及授权。其余 10 渠道的网页发布、图片/视频路径已实现并通过本地 DOM 合约测试；全部平台实号验收仍未完成，详见 [渠道矩阵](docs/CHANNELS.md) |
| 推广链接、网站埋点、点击与访问、服务端转化 | 已实现，服务端独立密钥与事件去重已测试；真实网站接入需公开 HTTPS 地址 |
| 素材、实时通知和运营偏好调整 | 图片/视频真实解码、Agent 范围存储；SSE 自动刷新；对话修改/撤销偏好，取消旧方向的排队内容 |
| OIDC、成员权限、迁移、备份和监控 | 已实现并完成本地数据库/签名协议/恢复/故障测试；真实 OIDC、异地存储和正式节点仍待验收，见 [OPERATIONS.md](docs/OPERATIONS.md) |
| 每个环境独立出口、泄漏检测与第三方设备判定 | 已实现同一受控转发层处理浏览器连接和 DNS、私网阻断、代理失败关闭、双公网出口探针。当前无独立代理，不显示验证通过；新增 Linux 网络命名空间启动边界，OS 通道与原生浏览器完整泄漏验收分别进行，尚未完成生产验收 |

本地测试发布不会计入社媒发布数、访问人数或获客成果。界面没有预填演示流量。当前版本是可运行的工程基础，不能作为 11 个平台均已支持自主运营的商业版本交付。

## 启动与关闭

本次环境已安装依赖。Docker Desktop 保持运行，在项目目录执行：

```sh
.venv/bin/python scripts/run.py start
.venv/bin/python scripts/run.py status
.venv/bin/python scripts/run.py stop
```

本地管理器启动前端、API、任务 Worker、浏览器网关及独立备份监控，并在子进程意外退出后重新启动。数据库与 Temporal 使用持久化 Docker 卷；关闭应用不会删除数据。电脑休眠/关机期间不执行任务，恢复并启动后由 Temporal 继续处理。

进程状态和诊断在 `.state/`，默认只监听本机环回地址。默认采用本地所有者模式；已实现 OIDC、工作室隔离和成员权限，尚未配置外部身份服务。维护者配置见 [运维、权限与恢复](docs/OPERATIONS.md)。不要将开发配置直接改为监听公网地址。

## DeepSeek

项目 `.env`：

```env
DEEPSEEK_API_KEY=填写自己的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

密钥每次调用时读取。之前因缺少密钥而阻塞的消息会在配置后自动重新排队；其他失败的消息可在对话中重试。密钥不通过前端发送，不写入消息或数据库；`.env` 被版本控制忽略，权限为 0600。环境文件本身不是加密保险库。

默认每个 Agent 每个 UTC 日最多使用 100,000 个模型 token。调用前预留，返回后按 provider usage 结算；无法判断是否计费的请求保留预留额。额度按 token 计算，不代表费用报价。高级配置 `LEAD_DAILY_TOKEN_LIMIT` 可调整，下一额度周期生效。

## 浏览器与网络

每个 Agent 的配置、Cookie、IndexedDB、LocalStorage 分别放在 `.state/profiles/<agent_id>/`。一个配置对应一个独立 Camoufox 进程，跨进程文件锁避免重复启动，同一环境串行执行，不同环境并发执行。默认上限由 `LEAD_BROWSER_CONCURRENCY=3` 控制。

本次固定验证版本：Camoufox SDK 0.5.8 / 浏览器 156.0.1-beta.36。已验证 UA、屏幕、平台、Canvas 像素与存储的重启保持，以及不同配置的独立性。Firefox PNG 导出的每次会话附加信息仍会变化，这不等同于 Canvas 像素或已保存身份配置丢失。没有承诺所有指纹项目不变，或第三方一定判为不同真实设备。

可在 `.env` 为 Agent 绑定固定代理：

```env
LEAD_PROXY_<去掉横线并大写的AGENT_ID>=http://user:password@proxy.example:1234
```

浏览器始终连接本 Agent 的受控本地转发器，由它使用绑定的上游代理。目标 DNS 通过同一代理中的固定公网 DoH 解析并固定到公网 IP；禁止私网和本机控制服务，只有本 Agent 的测试站路径例外。绑定代理失败不回落直连，已用真实浏览器故障测试验证。WebRTC、IPv6、HTTP/3 与预取关闭。

高级设置「检查环境」通过实际浏览器访问 ipify 和 Cloudflare，记录设备采样和两个出口结果。还需设置 `LEAD_EXPECTED_IP_<AGENT_ID>=固定公网IP`，两次观测一致且匹配固定值、配置了上游代理、WebRTC 禁用，才能通过固定出口检查；数据库拒绝把同一已验证出口绑定到多个 Agent。结果有效 15 分钟。此检查不代表第三方一定无法关联身份，也不代替 OS 防火墙及 DNS/WebSocket/地区一致性等完整检测。

默认本地所有者模式允许对自有 Telegram 频道完成显式验证；不会显示为独立出口已经通过。`LEAD_DEPLOYMENT_MODE=production` 时，十一个平台每次新提交前会重新测量出口并核对身份，失败停止提交。新增正式 TLS 连接与服务模板；完整生产安全与部署验收仍未完成。

浏览器远程接管的输入仅在有效接管期内开放，登录资料不进入 Agent 对话。验证码与平台身份验证由用户在浏览器中完成。

## 访问统计

在 Agent 的「获客效果 → 接入网站统计」生成网站代码及推广链接。公网使用需要先配置 `LEAD_PUBLIC_URL=https://实际部署地址`；当前本机链接只能在本机使用。

- 推广链接请求仅算点击，已识别的预览机器人不计入有效点击。
- 网站实际回传会话后才计访问；仅把带当前 Agent 推广归因的会话计为其获客。
- 当前有效访问规则 `engaged-v1`：页面前台活跃至少 10 秒，服务器同时检查两次事件的间隔。
- 数据标记为 `client_observed`，不是经过服务端验证的成交。服务端转化使用独立回传凭据并与归因会话关联，未接入或数据过期时显示空值。接入方法见 [CONVERSIONS.md](docs/CONVERSIONS.md)。

客户端埋点可被伪造，当前规则不能等同于反欺诈认证或保证人工访问。

## 验证

```sh
# 业务规则、真实数据库权限、安全边界及 DeepSeek 请求格式
PYTHONPATH=services/api .venv/bin/python -m pytest tests/test_core.py -q

# 需要本地 API 正在运行，使用真正的 Camoufox
LEAD_BROWSER_TESTS=1 LEAD_WORKFLOW_TESTS=1 PYTHONPATH=services/api .venv/bin/python -m pytest tests -q

# 使用自己的 DeepSeek 密钥，产生少量真实模型调用
.venv/bin/python scripts/test_model.py
.venv/bin/python scripts/verify_runtime.py
.venv/bin/python scripts/verify_public_browser.py
.venv/bin/python scripts/verify_environment.py

# 前端
cd apps/web
npm run typecheck
npm run build
```

验收脚本的临时业务会自动归档。测试区分公网浏览实测、真实模型调度、本地发布站、十一平台 DOM 合约测试和真实 Telegram 频道实测，不互相替代。报告保存在 `.state/public-browser-verification.json`、`.state/runtime-verification.json`、`.state/environment-verification.json`、`.state/concurrency-verification.json`、`.state/verification-tests.xml`；截图和提交证据在 `.state/evidence/`。详细状态见 `docs/ACCEPTANCE.md`。

## 工程结构与后续工作

剩余任务、优先级、依赖与交付标准见 [开发执行计划](docs/DEVELOPMENT_PLAN.md)。当前不是原方案全量交付状态。

详细数据流、边界和发布前要求见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。前一阶段完整实施规划保留在[IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)，它描述目标架构，不代表本版本已全部实现。

首次重建环境需 Python 3.12+、Node 20.9+、Docker，以及兼容主机的 Camoufox 浏览器。本机已验证 Python 3.14.2 / Node 24.20.0 / macOS ARM64。Python 版本锁定于 `requirements.lock.txt`，前端锁定于 `apps/web/package-lock.json`，基础服务使用镜像摘要固定。

## Telegram 实号验收

1. 在 Agent 对话中提供业务信息。连接账号 → Telegram，在专属浏览器内登录并点击「登录完成」。
2. 在对话中发送自己有发布权限的公开频道 `https://t.me/频道名`。系统读取当前账号编号对应的稳定 ID，核对频道和可写状态。
3. 点击频道授权卡，范围为此频道发布、每天最多 3 条；不会授权私信、加入群组或采集他人消息。
4. 系统显示完整验证消息。只有点击「发布此条验证」才会执行一次真实网页发布。收到唯一消息 ID 和截图后，这个目标才标记为发布已验证。验证消息不计推广成果。
5. 免费启动后，Supervisor 持续按业务、偏好、近期内容及真实效果规划，并在授权范围内执行。正常帖子无需逐条审批。未知结果只核对，不再次点击；登录或权限异常需要重新连接。

Telegram 的文字、图片、视频网页路径均已实现；其余十个平台的格式范围、权限状态与测试边界见 [CHANNELS.md](docs/CHANNELS.md)。没有登录和授权自有测试频道之前，M3 不能标为验收完成。

## GitHub 与持续检查

公开源码仓库：[zdaiwmm/lead](https://github.com/zdaiwmm/lead)。仓库独立于其他项目，密钥、登录资料、数据库、素材及运行截图不提交。GitHub Actions 执行数据库权限、媒体处理、业务规则和前端构建检查；真实浏览器合约测试及真实模型测试在本机单独执行，CI 通过不替代平台实号验收。
