"use client";
import { useEffect, useId, useState } from "react";
import { Check, ChevronDown, Copy, Info, RefreshCw } from "lucide-react";
type Row = Record<string, any>;
export default function AnalyticsPanel({
  aid,
  readOnly = false,
  url,
  request,
}: {
  aid: string;
  readOnly?: boolean;
  url?: string;
  request: (path: string, body?: Row) => Promise<any>;
}) {
  const [data, setData] = useState<Row | null>(null),
    [site, setSite] = useState(url || ""),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState(""),
    [error, setError] = useState(""),
    [serverKey, setServerKey] = useState(""),
    [feedback, setFeedback] = useState("");
  const fieldId = useId();
  async function load() {
    const value = await request(`/agents/${aid}/analytics`);
    setData(value);
    return value;
  }
  useEffect(() => {
    let active = true;
    request(`/agents/${aid}/analytics`)
      .then((d) => {
        if (active) {
          setData(d);
          if (!url && d.connection?.site_host) setSite(d.connection.site_host);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [aid, request, url]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(""), 2500);
    return () => clearTimeout(timer);
  }, [copied]);
  async function perform(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy(value: string, key: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(key);
    } catch {
      setError("复制未完成，请选中代码手动复制。");
    }
  }
  const connection = data?.connection;
  const fresh =
    connection?.state === "verified" &&
    connection.last_received_at &&
    Date.now() - new Date(connection.last_received_at).getTime() < 86400000;
  return (
    <div className="pixel-settings">
      {error && (
        <div className="inline-feedback error-feedback" role="alert">
          <span>{error}</span>
          {!data && (
            <button onClick={() => perform(async () => {})} disabled={busy}>
              重试
            </button>
          )}
        </div>
      )}
      {!data ? (
        <p className="muted">
          {error ? "网站设置暂不可用" : "正在读取网站设置…"}
        </p>
      ) : (
        <>
          {/^http:\/\/(127\.0\.0\.1|localhost)/.test(data.public_url) && (
            <p className="pixel-local-note">
              <Info size={15} />
              <span>当前为本地环境，网站对外接入前需要配置公开地址。</span>
            </p>
          )}
          <section className="pixel-section">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                perform(async () => {
                  await request(`/agents/${aid}/analytics`, { site_url: site });
                });
              }}
            >
              <label htmlFor={fieldId}>产品网址</label>
              <div className="pixel-url-row">
                <input
                  id={fieldId}
                  type="url"
                  required
                  value={site}
                  placeholder="https://your-website.com"
                  disabled={readOnly || busy}
                  onChange={(e) => setSite(e.target.value)}
                />
                <button
                  className={connection ? "" : "primary"}
                  disabled={busy || !site || readOnly}
                  type="submit"
                >
                  {busy
                    ? "正在处理…"
                    : connection
                      ? "更新网址"
                      : "生成安装代码"}
                </button>
              </div>
            </form>
          </section>
          {connection && (
            <section className="pixel-section">
              <div className="pixel-section-heading">
                <div>
                  <h3>安装代码</h3>
                  <p>
                    将代码加入网站的 <code>&lt;head&gt;</code>{" "}
                    中，发布后等待首次访问。
                  </p>
                </div>
                <button
                  className="primary"
                  onClick={() => copy(connection.snippet, "snippet")}
                >
                  {copied === "snippet" ? (
                    <Check size={14} />
                  ) : (
                    <Copy size={14} />
                  )}{" "}
                  {copied === "snippet" ? "已复制" : "复制代码"}
                </button>
              </div>
              <textarea
                className="pixel-code"
                aria-label="网站统计安装代码"
                readOnly
                value={connection.snippet}
                rows={5}
                spellCheck={false}
              />
              <div className="pixel-connection">
                <div>
                  <span className="pixel-status">
                    <i className={`dot ${fresh ? "green" : ""}`} />
                    {fresh
                      ? "已收到网站数据"
                      : connection.state === "verified"
                        ? "最近 24 小时未收到回传"
                        : "等待网站数据"}
                  </span>
                  <p>
                    {connection.last_received_at
                      ? `最近收到：${new Date(connection.last_received_at).toLocaleString("zh-CN")}`
                      : "生成安装代码后，仍需在网站完成安装。"}
                  </p>
                </div>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    perform(async () => {
                      const value = await load();
                      setFeedback(
                        value.connection?.state === "verified"
                          ? "接入状态已更新"
                          : "尚未收到网站数据，请确认代码已发布。",
                      );
                    })
                  }
                >
                  <RefreshCw size={13} />
                  检查接入
                </button>
              </div>
              {feedback && (
                <p className="field-feedback" role="status">
                  {feedback}
                </p>
              )}
              <p className="pixel-measurement">
                有效访问仅统计来自推广链接、且前台活跃至少 10 秒的访问。
              </p>
            </section>
          )}
          {connection && (
            <details className="pixel-disclosure">
              <summary>
                <div>
                  <h3>服务端转化回传</h3>
                  <p>接收注册、购买等由网站服务端确认的转化。</p>
                </div>
                <ChevronDown size={15} />
              </summary>
              <div className="disclosure-content">
                <div className="pixel-field">
                  <label>回传接口</label>
                  <div className="copy-field">
                    <code>{data.public_url}/collect/server/conversion</code>
                    <button
                      className="icon"
                      aria-label="复制回传接口"
                      onClick={() =>
                        copy(
                          `${data.public_url}/collect/server/conversion`,
                          "endpoint",
                        )
                      }
                    >
                      {copied === "endpoint" ? (
                        <Check size={15} />
                      ) : (
                        <Copy size={15} />
                      )}
                    </button>
                  </div>
                </div>
                <div className="pixel-connection">
                  <span className="muted small">
                    {data.server?.last_received_at
                      ? "已收到服务端回传"
                      : "尚未收到服务端回传"}
                  </span>
                  <button
                    disabled={busy || readOnly}
                    onClick={() =>
                      perform(async () => {
                        const result = await request(
                          `/agents/${aid}/analytics/server-key`,
                          { connection_id: connection.id },
                        );
                        setServerKey(result.key);
                      })
                    }
                  >
                    {data.server ? "更换回传密钥" : "生成回传密钥"}
                  </button>
                </div>
                {serverKey && (
                  <div className="pixel-field">
                    <label>服务端回传密钥</label>
                    <textarea
                      className="pixel-code"
                      aria-label="服务端回传密钥"
                      readOnly
                      value={serverKey}
                      rows={2}
                    />
                    <div className="secret-key-actions">
                      <span className="muted small">
                        仅本次打开时显示，请保存在网站服务端。
                      </span>
                      <button onClick={() => copy(serverKey, "server-key")}>
                        {copied === "server-key" ? "已复制" : "复制密钥"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </details>
          )}
          <details className="pixel-disclosure">
            <summary>
              <div>
                <h3>
                  推广链接 <span className="muted">{data.links.length}</span>
                </h3>
                <p>让来自 Agent 的访问可以归因到推广内容。</p>
              </div>
              <ChevronDown size={15} />
            </summary>
            <div className="disclosure-content">
              <div className="pixel-link-heading">
                <span className="muted small">
                  目标：{site || "请先填写产品网址"}
                </span>
                <button
                  disabled={busy || !site || readOnly}
                  onClick={() =>
                    perform(async () => {
                      await request(`/agents/${aid}/tracking-links`, {
                        destination_url: site,
                        platform: "direct",
                      });
                    })
                  }
                >
                  创建推广链接
                </button>
              </div>
              {data.links.length ? (
                <ul className="pixel-link-list">
                  {data.links.map((link: Row) => (
                    <li key={link.id}>
                      <code title={link.url}>{link.url}</code>
                      <button
                        className="icon"
                        aria-label={`复制推广链接 ${link.url}`}
                        onClick={() => copy(link.url, link.id)}
                      >
                        {copied === link.id ? (
                          <Check size={15} />
                        ) : (
                          <Copy size={15} />
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">尚未创建推广链接</p>
              )}
            </div>
          </details>
        </>
      )}
    </div>
  );
}
