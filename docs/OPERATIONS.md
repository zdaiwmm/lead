# 运维、权限与恢复

本文件面向平台维护者。这里的参数不进入新手创建 Agent 的流程。开发环境仍默认本地所有者；正式配置与真实第三方验收需要分别完成。

## 本地运行

`scripts/run.py start` 启动 Next.js、API、浏览器网关、Temporal Worker 和独立的维护进程。维护进程每 10 秒采样，每 10 分钟备份业务数据库与关联文件，每天尝试在浏览器安全关闭点备份 Profile。浏览器忙、用户正在接管或任务正在执行时延后备份。普通用户无需逐条审批或每日启动。

只有一个浏览器网关控制本机 Profile。每个 Agent 独占一个原生进程与文件锁，各自串行，不同 Agent 并行。浏览器固定使用已验证的 `official/156.0.1-beta.36` 构建，不跟随全局 active 变化。浏览器崩溃后，下次任务重建同一 Profile；数据库将未确认提交保留为待核对。Firefox 崩溃前尚未落盘的最新页面存储不能视为已持久保存；业务记录和动作账本写入 PostgreSQL。

电脑关机或休眠期间不能执行任务。重启本地服务后 Temporal 恢复排期；长期无人值守应部署到持续在线节点。

## 版本化迁移

```sh
.venv/bin/python scripts/migrate.py
```

迁移入口使用 Alembic，持有数据库事务级锁后升级，再单独完成运行角色和能力登记。`infra/migrations/sql/0001_baseline.sql` 与 `0002_runtime.sql` 是冻结版本，不随参考 schema 文件修改。旧的本地安装在验证关键字段和 RLS 后登记基线，随后增量升级；空库与既有库两条路径都经过真实 PostgreSQL 测试。

不通过降级脚本删除业务表。代码回退保留兼容字段；恢复备份必须进入新数据库并保持外部写入关闭。迁移账户、业务账户、身份解析账户分别使用 `lead_owner`、`lead_runtime`、`lead_auth`；后两者没有超级用户或绕过 RLS 的能力。业务账户不能修改成员、会话、恢复控制和备份目录。

## OIDC 与工作室

在平台维护者环境中设置：

```env
LEAD_AUTH_MODE=oidc
LEAD_PUBLIC_URL=https://lead.example
LEAD_OIDC_ISSUER=https://identity.example/realm/lead
LEAD_OIDC_CLIENT_ID=lead
LEAD_OIDC_CLIENT_SECRET=由身份服务提供
```

身份服务登记回调 `https://lead.example/api/auth/callback`，启用 Authorization Code、PKCE S256 和 OpenID。正式服务必须 HTTPS；本机开发身份服务只有显式允许环回 HTTP 时可以例外。

验证 issuer、audience、签名算法、公钥、有效期、nonce、azp 与浏览器绑定的单次 state 后，平台签发不透明会话；数据库只保存会话哈希。身份服务令牌、PKCE verifier 和用户密码不进入业务对话、模型或工作流历史。已有登录流的 nonce/verifier 使用私有密钥加密保存，五分钟过期并原子消费。

首次登录创建独立工作室，不能直接接管既有本地 Seka。迁移本地所有者数据需要维护者核实已登录身份的 issuer/sub 后，显式授予既有工作室所有者权限；不能把第一个公网登录者自动绑定成本机所有者。

角色为所有者、运营成员和仅查看。每个请求重新检查成员状态；撤销立即影响后续请求及 SSE，至少保留一名活动所有者。切换工作室后，旧页面携带的范围标识会被拒绝并刷新，避免跨标签页写错工作室。专业入口在「设置 → 工作室与成员 → 管理成员」。当前尚未对真实外部身份服务进行部署验收；本地使用真实 RSA 签名和隔离模拟身份服务验证协议与权限。

## 加密备份

本地维护进程首次运行在 `.state/keys/backup.key` 生成 32 字节密钥（0600）。这方便本地验证；磁盘损坏时，密钥和本地备份可能同时丢失。正式环境必须把密钥放在仓库和备份目录之外，并独立托管、保留旧版本。不能只上传备份而没有可恢复的密钥。

```sh
.venv/bin/python scripts/backup.py key-init --key-file /安全位置/lead-backup.key
.venv/bin/python scripts/backup.py database --key-file /安全位置/lead-backup.key
.venv/bin/python scripts/backup.py profile --agent AGENT_ID --key-file /安全位置/lead-backup.key
```

数据库通过 PostgreSQL 自身快照导出，文件清单使用同一个快照。关联的图片、视频、网页正文、截图及证据单独形成配对加密包，恢复时校验 SHA256 和大小。不能只保留数据库包而丢弃其配对文件包。活跃业务缺失文件时不生成成功恢复点；已经归档的历史缺失引用会在清单中明确登记。

Profile 备份先封住新的控制请求、提升控制版本、关闭原浏览器并取得 OS 文件锁，保存身份和原生存储；不复制可重建缓存。维护锁过期之前不能启动第二个浏览器。自动备份保留原来的运行意图，并在释放后唤醒监督工作流。

格式为分块 AES-GCM，包含经过认证的头、分块顺序与终止标记；截断、错密钥、篡改和追加数据都会失败。完成认证后才发布恢复明文。备份不覆盖现有文件，敏感临时文件仅放在私有恢复目录。

自动管理的副本保留数据库最近三份及容量许可的七日每日恢复点、每个环境最近两份 Profile，并保留数据库所需配对文件。只清理目录内、在目录册中明确由自动任务管理的副本；手工备份和其他目录不清理。最低保留集合可能超过附加日副本的容量预算，磁盘不足会告警并停止新写入，不能据此承诺任意数量 Agent 占用固定总空间。

### 异地存储

设置 `LEAD_BACKUP_S3_URI=s3://bucket/prefix`，并通过节点身份或独立凭据设置 S3 访问权限。非 AWS 端点使用 `LEAD_S3_ENDPOINT`（正式环境 HTTPS）；可设置 `LEAD_S3_KMS_KEY_ID`。

文件在上传前已加密；上传附带服务器端加密要求、条件创建及 SHA256，远端返回匹配的服务器校验结果后才登记成功。配对文件先上传，再上传数据库包。正式发布要求最近 15 分钟内数据库与配对文件都已完成异地校验。当前 SDK 合约测试通过，尚无真实对象存储账户完成异地灾备演练。活动 Profile 所在磁盘的静态加密也必须在实际节点另行验证，不能用备份加密替代。

### 恢复

```sh
.venv/bin/python scripts/backup.py restore-database \
  --source /备份目录/DATABASE_BACKUP_ID.leadbackup \
  --database lead_restore_drill_20261029 --key-file /安全位置/lead-backup.key
.venv/bin/python scripts/backup.py restore-profile \
  --source /备份目录/PROFILE_BACKUP_ID.leadbackup --key-file /安全位置/lead-backup.key
```

数据库包与配对文件包保存在同一目录。只允许新建 `lead_restore_*` 数据库，拒绝覆盖既有业务库。恢复后立即提交 `recovery_required` 保护，并撤销旧会话；重新登记运行角色、恢复身份函数的非登录所有者和最小调用权限，防止无 ACL 的导入扩大权限；后续文件恢复失败也不会撤销这层保护。文件安装不覆盖内容不同的已有文件。Profile 只进入恢复隔离目录，不替换在线环境，也不回滚动作账本。

切换到恢复库之前，需确认旧写节点已经隔离，核对备份窗口内的真实账号内容、动作证据及未知提交；身份/登录状态和所有受影响目标复核后才能解封。当前没有一键解除恢复保护的入口，不能只恢复文件就自动继续发布。跨节点在线 Profile 安装、账本缺失窗口的自动核对仍待实现和真实账户验证。

设计目标是数据库 RPO ≤15 分钟、Profile RPO ≤24 小时、RTO ≤4 小时。自动时间表、过期保护和小样本本地恢复已经实现并测试；真实异地故障、完整数据量和目标硬件下的 RPO/RTO 尚未验收。

## 运行观测

API、工作流活动、模型请求及内部浏览器 RPC 使用 OpenTelemetry 传播 traceparent。事件和任务保存追踪关联。只导出路由模板、状态、耗时、操作类型和错误类型；不导出请求正文、表单、Cookie、密钥、模型提示词或原始 URL 查询。私有文件在 `.state/telemetry/`，滚动保存；可设置 `LEAD_OTLP_TRACES_ENDPOINT` 接入 HTTPS 接收端。

维护进程保存 `.state/ops-status.json`；状态过期会明确显示不新鲜。指标覆盖队列年龄、未知提交、账号授权、实际进程 RSS/CPU、磁盘、服务心跳、备份时间与异地状态。告警按事件键去重并可恢复，没有向外部人员发送消息。

`/internal/status`、`/internal/metrics` 使用独立运维凭据，只在内部监听；前端代理不提供这些路由，生产反向代理也明确阻断。未配置远端收集器或告警接收系统，不应声称已接通外部监控平台。

## Linux 浏览器出口边界

正式模式要求 Linux、普通系统用户、原生 Camoufox 与可运行的 rootless bubblewrap。每个浏览器使用独立网络命名空间，只有环回接口；唯一出口是映射进来的该 Agent 专属 Unix 代理通道。宿主转发器继续执行同一上游的 DNS、目标地址校验和失败关闭。只有本环境存储可写、本环境素材只读；应用代码、`.env`、其他 Profile 与运维密钥不映射进浏览器。原生 Juggler 管道保留给控制器。

这没有为每个 Agent 启动虚拟机或完整操作系统，也不宣称能保证第三方永远认定为不同真实设备。需要扩展原生桌面应用时，另行引入更完整的计算机环境。当前设备身份不能静默从 macOS 改成 Linux；部署新节点或迁移需要显式验证。

一次性 OS 通道测试：

```sh
docker build --secret id=lead_ca,src=/etc/ssl/cert.pem -f infra/namespace-test.Dockerfile -t lead-namespace-test:local .
docker run --rm --network none --cap-drop ALL \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --security-opt systempaths=unconfined \
  lead-namespace-test:local
```

Linux 构建时 CA 路径通常为 `/etc/ssl/certs/ca-certificates.crt`，macOS 示例为 `/etc/ssl/cert.pem`；公开 CA 只通过构建 secret 临时提供，不写入镜像。这些选项只用于一次性测试容器内部创建嵌套命名空间；不修改宿主网络、不开启特权容器。测试包含三命名空间、UDP/DNS/IPv6/宿主环回直连、专属通道与文件边界；不包括 Linux 原生浏览器上传与真实代理故障的完整矩阵。实际浏览器节点还需完成端到端验收。当前本机 macOS 仍使用原生 Profile 及应用层受控代理。

## 长期验证

```sh
.venv/bin/python scripts/soak.py start --hours 72
.venv/bin/python scripts/soak.py status
.venv/bin/python scripts/soak.py resume
.venv/bin/python scripts/soak.py stop
```

使用独立测试工作室、三个 Agent、真实 Temporal 和本地浏览器发布，重复验证任务去重、资料/记忆隔离、图片/视频解码存储、Profile 重新打开及资源采样。真实社媒发布和持续模型付费负载不在这项测试中；十一平台媒体网页合约由单独原生测试覆盖。

状态、时间、任务耗时与资源样本在 `.state/endurance/`。进程不在运行时不能把过去墙上时间当作成功观察；异常会保留失败状态和证据，不会显示绿色完成。成功预演只回收该次验证创建的已归档 Profile 存储，保留身份与证据。72 小时未实际经过并检查资源趋势之前，不能写成长期耐久已通过。

## 正式部署入口

`infra/systemd/lead@.service`、`infra/Caddyfile` 和 `scripts/service.py` 提供正式入口模板。使用独立在线 Linux 节点、证书验证的 PostgreSQL 18、持久 Temporal（外部部署或托管服务）和异地对象存储。`infra/compose.yaml` 中的 start-dev Temporal 只用于开发，不能作为正式集群。

先在独立身份、数据库和备份凭据下执行迁移、部署前检查，再启动 `lead@api`、`lead@browser`、`lead@worker`、`lead@maintenance`、`lead@web`。共同配置放在 `/etc/lead/common.env`，各服务凭据放在独立 EnvironmentFile；文件权限和具体资源上限由节点维护者落实。模板不代表已经部署到生产。

Temporal 支持 `LEAD_TEMPORAL_ADDRESS`、`LEAD_TEMPORAL_NAMESPACE`、`LEAD_TEMPORAL_TLS=1` 与证书/私钥或 API key；正式模式拒绝明文连接。数据库需要 `LEAD_DB_SSLMODE=verify-full` 和可信 CA。正式入口还要求 OIDC、HTTPS、异地备份与独立密钥；未配置会拒绝启动。

参考：[bubblewrap 参数与边界](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)、[Camoufox 原生启动配置](https://camoufox.com/python/usage/)。

例行 Profile 维护返回可恢复的 `browser_busy`，不会转成每日需要用户核对的授权任务。发布 Activity 对临时故障持续退避重试，单次调度总窗口为 24 小时；无效内容、越权和缺失资源等确定性错误不按临时网络故障无限重试。进入提交后的恢复始终经过原动作账本核对。

## 本轮本地收尾工具（2026-10-29）

这些命令供维护者使用，不放进用户创建 Agent 的表单。先执行 `.venv/bin/python scripts/install.py check`；`schema-preview` 只报告数据库版本、浏览器构建分布、适配版本与回退要求，不修改数据。新增迁移截至 `0010_browser_metering`，恢复旧备份时会在隔离的新库中升级结构并重新收紧权限，不回滚在线账本。

### 固定出口由平台分配

用 `.venv/bin/python scripts/network.py add < .state/private-exit.json` 从权限为 0600 的私有文件导入。字段为 `label`、`url`、`expected_ip`，可附 `region`（两位国家代码）、`timezone`、`locale`、`expires`。必须是真实可核验的固定公网 IP；文档中的保留地址不能用于通过检测。凭据不放在命令参数、模型上下文或页面，数据库只存认证加密密文，密钥位于私有 `STATE/keys/network.key`。

`list` 只输出维护元数据；`disable --id UUID`、`quarantine --id UUID` 停止新操作；`enable --id UUID` 恢复同一绑定。一个 Agent 仅有一个有效绑定，一个出口仅分给一个 Agent；容量不足自动等待，故障/到期不借用其他出口，也不静默换 IP。归档才释放分配。没有配置出口池的本地环境保留 shared_development，不能标独立出口通过。原逐 Agent 环境变量仅为兼容路径，新增配置用池。

Linux 启动器经专属 Unix socket 提交带随机认证的网络命名空间实测证明；TCP 和伪造证明不能提升状态。身份检查核对双出口、国家、时区、语言、稳定设备观测及漂移。生产提交前重新检查；本机 Mac 未提供 Linux 系统边界。三组实际独立出口及完整泄漏矩阵仍需外部环境，不能以证明交换本身替代。

### 恢复切换，原节点与恢复节点不能同时写

先按已有备份命令恢复到新的 `lead_restore_*` 数据库，Profile 保持在 `restore-quarantine`。然后显式执行：

```sh
.venv/bin/python scripts/recovery.py prepare --target NEW_DATABASE --source OLD_DATABASE --node-root "$PWD/.state/recovered-nodes/NEW_NODE"
.venv/bin/python scripts/recovery.py isolate-source --target NEW_DATABASE --case CASE_UUID
.venv/bin/python scripts/recovery.py install-profile --target NEW_DATABASE --case CASE_UUID --quarantine PROFILE_QUARANTINE
.venv/bin/python scripts/recovery.py review --target NEW_DATABASE --case CASE_UUID --target-id TARGET_UUID --note '实际外部动作窗口核对记录'
.venv/bin/python scripts/recovery.py release --target NEW_DATABASE --case CASE_UUID
```

`isolate-source` 会隔离显式指定的源库并暂停其中的 Agent，因此只在真实切换时执行。不能联系并确认旧控制者已被数据库屏障隔离时，拒绝激活。每个恢复目标均须核对；Profile 不覆盖在线目录，构建/SDK/归属/文件哈希不匹配即停止。配对素材复制到新节点后重定位；恢复窗口的内容标识单独保留用于去重。

释放后 Agent **保持暂停**，旧社媒授权和身份有效期失效；旧会话/租约关闭。使用新的 Temporal 命名空间，旧任务历史不重放到新账本。新节点私有 `launch.json` 只给出 `LEAD_STATE_DIR`、`LEAD_DB_NAME`、`LEAD_TEMPORAL_NAMESPACE`，仍需维护者停止旧服务、用这些环境启动新服务，再完成必要的新授权。回退不解除旧库屏障，不覆盖任何在线 Profile。实测使用的是新建测试库，未对用户业务库切换；异地完整规模 RPO/RTO 仍需目标环境演练。

### 身份绑定、用量与数据生命周期

启用真实 OIDC 后，先让将成为所有者的用户完成一次验证登录，再用 `scripts/install.py bind-owner --tenant UUID --issuer ISSUER --subject SUBJECT` 显式绑定。它只接受仍有效且成员未撤销的已验证身份，不把第一个登录者自动设为本地数据所有者。正式服务拒绝未经核验的活动 Profile 存储；当前检查支持 Linux 挂载链中实际的 dm-crypt 加密卷。云厂商透明卷加密等无法由此证明的方式保持未知，需补充对应主机集成，不能设置一个布尔值假装通过。

用量按调用保存输入/输出、DeepSeek 实际缓存命中、总 token、请求数；会话保存采样 CPU 下界、RSS 合计峰值、文件句柄和转发字节，不记录访问正文。历史未细分的会话保留未计量状态。私有 `STATE/operator-rates.json` 格式为 `version`、`effective_at`、`currency`、`models[MODEL]` 下的 `input_per_million`、`cached_input_per_million`、`output_per_million`。只有供应商确实报告缓存拆分、价格齐全时才按该版本计价；缺价格/未知用量不填零，不据此宣称免费额度已具有经济可持续性。

用户通过高级入口导出业务、对话、偏好、发布记录和素材 ZIP；不导出浏览器 Cookie、凭据或代理身份。维护端同样可用 `scripts/lifecycle.py export --tenant UUID --agent UUID --output NEW_FILE.zip`。在线导出上限 256 MB，较大资料用维护端同一 export 命令加 `--part 1`、`--part 2` 等分卷；各卷的 lead.json 记录分卷序号、总数及完整素材清单，导出期间应避免继续修改素材。

`purge-preview --tenant UUID --agent UUID` 仅预览。只有明确归属、已归档七天以上、无未核对外部提交且控制权已释放的数据，才可 `purge ... --confirm-agent SAME_UUID`；不遍历删除历史环境。检查点先于数据库提交落盘；文件清理中断后可 `finish-purge --tenant UUID --agent UUID`，它再次确认数据库删除已提交。既有加密备份按独立保留策略过期，当前数据清除不声称已立即抹除所有恢复点。本轮只用新建测试数据验证清除路径。

### 完整混合运行器

`soak.py start --hours 72 --interval 300` 使用新的 mixed-v2 起点，不承接旧部分负载时长。每轮实际运行三 Agent Temporal 文字任务、21 种原生上传、确认丢失后的重开核对、接管时 B/C 继续。首次和每十二轮另跑独立 TCP 数据库中断、原生浏览器崩溃、持久重试/历史重放测试，不停止用户数据库。受控响应等待用于隔离性验证；真实 DeepSeek 另由有调用上限的 `evaluate_agent.py`、`verify_runtime.py`、`verify_ui.py` 验证，不进行无界付费模型循环。

5 秒采样资源与私有状态接口耗时，报告记录执行源码哈希、硬件、构建和事先固定的阈值。72 小时、至少十二轮、预热后足量样本、无观测缺口、资源增长与 API P95 均满足才标 passed。短跑输出 completed_not_accepted 是预期结果，不能当长期验收。主机睡眠、代码改动或缺场景时不会凑够时间自动通过；`status` 读取私有报告。暂停测试用 `stop`，它只归档和回收本次 Verification 工作室创建的确切环境，保留报告/账本/证据。

维护唤醒与重启只重新核对持久计划，不跳过其下次工作时间。业务事实纠正与偏好变更一样会形成配置版本并使旧排队内容失效；缺少必要信息的等待计划将问题发到对话，未收到新信息时不重复追问。
