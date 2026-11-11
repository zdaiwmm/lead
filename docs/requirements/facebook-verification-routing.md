# Facebook 登录后无法完成绑定

## 范围与验收

2026-11-11 用户反馈 Facebook 已显示登录后的中文首页，但「验证并完成」提示「浏览器操作未完成」。本任务授权排查和修复该故障；生产发布仍需另行确认精确主线提交和目标环境，不继承其他聊天的授权。

旧共享目录 `/Users/zhouding/work/lead agent` 位于 `main@d887831` 且有大量其他任务改动，仅作线索来源。核实远端为 `zdaiwmm/lead`，同步 GitHub main 后从 `4ba6903aff8ea09fe7b2775b72f7a90fcc02c88f` 创建独立分支 `codex/facebook-binding-inspect`，规则取自同一 main 的 `AGENTS.md`、`SMALL_FIX_RELEASE.md` 与相关验收、验证和运维文档。

家庭生产入口只读 `current` 返回 `7257bfaeb0b5d4417d501223e07d341e3c0ee00d`。数据库只读核验显示 Facebook 仍待绑定，未处于发布维护；真实账号、浏览器资料和业务状态未由本任务修改。账号检查按已有路由选择独立执行器，但新增的 `challenge/status`、`challenge/verify` 未被路由分支覆盖，落入默认 Telegram 执行器。前端点击验证时会先请求 challenge status，因此尚未到账号检查就可能失败。没有取得本次失败请求的完整服务日志，不把截图提示当作某个具体底层异常的证明。

本批仅修复验证请求的浏览器选择：按有效 takeover 找到原资料槽，与截图、输入保持一致；拒绝过期、结束、跨项目、平台不匹配及无对应账号的请求。默认 Telegram 继续走原执行器。无需导航到个人主页，不改变真人验证、绑定、接管、账号身份、运营授权和外部发布语义。

验收条件：普通登录与处理真人验证后均能继续绑定同一账号；错误不能结束有效接管或冒充绑定成功；Facebook、X、Reddit 各自的独立浏览器与 Telegram 会话不串用；所有测试禁止真实平台写入。

## 实施与交接

- 实现：`services/api/lead_app/api.py::login_browser_port` 补齐两个 challenge 路由并校验窗口与平台一致。
- 回归：扩展 `tests/test_split_reddit_login.py`，通过真实 Camoufox、隔离数据库和受控页面执行与前端相同的 status → verify-login 或 status → challenge verify → verify-login 顺序；覆盖三个独立平台、重试、失效、跨项目、绑定持久化和 Telegram 保留。
- 保护：`tests/test_login_challenge_routing.py` 覆盖两个入口的失效与不匹配请求，以及默认 Telegram 选择。
- 修复前，同一隔离测试真实重现 Facebook challenge status 被发往 8089；修复后结果见本次提交与 PR。
- 本地测试使用本任务新建 PostgreSQL 18、独立配置和临时浏览器资料；未连接生产数据库进行写入，未请求真实社媒页面。初次环境因 Unix socket 路径过长失败，改用本任务专属短临时目录后重现故障。
- 本地验证：`LEAD_BROWSER_TESTS=1 python -m pytest -q tests/test_login_challenge_routing.py tests/test_split_reddit_login.py tests/test_facebook_account.py tests/test_browser_challenges.py`，59 项通过，219.85 秒；19 条为既有 Camoufox 代理 geoip 建议。使用 Python 3.12、PostgreSQL 18 与固定 Camoufox，配置和数据库地址显式指向本任务隔离目录。受影响文件 Ruff、`git diff --check` 与仓库凭据扫描通过。
- 用户界面的流程与授权未改变，因此无需修改运营流程图内容源。前端文件未修改。

## 发布记录

当前线上到任务基线还包含 PR #30/#34 的交付流程和下载缓存工具改动，不能把未来整个主线发布称作仅有本次修复。生产仍按既有正式入口、精确 main CI、单写维护和独立回读执行；本任务没有申请部署、切换版本、重启服务或改写账号状态。线上实号绑定结果待获批上线后验收。
