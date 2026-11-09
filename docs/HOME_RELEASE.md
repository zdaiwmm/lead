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
