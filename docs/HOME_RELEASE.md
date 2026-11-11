# 家庭服务器发布

新任务按[小修复交付](SMALL_FIX_RELEASE.md)提前准备并冻结批次；现有精确 main CI、生产审批、维护锁和独立核验要求保持。可选下载缓存仅在显式传入 `--cache-dir` 时启用；它不改写在途发布，不自动安装到节点。

## 隔离 Worker 的模型连接

模型凭据和网络通道分别核验。API 可读到密钥不表示 Worker 的挂载命名空间也能读取。为 Worker 绑定部署端既有凭据文件到应用 `ROOT/.env`，或使用其受控 EnvironmentFile；仅检查有效值是否存在，不输出密钥。保持文件权限与只读挂载。

隔离网络可使用节点已有的官方模型 TLS 通道。部署方为每个 Agent、供应商设置 `LEAD_MODEL_SOCKET_<AGENT_UUID去横线大写>_OPENAI` 或 `_DEEPSEEK`，值为 Worker 内可见的 Unix socket 绝对路径。Agent ID 和实际节点路径只留在私有配置。未配置的 Agent/供应商保持原连接方式；显式配置空值、相对路径或不支持的端点会失败，不回落直连。仅接受对应供应商的官方 HTTPS 主机，保留证书与主机验证；环境代理和重定向关闭。通道宿主端也应限制对应官方目的地，不能暴露通用转发器。socket 路径由运行用户不可修改的部署配置控制。

systemd 示例（部署时替换占位符，不直接照抄）：

```ini
[Service]
BindReadOnlyPaths=/secure/existing-model.env:/run/lead-home/bin/original/.env
BindReadOnlyPaths=/secure/sockets/openai.sock:/run/lead-model-egress/openai.sock
BindReadOnlyPaths=/secure/sockets/deepseek.sock:/run/lead-model-egress/deepseek.sock
EnvironmentFile=/secure/worker-model-routes.env
```

socket 文件绑定会固定当时的 inode；通道服务重新创建 socket 后需按安全点重启 Worker 重新绑定。需要证书覆盖时使用包含公共根证书的受审阅信任集，不使用关闭验证的选项。

已有临时单文件 `model.py` 挂载时，版本检查应继续拒绝。先完成本修复的 PR/CI/main，再与其他待发布项统一确认精确 SHA。维护窗口内核验并保存旧配置，撤除临时代码覆盖、保留凭据和 socket 挂载、登记上述路由环境变量；重新启动后让 `current` 核对旧正式镜像，再走已批准新版本的正常发布入口。不要删掉版本检查或为未知覆盖添加白名单。不要在旧版本不支持路由时重试业务。新版本独立回读有效密钥布尔值、只读模型查询、持久 Worker 版本回执和原消息结果；HTTP 429、仅查到模型或仅服务 active 均不能视为生成成功。

发布日志分别保存配置修复、模型服务错误、用户批准的模型切换和原消息最终状态。不能自动更换供应商、重发社媒内容或修改失败账本为成功。

在 GitHub Actions 的 **Approve a home release** 选择 main，输入已确认的完整应用 SHA。审批环境 home-release-approval 应限制仅 main，要求仓库所有者审核。主机只需主动向 GitHub 出站访问。公共仓库的任意 CI 作业都在 GitHub 托管节点运行。

工作流检查提交是合入本仓库 main 的 PR 合并结果，同一 SHA 的最新主线 CI 成功且 backend、frontend、namespace 三个任务均通过。在 home-production 创建 task 为 lead-home-release 的 Deployment，记录精确 SHA、CI run/attempt 和审批运行。工作流绿色仅表示请求已登记。线上状态由节点独立上报。

## 已运行节点的接入约定

1. 在家中先核对线上仓库、真实路径、版本、启动方式、持久数据与现有更新机制。记录上个可恢复版本，以及数据库迁移、正在执行的任务和备份对回退的限制。
2. 由已有更新程序轮询本仓库 home-production 的 lead-home-release 请求。只接受上述工作流完成成功、工作流 main/手动/首次运行、运行标题及 SHA 与请求吻合且精确 CI 仍成功的请求。节点凭据限本仓库读取和 deployments 状态回传。
3. 绑定目标版本，核对当前线上 SHA 是候选的祖先。以持久请求 ID 和单写锁防重入；离线积压、已被更新版本覆盖或先前结果未知时停下来核对。切换前先保存开始状态并向 GitHub 登记 in_progress。中断时查同一请求，不重复执行迁移或切换。
4. 调用节点现有备份、预检和发布入口。保留配置、数据库、任务记录和浏览器资料。另起只读验收，检查真实运行 SHA、Web/API、持久 Temporal 和工作进程、隔离测试任务。验收不向真实社媒账号发内容。
5. 仅在独立核验通过后向同一个 deployment 的 statuses 回传 success；失败标记 failure/error 并保留脱敏诊断。临时网络中断只补传已有结果。首次接入应由节点维护者先演练只读核验和回报，再确认精确版本切换。

仓库提供审批入口和可只读运行的节点轮询工具。家中节点的实际安装目录、执行入口及线上版本由节点交接盘点补齐。

## 节点接单工具

Python 3.11+、GitHub CLI 和可出站访问 GitHub 的 Unix 节点可以运行 `python3 scripts/poll_home_release.py` 只读查看请求。工具校验审批工作流上传的凭证中绑定的 deployment ID、SHA 和 CI 编号。节点实际运行位置及服务管理方式确认后，编写节点本地适配程序，放在应用更新目录外，权限只对维护账户开放。适配程序必须实现：

- `current` 输出当前实际运行的完整 SHA。
- `deploy <SHA>` 调用节点既有的预检、备份和精确版本部署入口，并等待完成。
- `verify <SHA>` 独立核对真实服务及版本，全部通过才返回退出码 0。

先以当前版本测试 current 与 verify。接入完成后由现有调度器周期执行 `python3 scripts/poll_home_release.py --hook /绝对路径/节点适配程序 --state-dir /绝对路径/持久发布记录`。只在一台获指定的节点启用。该目录应与应用版本隔离并持久保留。请求超过一天需要重新确认；已开始的请求只继续独立核验或回传状态。登记开始前失败可修复后再检查。节点部署结果不明时先查运行状态。

### 家庭 systemd 节点适配

任务分支提供 `scripts/home_systemd_release.py`。维护者将经过审阅的脚本安装在应用版本之外。当前共享挂载布局会触发保护性退出。首次纳管需要一次单独的主机变更；先保存运行进程的实际代码、构建产物、挂载及回退清单，并解决 Chamber 对相同服务和持久数据的引用。旧 PC 包交换工具仅更新指定 Python 文件，设备发布器仅管理指定执行器。这两个工具都不能给整个 lead 实例签发版本证明。

**版本与持久化**　节点从 GitHub 取经过审批的 SHA。维护者在加密卷的 `lead-git-releases` 下管理只读镜像挂载点 `<完整 SHA>/`，其中包含 `REVISION`、完整的 `services/` 与建好的 `web/`。准备阶段必须从同一 Git 提交构建、校验产物摘要与来源，并留存旧镜像。镜像内这两个目录应为真实目录。裸镜像 `lead.git` 的 `refs/heads/main` 从指定 GitHub 仓库更新。准备与构建使用受限身份，根账户只接收核验过的产物。节点的配置、数据库、任务、备份、浏览器资料和缓存留在镜像之外。

**服务绑定**　永久 selector `current` 是指向同目录 SHA 名的相对符号链接。映像必须在引导时先挂载，两个版本在整个发布和恢复期间一直可用。使用既有 systemd 的 `BindReadOnlyPaths=`，让 API、collector、worker、browser、maintenance 单元把 `current/services` 绑定到 `/run/lead-home/bin/original/services`，Web 把 `current/web` 绑定到 `/run/lead-home/bin/web`。保留 Web 缓存的独立绑定。维护者须核对各单元 ExecStart、解释器、应用搜索路径、Web 工作目录和对持久目录的依赖。当前 PC 缺失的 lead 单元须先确定实际部署归属。全套单元和旧基线均验收后才能开放接单。

**构建与取件**　Linux 推送 CI 从同一 checkout 完成前端构建后，由 `scripts/build_home_image.sh` 封装 services、Web 和运行依赖。节点在低权限账户下运行 `python3 scripts/prepare_home_image.py <SHA> <私有暂存目录>`。它复核 main 的 CI、run/attempt 和 GitHub 归档摘要，再读 SquashFS 中的 REVISION。产物过期或摘要不符会退出。首次安装时将 home_mount_image.py 安装为受信任的 mount-image，将 prepare_home_image.py 和 request_home_release.py 安装到同一目录。节点调度器确认审批后，deploy 在缺少候选时调用该入口。它从 GitHub 公共 API 独立验证精确 main CI 及归档元数据，只用专属 lead-release-fetch 账户及该目录的受控 gh 下载。根账户在私有目录中验证同一份归档并创建永久只读 systemd mount 单元。镜像随原 lead-home-live.target 挂载。部署前本机裸仓库从固定的 GitHub 地址刷新 main 并再次核对祖先。

**节点专用检查**　将受审阅的 `scripts/home_release_fence.py` 和 `scripts/home_verify_workflow.py` 分别安装为 `/usr/local/lib/lead-home-release/check-fence` 和 `verify-workflow`。两个入口使用该目录下只由管理员维护的 venv，依赖采用仓库固定的 psycopg、temporalio 版本。

- 安装 `infra/systemd/lead-home-release-fence@.service`。独立 OS 与数据库身份名均为 `lead-release-fence`，使用节点私有 `/etc/lead-home-release/pg_service.conf` 中同名 libpq 服务，经 Unix socket 连接**专属 lead 库**。配置需能跨租户读取安全点并在脚本列出的表持有 SHARE 锁。无法完整读取或获取锁时检查拒绝放行。已有业务须排空。检查锁后，适配器关闭专属 ingress socket 及现有连接，通过 loopback 验收；切换或回退验收通过后重新开放 socket。首次纳管须确认该入口仅服务 lead。进程从取得锁到新版本验收结束一直持有独立数据库会话。重复持锁、尚在运行的任务、未决外部动作均拦截。发生异常时保留维护单元，管理员须先核对版本、任务、锁与访问入口，再决定恢复服务。
- 将同一镜像的 REVISION 只读绑定到 worker 的 `/run/lead-home/release-revision`，在专属 worker 启用 `LEAD_HOME_RELEASE_PROBE=1`。worker 每次正常启动在独立的 per-invocation 队列执行无业务输入的合成任务，并返回它读取的 SHA 和 systemd InvocationID。验收端使用 `/etc/lead-home-release/temporal/` 内的 address、namespace、ca.pem、client.pem、client.key 连接同一持久 Temporal。使用独立只读 mTLS 身份，仅查询完成的工作流、运行 ID、队列与结果。verify 不创建任务。
- 旧共享节点可先用 `scripts/home_code_inventory.py` 查看受控代码摘要。管理员用同一脚本的 `--archive` 在已挂载的加密卷内保存有效 Python 模块及已编译 JS/CSS/WASM 的原字节和逐服务映射。归档按内容去重、原子落盘，输出归档摘要。Web 构建的其余文件、服务配置、可恢复数据库及 Profile 仍按节点既有备份流程保留。

**接单与恢复**　先运行 `current` 和 `verify <完整 SHA>`。前者只在系统各应用进程实际看到同一只读镜像且没有未知代码覆盖时输出 SHA。调度端运行前文的 `poll_home_release.py`，只需节点出站访问。可使用 infra/systemd/lead-home-release-poll.service 和同名 timer。受信控制器、hook 及请求日志由 root 持有；所有 gh 网络子进程先清空环境、放弃附加组并降权到独立 lead-release-fetch 身份。该身份只持有单仓库 contents/actions 只读、deployments 读写权限，无 sudo 或应用数据访问权。归档由控制器核对 GitHub 摘要后直接读取固定 JSON 成员。初次主机纳管并通过 current / verify 前保持 timer 禁用。启用前用临时部署验证回传权限。应用更新会等待已有的本地备份及恢复验证单元完成，原子切换 selector，再重启纳管的应用单元。验收失败时恢复上次镜像并重新检查。数据恢复使用节点原有的隔离恢复流程；此 hook 不运行迁移。进程中断、回退失败或运行版本不一致时保留镜像和请求记录，维护者核对各 PID 后继续。

隔离回归可执行 `python -m pytest --noconftest -q tests/test_home_release.py tests/test_home_systemd_release.py`。测试使用临时目录和合成版本。首发还需以真实的只读镜像、挂载命名空间、备份、维护锁和独立工作流回执完成整链演练。

## 先合并，再部署

每次生产变更先在独立分支实现，推送 PR，在候选提交的 CI 通过后合入 main。应用、主机脚本、systemd 单元和站点路由遵循同一顺序。部署设备从指定 GitHub 仓库刷新 main，核对获批 SHA 已在 main 历史内、该 SHA 的主线 CI 成功，再使用该次构建的发布包。检查现网进程、编码服务和入口规则；遇到线上已有、仓库缺失的必要改动，先补 PR 并重新确定发布基线。再次部署时仍需本次目标环境和精确 SHA 的授权。

## 家庭入口画面编码

仓库目录 `infra/home-browser-frames/` 随同一提交的 Linux CI 发布包写入镜像 `home-browser-frames/`。服务复用镜像 `web/node_modules/sharp` 的锁定依赖。以下步骤面向已有 lead-home-net、Web 服务和加密卷的家庭节点。首次安装或变更主机配置走已授权的维护窗口。

1. 从获批 SHA 的只读挂载中取出这四个文件。比较编码器、单元和现网站点规则。当前单元中的 `lead-onboard-87d151a/perf` 是既有主机路径，后续发布仍以本次获批镜像中的文件为来源。
2. 首次安装时使用下列命令（以管理员身份，`SHA` 设为获批完整提交，`current` 已指向该版本）。已有服务在文件一致时可直接核对。替换已有文件前先保存当前版本和站点配置。

   ```sh
   set -eu
   store=/srv/lead-prod-vault/lead-git-releases
   bundle="$store/$SHA/home-browser-frames"
   test "$(cat "$store/$SHA/REVISION")" = "$SHA"
   test "$(readlink "$store/current")" = "$SHA"
   dest=/srv/lead-prod-vault/lead-onboard-87d151a/perf
   install -d -m 0700 "$dest"
   install -m 0644 "$bundle/frame-live.cjs" "$dest/frame-live.cjs"
   install -m 0644 "$bundle/lead-frame-fast-live.service" /etc/systemd/system/
   systemctl daemon-reload
   systemctl add-wants lead-home-live.target lead-frame-fast-live.service
   systemctl restart lead-frame-fast-live.service
   ```

3. 将 `upstream.conf` 的内容放入家庭 Nginx 的 `http` 作用域，将 `location.conf` 的内容放入已有的 `shui.click` 服务块内，保留其他入口和鉴权设置。每段只安装一次。该主机的 Nginx 使用单文件只读绑定；保留原配置文件 inode，将已审阅配置写入后在服务命名空间运行 `nginx -t`。通过才平滑重载；语法失败时还原原配置。常用核验命令如下。

   ```sh
   pid=$(systemctl show lead-home-edge -p MainPID --value)
   nsenter -t "$pid" -m -n -- /run/lead-home/bin/nginx -p /tmp/ -t -c /run/lead-home/nginx.conf
   systemctl kill --kill-whom=main -s HUP lead-home-edge
   systemctl is-active lead-frame-fast-live
   ```

4. 切换或回退应用镜像时，编码器需随 Web 重启以重新绑定 `current/web` 的依赖。用发布包比对实际编码器字节和单元，检查 Nginx 的两段规则。未登录访问取图应返回 401；正常编码路径带有 `Server-Timing`。用隔离工作室和独立浏览器资料测试登录页及点击；同源支持 AVIF 的客户端应收到私有响应、原始坐标尺寸。生产验收留存 SHA、回读和服务状态。回退编码服务时先还原入口规则并平滑重载，确认请求已回到原 Web 服务，再停止编码器；保留浏览器及工作室数据。

## 模型依赖与浏览器验收工具的兼容核验

PR #26 新增 `httpx2` 后，家庭节点共享解释器须已经由节点维护者安装 `httpx2==2.16.0`；应用 SquashFS 不携带 Python 环境，不能用 CI 安装成功代替节点检查。正式入口仅接受已审阅的完整依赖清单 blob，并在 API、Worker、browser 各自运行空间使用低权限、隔离模式核验版本和 Unix socket transport，不读凭据、不联网、不安装依赖。进程在检查期间改变、缺包或清单变化均拒绝。

`verify_account_browser.py` 是开发验收工具，不由生产服务调用。发布入口仅识别 PR #28/#29 对应的精确 blob，不开放其他脚本，也不执行该工具。未来修改须重新审阅并更新对应核验。节点发布器本身的升级仍须单独确认完整 SHA；从同一获批提交取出节点工具，保留原工具备份，核对安装字节后再使用正式发布入口。

预先撤除模型临时覆盖且持有维护状态时，如果正式入口在切换前拒绝，不要让维护状态等待下一轮 CI。先只读确认 selector 未变、独立核验旧映像，再恢复备份的原配置和服务，健康检查通过后通过 `home_live_maintenance.py restore <本次SHA>` 解除维护；记录原覆盖仍存在和本次未上线。禁止把旧请求改写成成功，后续使用新的已确认正式请求。
