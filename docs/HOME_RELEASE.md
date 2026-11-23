# 家庭服务器发布

新任务按[小修复交付](SMALL_FIX_RELEASE.md)：PR 快速检查，完整验证在 main 按范围一次执行；文档/工具证明不能放行业务应用。用户授权需求和环境，Agent 记录精确 SHA，同范围不因新哈希重复询问。维护锁、备份、平台环境审核与独立核验保留，在途发布沿用原协议。

维护电脑的正式取件入口使用 `prepare_home_image.py --archive-only --cache-dir`，复用同机私有缓存，不再为每轮任务重写取件脚本。它只交付带 GitHub 认证摘要的 ZIP，节点仍核验 REVISION。登记工具源码不进入业务镜像，不再要求手改摘要白名单。维护工具由 tool_release.py 取得同轮独立工具包，核验来源后单文件备份、锁、原子更新与回读；不重启业务。未知脚本、运行依赖及主机配置仍走原兼容核验。

## 隔离服务的公开网页读取

API与worker保留 `PrivateNetwork=yes` 及原 `JoinsNamespaceOf`；隔离空间没有公网路由时，不能通过更换DNS或借用账号/模型代理来读取公开网页。`lead-public-reader.socket/service` 在隔离外提供固定匿名读取通道，仅Unix socket、无TCP监听；只接受实际业务运行UID，操作仅为health及单个公开HTTP(S)网址读取。DNS公网地址检查、固定目的IP、每次重定向、TLS/SNI及原内容上限继续执行；不接收任意请求头、Cookie、代理或执行指令。原项目/任务/账号权限和读取后的复查、资料保存均留在API。

服务使用独立DynamicUser及空RootDirectory，只读绑定同轮公共源码、reader专属受信Python、公共依赖及DNS/CA文件，不映射业务配置、数据库、账号或认证目录。专属解释器由正式入口从固定受信系统Python及匹配公共标准库复制到 `/var/lib/lead-public-reader-runtime`，核验明确的非执行栈、版本与完整来源/副本清单；不修改App共享 `bin/py`，不安装额外包或修补其ELF。当前明确支持CPython3.12/3.14，实际版本单独记证。原 `bin/site` 的锁定纯依赖单独绑定到固定 `/site`，不继承任意PYTHONPATH；固定解释器及子进程均为 `-I -S -B`。正式工具核对运行时和锁定包版本，服务在健康放行前实际检查父进程与同一固定环境子进程的线程、纯依赖/CA及进程启动；不访问网址或模型。`MemoryDenyWriteExecute` 保持，不能为兼容缺失GNU_STACK的旧解释器放宽内存保护。客户端显式配置 `LEAD_PUBLIC_READER_SOCKET` 后失败即报告，不静默直连。该通道不授予账号、发布或target权限。

首次置备采用以下次序，全部使用本需求已批准且完整应用验证通过的精确SHA；同轮工具包与原镜像分别留证，不能用工具/docs检查代替应用放行：

1. 通过 `tool_release.py <工具SHA> --tool home_systemd_release.py --install` 安装正式工具并独立回读。工具修复与冻结应用候选分别记录，工具证明不能替代应用证明。镜像保持永久只读挂载：安装了 `mount-image` 的节点由该入口核同一CI/run/attempt及原包；既有维护电脑取件布局先用 `prepare_home_image.py <应用SHA> <私有暂存目录> --archive-only --cache-dir <私有缓存目录>` 取得原ZIP，再按原节点导入流程独立重核精确应用CI、GitHub归档摘要和REVISION后挂载。通道置备与原deploy一样复用该正式挂载，仍核root属主、squashfs/只读、REVISION、main祖先、运行时与逐文件Git blob；缺少挂载及下载入口时明确拒绝，不直接信任目录或跳过证明。
2. 首次准备专属解释器：在节点执行 `home_systemd_release.py public-reader-runtime-plan <SHA>`，只读冻结固定系统解释器/标准库、原App与目标路径；以root私有0600且不可覆盖方式保存到 `/etc/lead-home-release/public-reader-runtime-provision.json`。执行 `provision-public-reader-runtime <SHA> /etc/lead-home-release/public-reader-runtime-provision.json`，持原deploy.lock，冻结源字节，在 `/var/lib` 同盘暂存并核完整清单后原子安装。另起连接执行 `verify-public-reader-runtime <SHA>`。不在只读plan/verify中隐藏复制或写入；未知安装只按原journal与实际同inode产物收口，不重复复制。
3. 执行 `home_systemd_release.py public-reader-plan <SHA>`，只读冻结实际API/worker身份、原单元/挂载/进程及当前App。将该JSON以root私有、0600、不可覆盖方式写入固定 `/etc/lead-home-release/public-reader-provision.json`；本机业务标识与配置不进入公开仓库。
4. 执行 `home_systemd_release.py provision-public-reader <SHA> /etc/lead-home-release/public-reader-provision.json`。工具持原deploy.lock，核对私有计划与全部原字节，只向固定目标安装socket/service和末尾consumer drop-in，不清空原Bind列表；仅启动新增服务，原App及API/worker进程不重启。另起连接执行 `verify-public-reader-prepared <SHA>`，核同版本服务、真实沙箱/依赖/健康以及原App/consumer未变。已有其他候选、路径或身份不匹配即拒绝。
5. 经原受保护应用入口切换同一SHA及原渠道。正式部署在维护前和切换前复核consumer预像；正式verify除原应用、渠道、持久工作流和维护条件外，强制核API/worker实际进程已获得固定只读socket目录及路由，读取服务仍用独立身份、固定沙箱和对应源码。另起连接执行 `verify-public-reader <SHA>`，不能把待生效drop-in或服务active当成应用已接入。
6. 各阶段保留同一持久journal。结果未知只读回原状态，实际已启动且证据完整时只登记原结果，不重发start或重建计划。已激活consumer的失败回退先恢复旧App及consumer原配置/运行绑定，再停止新通道；正式 `rollback-public-reader <SHA>` 要求旧App已恢复并取得原维护安全点。回退后不能重放已完成的置备intent。

无关后续App版本仅在读取服务、fetch、模板、依赖清单等相关输入逐字节不变且实际服务/依赖/挂载仍可独立核实时复用原读取服务；证据分别报告reader SHA与App SHA。输入变化或多候选不明确时拒绝，本入口不自动升级或覆盖已有服务。真实Linux演练覆盖隔离内直连拒绝、Unix读取合成公开源、其他UID拒绝、私有文件不可见、socket重建、回退和未知结果回读。最终经营验收仍须跟踪一次真实新事实的消费决策、实际原文、引用与后续结果；服务健康不等于来源可读或业务成功。

尚未激活的首次reader可用 `rollback-prepared-public-reader <旧reader SHA>` 窄回退：必须证明旧App尚无reader客户端、原API/worker PID与Invocation全部保持、实际无新增socket绑定或有效路由，且待生效文件精确匹配原记录。旧进程可能看见宿主新socket路径，不能把可见路径误作已激活绑定；回退先固定同实例Root.parent/root、socket目录及私有计划的dev/inode/hash，再仅还原待生效文件和停止新reader，不重启API/worker。任何已激活、身份漂移或未知停止结果均拒绝盲续；停止后核实际PID/cgroup和socket消失，再恢复单元预像。

仅在原journal明确rolled-back、原App和consumer绑定恢复、旧reader已停止且无存活成员时，才执行 `retire-public-reader <旧reader SHA>`。它只处理上述同实例身份：旧root父目录同盘完整归档到 `/var/lib/lead-public-reader-retired/<旧SHA>/root-parent`，socket目录仅在原inode且为空时移除；固定私有计划复制到root私有归档、核摘要并fsync后，只移除同原inode/原摘要的文件。原journal、目录及计划证据均保留。完成重入只回查原归档，不能处理新实例复用的固定路径；不递归清空未知目录，不跨文件系统直接rename。此后新SHA从新的固定私有计划正常置备，不能重放旧候选的首次安装intent。

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

在 GitHub Actions 的 **Approve a home release** 选择 main，由 Agent 输入本需求授权范围内已固定的完整应用 SHA。审批环境 home-release-approval 限制 main，并沿用仓库所有者审核；Agent 可在本次授权和自身平台权限内办理，不再增加重复聊天确认。主机只需主动向 GitHub 出站访问，公共仓库 CI 在 GitHub 托管节点运行。

工作流检查提交是合入本仓库 main 的 PR 合并结果，同一 SHA 的最新主线应用 CI 成功且 backend、frontend、namespace 三个任务均通过，新流水线另要求 Application verification；工具/docs 结果不放行应用。在 home-production 创建 task 为 lead-home-release 的 Deployment，记录精确 SHA、CI run/attempt 和审批运行。工作流绿色仅表示请求已登记。线上状态由节点独立上报。

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

每次生产变更先在独立分支实现，PR 快速检查成功后合入 main，再等待该 SHA 适用的完整验证。应用、主机脚本、systemd 单元和站点路由遵循同一顺序。部署设备从指定 GitHub 仓库刷新 main，核对获批 SHA 已在 main 历史内、该 SHA 的主线 CI 成功，再使用该次构建的发布包。检查现网进程、编码服务和入口规则；遇到线上已有、仓库缺失的必要改动，先补 PR 并重新确定发布基线。授权按本需求和环境继承，Agent 固定记录精确 SHA；超范围变化重新确认。维护工具使用独立工具证明和同轮工具包，不创建应用 Deployment。

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

3. 将 `upstream.conf` 的内容放入家庭 Nginx 的 `http` 作用域，将 `location.conf` 的内容放入已有的 `shui.click` 服务块内，保留其他入口和鉴权设置。每段只安装一次。该主机的 Nginx 使用单文件只读绑定；保留原配置文件 inode，将已审阅配置写入后，在服务挂载及网络命名空间中以实际 Nginx 进程的 UID/GID 运行 `nginx -p /tmp/ -t -c /run/lead-home/nginx.conf`。进入命名空间不等于切换运行身份；须先清空附加组并降到核实的普通用户，不能以 root 执行 `-t` 或 `-T`。校验也可能创建、调整临时目录，不是只读操作。运行前后核对临时目录实际 inode、属主及模式未漂移，通过才对原 `lead-home-edge` 主进程发送 HUP；语法失败时还原原配置。本次目录恢复不改配置，不需要校验或重载 Nginx。

4. 切换或回退应用镜像时，编码器需随 Web 重启以重新绑定 `current/web` 的依赖。用发布包比对实际编码器字节和单元，检查 Nginx 的两段规则。未登录访问取图应返回 401；正常编码路径带有 `Server-Timing`。用隔离工作室和独立浏览器资料测试登录页及点击；同源支持 AVIF 的客户端应收到私有响应、原始坐标尺寸。生产验收留存 SHA、回读和服务状态。回退编码服务时先还原入口规则并平滑重载，确认请求已回到原 Web 服务，再停止编码器；保留浏览器及工作室数据。

### 2026-11-15 原回执临时目录恢复

生产应用及渠道保持 `8276c7cd4c300e1e7fc2a7bfc5cb7be7edc65d88`。原 managed Node 请求已在本地完成，399 条原事件尚未回传。实际 RPC 持续拒绝、HTTPS 返回 500；仅观察既有重试的文件打开调用，30 秒内记录到 `/run/lead-home/scratch/body/` 下 8 次 EACCES、0 次成功。目录为 0700、属主与三个实际 Nginx 进程的普通运行用户不符，父 scratch 目录归属正常。超过内存缓冲的请求体需要使用该临时目录；未超过入口 256 KiB 上限不能排除此故障。

本次只恢复现有目录归属，先完成本记录的 PR、精确 head 检查、合入 main 及文档范围验证，再在原节点操作；该文档证明不替代应用或工具发布证明。操作须持有原 deploy.lock，核对机器、既有正式工具字节、实际应用 SHA 和无在途切换；从实际 edge 的 BindPaths 找到 live vault 内持久 scratch 源，并确认宿主 body 与运行空间为同一设备/inode、无链接。核对主进程及现有 worker 的 UID/GID/FSUID/FSGID 一致且非 root，父目录归属和 0700 正确，body 仍为预期的旧属主/0700且为空；条件变化则停止。

先在服务器私有 journal 保存目录原 UID/GID、模式、设备/inode、进程和操作意图，再通过 O_NOFOLLOW 打开的目录文件描述符，仅将该 body 目录改为已核实的 Nginx UID/GID。保留 0700，不递归、不改父目录、不删内容、不调整入口上限、用户授权或配置，不安装工具或重启任何服务。结果未知先回读同一 journal 与 inode，不盲目重复。回退只在目录身份和本次新属主仍匹配时恢复 journal 中的原属主，不删除已产生的文件。

另起 SSH 独立核对同一持久 inode 的正确归属、0700、原进程和应用版本；等待原请求自然重试，回读原 outbox、服务端请求及 growth Task。不得清空 outbox、改成功状态、重启 Node、重投模型或另发能力变化。只有原事件自然回传、服务端原请求完成，才能解除本次回传阻断；判断是否形成有效来源、可执行后续或具体停止条件另按真实结果验收。历史目录改属的发起者尚无证据；root 配置校验是已识别的复发入口，不追认为本次历史原因。

**执行结果：**配方 PR86 精确 head `cc94ebdbd8b6890c2c1dd1d1edca4e76693d5fdf` 的全部必需检查通过，合入 `a9c1f9c1100f4db1a3bf5bd494c87ddb41483424`，精确 main 文档 CI `43694471734/1` 成功；应用/工具任务按范围跳过。随后一次执行上述定点恢复，服务器私有 intent/restored journal 与原元数据保留；新 SSH 独立确认同一持久 inode、0700、正确运行属主和原 edge 主进程。原请求于北京时间 16:28:10 自动回传完成：原 outbox 从399归零，服务端同一请求保存399条唯一事件，命令未确认数归零，原 thread/turn 与本地一致。原 growth.decision 于16:28:12成功处理，给出完整 waiting_for_change 条件（evidence_added、owner），没有接受新工作、保存新原文或触发社媒动作。因此本次回传故障已解除，真实需求或业务进展仍无新证明。没有重启、清状态、重投模型或读取认证文件，实际应用继续为 M8276。

## 模型依赖与浏览器验收工具的兼容核验

PR #26 新增 `httpx2` 后，家庭节点共享解释器须已经由节点维护者安装 `httpx2==2.16.0`；应用 SquashFS 不携带 Python 环境，不能用 CI 安装成功代替节点检查。正式入口仅接受已审阅的完整依赖清单 blob，并在 API、Worker、browser 各自运行空间使用低权限、隔离模式核验版本和 Unix socket transport，不读凭据、不联网、不安装依赖。进程在检查期间改变、缺包或清单变化均拒绝。

`verify_account_browser.py` 是开发验收工具，不由生产服务调用。发布入口仅识别 PR #28/#29 对应的精确 blob，不开放其他脚本，也不执行该工具。未来修改须重新审阅并更新对应核验。登记节点工具按本需求/环境授权，使用 tool_release.py 的已验证工具包、备份和独立回读，不因合并 SHA 变化重复询问。

预先撤除模型临时覆盖且持有维护状态时，如果正式入口在切换前拒绝，不要让维护状态等待下一轮 CI。先只读确认 selector 未变、独立核验旧映像，再恢复备份的原配置和服务，健康检查通过后通过 `home_live_maintenance.py restore <本次SHA>` 解除维护；记录原覆盖仍存在和本次未上线。禁止把旧请求改写成成功，后续使用新的已确认正式请求。

## 本机 Codex 执行层

本轮应用携带 0034 迁移和同 SHA 的本机连接器；从线上 0031 升级时仍使用 `unified_browser_upgrade.py` 的备份克隆演练、维护、安全点、原表对账和最终恢复流程。连接器包由同轮 CI 构建，镜像内 `services/local-executor/lead-local-executor.tgz` 的 `package/REVISION` 必须等于应用 SHA。不能用本地重建包代替该产物。应用和两个独立渠道执行器的切换、回读完成后，分别记录本机设备安装与项目启用结果。

登录 Lead 后通过 `/api/executor/download` 下载该版本安装包，在已有 Node24 和官方 Codex CLI 的本人电脑安装 `npm install -g ~/Downloads/lead-local-executor.tgz`。在项目「本机 Codex」生成并执行连接指令；指令包含项目 UUID、短期配对码和 `--install`，配对后启动独立用户后台服务。原 Codex 登录保持在本机，连接器不读取、复制或上传认证文件。状态报告真实 CLI、ChatGPT 权益、Sol/medium 和连接器 SHA；安装成功与真实模型/业务验证分别记录。

### Linux 专用执行服务器

服务器使用 `infra/local-executor/lead-codex@.service`，由管理员管理系统服务，实际以无 sudo、无业务组权限的固定普通用户 `lead-codex` 执行。私有 home 为 `/var/lib/lead-codex`（0700），Codex 凭据只由官方 CLI 保存在该用户的 `.codex`；项目状态仍按原 UUID 分目录。Node、官方 Codex 与已验证连接器分别置于 root 持有、执行用户不可改写的 `/opt/lead-codex/{node,codex,connector}`。不可复用业务服务用户、Mac auth/state 或浏览器资料。

首次置备先核服务器身份、服务和线上版本，固定官方 Node/Codex 版本与下载摘要，核已放行连接器原包的 SHA/REVISION。本次复用未改源码的 S9 原封连接器；新增 unit 则在独立 PR、精确主线 CI 和产物通过后，从本次同轮镜像 `infra/local-executor/` 安装，两个产物分别留证；原应用不因安装执行端重新切换。配对前以合成 HTTP/RPC 夹具核验普通用户、相同环境、unit 解析、脱离 SSH、进程崩溃恢复、outbox 断网保留与恢复，以及业务路径不可读；夹具不构成真实登录或模型验证。

官方登录在同一用户和环境运行 `codex login --device-auth`，由本人在 CLI 给出的官方页面确认。若账户要求，先在 ChatGPT Security 启用 device-code login。不要索取密码、二次验证码，不复制认证缓存，不静默改 API key。实际 `login status` 及连接器 `status` 确认 ChatGPT、Sol/medium 后，再由真实 Lead owner 生成配对码。服务器配对必须**去掉网页命令中的 `--install`**：该参数当前只支持 macOS，且后端配对会立即启用设备。运行 `/opt/lead-codex/node/bin/node /opt/lead-codex/connector/dist/cli.js pair --profile PROJECT_UUID --url https://shui.click --code OWNER_CODE`，然后启动事先核验的 `lead-codex@PROJECT_UUID.service`；登录、能力检查和配对均使用该用户的相同 HOME/CODEX_HOME/PATH。配对码不进仓库或公开记录。配对前核原设备和原在途请求；新服务器只接新的未固定请求，旧请求沿原设备，不能改写 device/thread 或重放。

服务通过 `multi-user.target` 自启，`KillMode=control-group` 处理子进程，`UMask=0077`；普通断网由连接器重试，信号异常退出由 systemd 恢复。显式错误退出（特别 401/409）保持停止，不能自动循环抢代次。新实例 poll 已提交 epoch 而响应丢失时，现有协议可能安全停止并需要人工核原设备/线程；不能宣称所有切点自动恢复。Lead 配对 bearer 现存有效期为30天，和 ChatGPT 登录自动刷新分开核验，到期须本人重新配对，原请求不得迁移。执行用户没有业务库、部署凭据、浏览器/CDP 权限，工具仍走原请求的 Lead 身份和项目校验，不新增原生模型 token、步数或运行时长限制。

`binding.url` 决定 pair/poll/events/tools 全部请求目的地；同机执行不等于本地连接。部署时记录实际 DNS、TLS、目标 IP 与请求耗时；保留 HTTPS、身份和项目权限，不为减少延迟改成未审阅的 loopback 通道。模型仍需访问 OpenAI 网络。

需要获准代理时，管理员使用 root 持有、0600 的 `/etc/lead-codex/network.env`，只配置标准 HTTP/HTTPS/ALL_PROXY 与 NO_PROXY 变量；不在该文件放模型 API key、替换官方 issuer/base URL 或关闭 TLS。官方登录、能力检查和常驻进程须用相同网络环境。Node24 的 Lead `fetch` 不启用环境代理，仍验证原 binding HTTPS 和设备权限；此行为已用实际24.14.1与不可达假代理访问公开首页验证。代理凭据和原出站通道资料仅留受控节点，不进入仓库或公共证据。

需要复用已有受控 Xray 出站时，单独使用 `infra/local-executor/egress.py` 与 `lead-codex-egress.service`，不更改原 API 通道。管理员先核既有通道归属和使用权限，固定官方26.10.31发行包及二进制摘要，建立与Codex不同的无登录网络用户；新脚本、二进制、operations与认证文件均由root持有。仅已核实的legacy源允许固定维护身份1000持有，路径及祖先权限仍逐级拒绝链接和不可信写入；不搬认证文件或私钥。包装进程读取单个窄出站后，清空附加组、实际/有效/保存UID与GID和进程能力，再以该网络用户运行Xray。源卷通过部署侧 `RequiresMountsFor=` 依赖现有挂载，不解锁卷、不重启原服务。

该适配只监听有随机Basic认证的loopback HTTP代理，TLS仅允许精确官方主机 `auth.openai.com`、`chatgpt.com`、`api.openai.com` 的443端口。嗅探将实际目标重写为允许的SNI；CONNECT名称和SNI不同时连接到官方SNI，不保留原任意目标。IP目标、无SNI、非TLS、其他域名/端口和UDP默认拒绝，保留官方证书校验。项目明确选择后才安装 `lead-codex-egress.conf` 为该实例的drop-in；必需环境缺失或代理格式不符即停止，不静默直连。官方CLI自身须实际支持并开启 `features.respect_system_proxy`，原生命令的代理继承单独配置和实测，不能限制为只可访问模型域名。真实二进制的认证/路由拒绝矩阵、降权、故障恢复、权限隔离和精确main CI通过后才安装，再用官方只读请求核出口；配置解析通过不表示可登录或账号有权限。

实际systemd259.5的seccomp启动路径会移除未列Ambient的SETUID，即使BoundingSet保留它；本模板只在root读取阶段显式保留该能力。降到网络用户后，固定prctl/capset调用清空Ambient及I/P/E，再验证四类能力全零才执行Xray；调用失败即停止，不能忽略继承能力或删除隔离设置来启动。验收读取实际Xray进程的UID/GID、Groups与四类cap，不能只看unit的root启动包装身份。临时unit必须核同一隔离配置的SIGKILL自动恢复、明确stop不自动重启及再次start，并独立清理。

每个项目使用独立 `~/.lead/executors/<UUID>`；macOS 使用 LaunchAgent，已置备的 Linux 服务器使用上述 systemd 项目实例。网页关闭不终止执行；执行设备关机或离线时，业务保留原请求等待恢复。设备更换不会自动迁移旧会话，未知发送必须在原线程和原 Action 对账。解除连接立即撤销设备权限；成员角色、项目范围和能力在每次设备请求重新核验。配对启用该项目的新任务设备执行，已固定的旧任务继续按原归属完成；未配对的已有项目保留原配置，不静默代用 API 模型。

长模型等待通过短 Temporal Activity 轮询，原请求与线程持久化；业务取消阻止新工具提交并中断原 turn，进程关闭仅等待原请求恢复。原生压缩由 Codex 执行。原生长期记忆可通过本机 `LEAD_CODEX_MEMORIES=1` 启用并由后台安装保留；它沿用本人的原 Codex home，不修改全局设置，启用前核对跨项目记忆范围。经营事实、可撤销经验、账号归属和外部回执仍由 Lead 保存。

## 独立渠道执行器

配置及启动入口为 `lead_app.channel_executor`。公共代码来自同轮只读应用镜像，私有平台代码放在独立只读适配器目录，禁止用旧 `browser.py`、`backups.py`、`budget.py` 等覆盖公共包。账号由当前 Profile 绑定解析，配置只选择项目和资料槽，不固化外部账号。现有家庭节点的依赖继续从已核验、只读的 `/run/lead-home/bin/site` 加载；不继承任意 `PYTHONPATH`。

每个进程使用独立绑定文件、适配器、回执目录与服务生命周期。绑定文件字段由 `channel_executor.Binding` 定义；操作员保存在 `/etc/lead-home-release/`，root 持有且组/其他用户不可写。`release` 为进程内稳定路径（例如 `/run/lead-executor/release`），必须完整只读绑定到指定主线镜像。`adapter_root` 为另一完整只读绑定，清单逐个覆盖所有 Python 文件，不允许未登记二进制、字节码或链接。`receipt` 放在该执行器独享、权限 0700 的可写目录。启动命令为 Python 的 `-I -B`、镜像中的 `services/api/lead_app/channel_executor.py` 及绑定文件绝对路径。

Reddit 现有 Worker 使用同一镜像内 `services/channel_adapters/reddit/entry.py`，保持原有持久队列。`LEAD_RESIDENT_CYCLE_AGENT_IDS` 是已有专用执行器接管项目主循环的显式 UUID 清单；迁移原 Worker 包装前先配置同一归属，不能让两个进程同时接管主循环。此配置不拦截 Reddit 的独立渠道时钟。Telegram 保留独立服务、资料与出口，先对账私有平台行为，再迁移到显式适配器；不能拿通用测试替代私有适配器的授权、回执及本地模型限制验收。

对账必须包含原进程实际加载的全部公共模块及配置入口。既有 `LEAD_IPV6_TUNNEL_<agent>` 仅允许用于绑定 IPv6 身份的受控 `socks5+unix` 路由，DNS 与连接必须经过同一 Profile 代理，失败不得改用直连。既有 `LEAD_BACKUP_POLICY=verified_local` 继续调用严格的 `lead.local_restore_ready()`，不由迁移自动启用；未配置时仍要求原异地备份证明。本地策略下数据库备份须保存同一导出快照的四表计数，供独立恢复核验。发送失败后的成功读取只有满足原等待期限和权限证据变化才能解除屏障。

发布前逐个保存旧服务/绑定/适配器与恢复清单；在各执行器安全点按渠道分别切换和回读，不把两个渠道加入同一 restart 命令。公共应用仍通过原正式取件、备份和审批入口交付。root 持有的 `executors.json` 登记所有渠道，例如 `{"version":1,"executors":[{"service":"lead-telegram.service","channel":"telegram","binding":"/etc/lead-home-release/telegram.json"},{"service":"lead-home-worker.service","channel":"reddit","binding":"/etc/lead-home-release/reddit.json"}]}`。这里是格式示例，服务名及配置需据真实节点盘点生成。

已登记执行器的节点在解除应用维护前，按实际应用版本核验整组执行器。独立执行器可在同一候选的维护 hold 下先升级，再由原入口切换应用；全部同版才恢复调度。应用回滚后若仍有执行器为候选版本，保留维护及关闭的入口，先按原 journal 核对并完成逐渠道恢复，不在混版状态恢复经营。既有首次迁移的 pending journal 继续沿原 finalize 流程完成，不把普通升级伪装为迁移。

独立执行器的项目原本已暂停时，先安装通过完整应用验证的 `home_live_maintenance.py`。从现有执行器绑定核实项目 UUID，在同一候选下运行 `hold-paused <应用 SHA> <项目 UUID>`，记录维护前的暂停状态及授权指纹。完成同版核验后使用原 `restore <应用 SHA>`，项目保持暂停。

正式节点工具 `home_systemd_release.py executors <SHA>` 独立核对两个渠道的 PID、InvocationID、命令、配置、内核挂载、公共代码、适配器及启动证明。注册后，发布轮询调用的最终 `verify <SHA>` 同样必须通过这项核验；缺少任一渠道、旧进程证明、旧模块覆盖或摘要变化均拒绝成功回传。`current` 仍是主应用版本，不单独证明渠道已交付。未完成首次注册的节点不能据旧 `verify` 宣称渠道统一完成。

启动证明只说明该进程的代码和绑定；真实业务验收还须分别记录任务、账号/Profile/出口、执行归属、平台结果/回执以及恢复情况。平台许可不足保持等待；未知发送先核对原结果，不发送额外测试内容。

### 已有渠道的首次纳管

正式工具同轮新增 `provision-executor <SHA> /etc/lead-home-release/<channel>-provision.json` 与 `register-executors <SHA>`。先通过 `tool_release.py` 安装该 SHA 的工具包，再使用同轮已验证只读镜像中的 `services/channel_adapters/systemd/executor.conf.in`。不把本地临时脚本装入生产。

操作员先只读盘点现有单元、全部 drop-in、真实进程、挂载、凭据来源和私有策略摘要，生成 root 私有计划。计划明确一个服务、完整 Binding、`main-current` 或 `independent` 来源模式、保留的只读挂载，以及旧主单元/drop-in/绑定/selector 的预期摘要。固定输出在 `/etc/lead-home-release/`、该服务的 `zzzz-lead-executor.conf` 和发布仓库 `executors/<channel>/`；不能指定任意写入目的地或原始 systemd 文本。私有适配器暂存到清单摘要命名的目录，文件集必须与 Binding 完全一致；不把源码或业务资料提交到公开仓库。

systemd 的空 `BindReadOnlyPaths=` 会同时清空两类绑定。工具从原服务实际 `BindPaths` 读取既有可写映射，保存到受保护快照，并在重置后显式重发；私有计划不能新增可写路径，也不能保留覆盖公共代码或解释器的可写绑定。稳定的 `/etc/lead-home-release/<channel>-layout.json` 记录这组来源和回执目录，随配置一起备份、回退。启动验收与后续独立核验均对照该记录检查 systemd 属性、实际进程内的可写挂载及来源 inode；记录不依赖当前应用 SHA，支持主线跟随执行器更新。CI 以真实 systemd 基础单元和 drop-in 验证原目录可写、公共代码只读及恢复行为。

凭据保持原 EnvironmentFile 或现有只读数据绑定；公共代码新位置需要读取外置 dotenv 时，用进程环境 `LEAD_ENV_FILE` 指定绝对普通文件，显式无效路径拒绝、不回落。该设置保留逐次重读与进程环境优先级。新镜像包含浏览器隔离、研究、备份和迁移运行依赖，不依赖旧私有公共代码。

经原维护入口取得本次 SHA 的安全点后，逐次调用一个渠道的纳管命令。工具持共享发布锁，只读核对维护指纹和空闲状态，持久保存旧字节，停止单个服务、安装模板、启动并核对实际进程证明。可知失败恢复原文件和服务；中断留下 journal，下次仅回读，不重放切换。工具不改变业务授权、不自动解除维护、不替换主应用 `current`。`main-current` 须先由正式应用发布切到候选；`independent` 可在主应用切换前单独准备获批版本。

两个渠道各自验证成功后，调用 `register-executors <SHA>` 进行最后登记；它再次检查两个实际进程且不重启服务。随后从独立连接执行 `executors <SHA>` 和正式 `verify <SHA>`，核对账号/Profile/出口及原任务后再经原入口恢复运营。首次纳管、后续升级与回退均保存同一套逐渠道证据；未知发送不因迁移而重新提交。
