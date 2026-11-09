# 家庭服务器发布

在 GitHub Actions 的 **Approve a home release** 选择 main，输入已确认的完整应用 SHA。审批环境 home-release-approval 应限制仅 main，要求仓库所有者审核。主机只需主动向 GitHub 出站访问。公共仓库的任意 CI 作业都在 GitHub 托管节点运行。

工作流检查提交属于 main，同一 SHA 的最新主线 CI 成功且 backend、frontend、namespace 三个任务均通过。在 home-production 创建 task 为 lead-home-release 的 Deployment，记录精确 SHA、CI run/attempt 和审批运行。工作流绿色仅表示请求已登记。线上状态由节点独立上报。

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

**接单与恢复**　先运行 `current` 和 `verify <完整 SHA>`。前者只在系统各应用进程实际看到同一只读镜像且没有未知代码覆盖时输出 SHA。调度端运行前文的 `poll_home_release.py`，只需节点出站访问。GitHub 凭据留在低权限轮询账户，特权 hook 由固定的本机分派入口调用，限制参数与 PATH。启用前用临时部署验证回传权限。应用更新会等待已有的本地备份及恢复验证单元完成，原子切换 selector，再重启纳管的应用单元。验收失败时恢复上次镜像并重新检查。数据恢复使用节点原有的隔离恢复流程；此 hook 不运行迁移。进程中断、回退失败或运行版本不一致时保留镜像和请求记录，维护者核对各 PID 后继续。

隔离回归可执行 `python -m pytest --noconftest -q tests/test_home_release.py tests/test_home_systemd_release.py`。测试使用临时目录和合成版本。首发还需以真实的只读镜像、挂载命名空间、备份、维护锁和独立工作流回执完成整链演练。
