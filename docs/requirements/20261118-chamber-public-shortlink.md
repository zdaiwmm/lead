# Chamber 公网推广短链

## 范围与验收

- 用户授权在 `shui.click` 的生产入口增加 `/chamber/r/…`，并验证公网 HTTPS 跳转和点击回传。本机 Chamber 的短链和点击写入本机数据库。
- 生产 Nginx 只把有效短链格式的 GET 发到 Chamber 采集器。转发头只含来源 IP、协议、User-Agent 和匿名点击 Cookie。请求体、会话 Cookie 和授权头均在入口丢弃。
- Mac 用既有 SSH 身份通过 Tailscale 建立反向 Unix 套接字。生产入口从自己的进程命名空间连接该套接字。Nginx 主机只开原有入口。本机采集器只监听环回，使用现有 PostgreSQL、Worker 和维护进程。短链可用时间取决于 Mac、隧道及本机采集器。
- 验收覆盖公开 302、产品目标、短链命名空间、一次已处理的点击记录及诊断过滤。独立核对现网工作台和原短链入口。

## 实施与交接

开工基线为 `441bb85fb052068370c3142808363b4aa55c5295`。应用支持独立的 `LEAD_SHORTLINK_BASE_URL`，本机也可在状态目录的 `app/config.json` 保存 `shortlink_base_url`。像素的采集地址由既有配置决定。应用改动与入口路由可独立验证，正式切换先验通路，再核对应用生成地址。

1. 按[家庭服务器发布](../HOME_RELEASE.md)验收精确主线提交和同轮镜像。从镜像的 `infra/chamber-shortlink` 取件，核对源码和产物字节。
2. 核对实际生产入口的 UID、GID、命名空间、配置 inode 和 `deploy.lock`。将 `tmpfiles.conf` 的 `@RELAY_USER@` 替换为已有 SSH 账户，以 root 持久安装，然后仅用该文件创建专用 `/run/lead-chamber-shortlink`。目录由 SSH 账户所有，组为入口用户组，权限为 `2750`。
3. 在 Mac 启动环回采集器，沿用本机 Chamber 的环境，显式设定短链基址。运行 `relay.py SSH_CONFIG HOST COLLECTOR_PORT`。SSH 配置使用既有主机公钥校验和登录身份。隧道持锁发布监听套接字，断开或持续 30 秒收不到心跳时释放。入口连接失败时返回上游错误。
4. 持生产维护锁保存旧配置。将 `location.conf` 加入 `shui.click` TLS 服务块。用入口实际身份及命名空间验证配置，然后平滑重载。校验失败时恢复旧配置。在另一个连接读回配置摘要及公网响应。
5. 创建一条专用诊断短链，通过正常采集器接收并处理一次覆盖探针。核对 HTTP 状态、跳转主机、作用于 `/chamber/r/` 的 Secure Cookie、点击入库及覆盖探针排除原因。公共路由验收通过后，再启用 Chamber 正常推广短链配置。

回退时先从生产入口撤除 Chamber 路由并验证重载，再停止专用隧道和采集器。恢复本机短链配置。保留诊断记录及旧入口配置备份。
