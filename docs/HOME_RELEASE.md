# 家庭服务器发布

在 GitHub Actions 的 **Approve a home release** 选择 main，输入已确认的完整应用 SHA。审批环境 home-release-approval 应限制仅 main，要求仓库所有者审核。主机只需主动向 GitHub 出站访问。公共仓库的任意 CI 作业都在 GitHub 托管节点运行。

工作流检查提交属于 main，同一 SHA 的最新主线 CI 成功且 backend、frontend、namespace 三个任务均通过。在 home-production 创建 task 为 lead-home-release 的 Deployment，记录精确 SHA、CI run/attempt 和审批运行。工作流绿色仅表示请求已登记。线上状态由节点独立上报。

## 已运行节点的接入约定

1. 在家中先核对线上仓库、真实路径、版本、启动方式、持久数据与现有更新机制。记录上个可恢复版本，以及数据库迁移、正在执行的任务和备份对回退的限制。
2. 由已有更新程序轮询本仓库 home-production 的 lead-home-release 请求。只接受上述工作流完成成功、工作流 main/手动/首次运行、运行标题及 SHA 与请求吻合且精确 CI 仍成功的请求。节点凭据限本仓库读取和 deployments 状态回传。
3. 绑定目标版本，核对当前线上 SHA 是候选的祖先。以持久请求 ID 和单写锁防重入；离线积压、已被更新版本覆盖或先前结果未知时停下来核对。切换前先保存开始状态并向 GitHub 登记 in_progress。中断时查同一请求，不重复执行迁移或切换。
4. 调用节点现有备份、预检和发布入口。保留配置、数据库、任务记录和浏览器资料。另起只读验收，检查真实运行 SHA、Web/API、持久 Temporal 和工作进程、隔离测试任务。验收不向真实社媒账号发内容。
5. 仅在独立核验通过后向同一个 deployment 的 statuses 回传 success；失败标记 failure/error 并保留脱敏诊断。临时网络中断只补传已有结果。首次接入应由节点维护者先演练只读核验和回报，再确认精确版本切换。

当前仓库只提供审批和请求端。家中节点的实际安装目录、执行入口及线上版本由节点交接盘点补齐。
