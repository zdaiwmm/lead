# 网站归因与服务端转化接入

在 Agent 的获客效果中接入网站统计，部署站点代码。公开使用需先设置 `LEAD_PUBLIC_URL` 为实际 HTTPS 服务地址。本机地址只适合本地测试。

浏览器回传只产生 `client_observed` 访问。有效访问规则 `engaged-v1` 要求前台活跃至少 10 秒，并检查服务器收到事件的时间差。链接请求仅算点击，不能当作网站访问。

## 服务端接入

在网站统计的深层设置中生成回传密钥，放在客户网站后端环境变量中。密钥只显示一次，lead 数据库只存哈希。重新生成会撤销旧密钥。网页代码的公开写入凭据不能调用转化接口。

网站提交表单或注册时，将 `window.leadAttribution()` 的返回值一起传给自己的后端；这个值仅包含 connection_id、session_id、ref，不包含服务端密钥。后端确认业务事件真实发生后再回传：

```http
POST /collect/server/conversion
Authorization: Bearer <网站后端保存的回传密钥>
Content-Type: application/json

{
  "connection_id": "<网站连接 UUID>",
  "event_id": "signup:业务事件唯一编号",
  "type": "signup",
  "session_id": "<浏览器归因会话 UUID>",
  "ref": "<16 字符 lead_ref>",
  "occurred_at": "2026-10-29T08:00:00Z"
}
```

类型支持 lead、signup、trial、purchase。相同 event_id 和相同内容重复回传只计一次；相同 event_id 改成不同内容返回 409。事件必须对应本网站连接下已有的归因访问，不能跨 Agent、跨网站引用。时间窗口为过去 30 天至未来 5 分钟；提供时间时必须带时区。

后端可以向 `/collect/server/ping` 发送相同鉴权、JSON `{"connection_id":"<UUID>"}` 建立健康状态。回传接口不接受 Origin，避免误将服务端密钥嵌入网页。该检查不代替凭据保密。

数据库的 `server_verified` 表示“由持有独立凭据的网站后端回传且与已知访问关联”，不代表 lead 独立核验了收入、支付结算或人工访客。当前总览统计的是归因事件数量，不是去重客户人数或收入。

未接入时显示未知；最近 24 小时没有有效回传或健康检查时标记过期，避免把断开数据源显示成零转化。过期不会删除历史数据。当前尚未在用户真实网站部署此回传，不能据此声称已产生获客成果。
