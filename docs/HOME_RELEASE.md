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

**节点专用检查**　维护账户在 `/usr/local/lib/lead-home-release/` 配置两个受信任的可执行入口。它们是接入点，此仓库没有可直接用于该 PC 的现成实现。

- `check-fence OLD NEW` 只读核对业务入口已停止接收新操作、未决任务和浏览器在安全点，并核对持续到恢复结束的维护锁。脚本在备份前后各执行一次。只检查一次活动数无法满足此约定。
- `verify-workflow SHA WORKER_INVOCATION_ID` 只读核对持久 Temporal 的存储和当前 worker 那次启动所完成的隔离测试任务。合成任务应由节点验收进程在专用队列驱动，使用独立身份和测试数据。脚本以 0 表示通过，响应正文留在节点。任何入口缺失时不得以空脚本替代。

**接单与恢复**　先运行 `current` 和 `verify <完整 SHA>`。前者只在系统各应用进程实际看到同一只读镜像且没有未知代码覆盖时输出 SHA。调度端运行前文的 `poll_home_release.py`，只需节点出站访问。GitHub 凭据留在低权限轮询账户，特权 hook 由固定的本机分派入口调用，限制参数与 PATH。启用前用临时部署验证回传权限。应用更新会等待已有的本地备份及恢复验证单元完成，原子切换 selector，再重启纳管的应用单元。验收失败时恢复上次镜像并重新检查。数据恢复使用节点原有的隔离恢复流程；此 hook 不运行迁移。进程中断、回退失败或运行版本不一致时保留镜像和请求记录，维护者核对各 PID 后继续。

隔离回归可执行 `python -m pytest --noconftest -q tests/test_home_release.py tests/test_home_systemd_release.py`。测试使用临时目录和合成版本。首发还需以真实的只读镜像、挂载命名空间、备份、维护锁和独立工作流回执完成整链演练。
