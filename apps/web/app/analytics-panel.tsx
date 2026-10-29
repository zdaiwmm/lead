"use client";
import { useEffect, useState } from "react";
type Row = Record<string, any>;
export default function AnalyticsPanel({
  aid,
  readOnly = false,
  url,
  request,
  onError,
}: {
  aid: string;
  readOnly?: boolean;
  url?: string;
  request: (path: string, body?: Row) => Promise<any>;
  onError: (s: string) => void;
}) {
  const [data, setData] = useState<Row | null>(null),
    [site, setSite] = useState(url || ""),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState("");
  const [serverKey, setServerKey] = useState("");
  async function load() {
    setData(await request("/agents/" + aid + "/analytics"));
  }
  useEffect(() => {
    load().catch((e) => onError(e.message));
  }, [aid]); // eslint-disable-line react-hooks/exhaustive-deps
  async function perform(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
    } catch {
      onError("请选中内容复制");
    }
  }
  return (
    <details className="advanced analytics-panel">
      <summary>接入网站统计</summary>
      <div className="advanced-content">
        {!data ? (
          <p className="muted small">正在读取…</p>
        ) : (
          <>
            {data.public_url.startsWith("http://127.0.0.1") && (
              <p className="muted small">
                当前链接仅供本机使用，连接外部网站前需部署公开地址。
              </p>
            )}
            <label htmlFor="site-url">产品网址</label>
            <div className="analytics-form">
              <input
                id="site-url"
                type="url"
                value={site}
                onChange={(e) => setSite(e.target.value)}
                placeholder="https://"
              />
              <button
                disabled={busy || !site || readOnly}
                onClick={() =>
                  perform(async () => {
                    await request("/agents/" + aid + "/analytics", {
                      site_url: site,
                    });
                  })
                }
              >
                {data.connection ? "更新" : "生成安装代码"}
              </button>
            </div>
            {data.connection && (
              <>
                <div className="setting-row">
                  <span>网站统计</span>
                  <span className="muted">
                    {data.connection.state === "verified"
                      ? "已收到网站数据"
                      : "等待网站数据"}
                  </span>
                </div>
                <textarea
                  aria-label="网站统计安装代码"
                  readOnly
                  value={data.connection.snippet}
                  rows={4}
                />
                <button
                  onClick={() => copy(data.connection.snippet, "snippet")}
                >
                  {copied === "snippet" ? "已复制" : "复制安装代码"}
                </button>
                <p className="muted small">
                  将代码加入产品网站。有效访问按前台活跃至少 10 秒计算。
                </p>
              </>
            )}
            {data.connection && (
              <details className="lab">
                <summary>服务端转化回传</summary>
                <button
                  disabled={busy || readOnly}
                  onClick={() =>
                    perform(async () => {
                      const issued = await request(
                        `/agents/${aid}/analytics/server-key`,
                        { connection_id: data.connection.id },
                      );
                      setServerKey(issued.key);
                    })
                  }
                >
                  {data.server ? "更换回传密钥" : "生成回传密钥"}
                </button>
                {serverKey && (
                  <>
                    <p className="muted small">
                      仅保存在网站服务端。此处只显示一次。
                    </p>
                    <textarea
                      aria-label="服务端回传密钥"
                      readOnly
                      value={serverKey}
                      rows={2}
                    />
                    <button onClick={() => copy(serverKey, "server-key")}>
                      {copied === "server-key" ? "已复制" : "复制密钥"}
                    </button>
                  </>
                )}
                <p className="muted small">
                  接口：{data.public_url}/collect/server/conversion
                </p>
                <p className="muted small">
                  {data.server?.last_received_at
                    ? "已收到服务端回传"
                    : "等待服务端回传"}
                </p>
              </details>
            )}
            <div className="section-head">
              <h2>推广链接</h2>
              <button
                disabled={busy || !site || readOnly}
                onClick={() =>
                  perform(async () => {
                    await request("/agents/" + aid + "/tracking-links", {
                      destination_url: site,
                      platform: "direct",
                    });
                  })
                }
              >
                创建链接
              </button>
            </div>
            {data.links.map((link: Row) => (
              <div className="tracking-row" key={link.id}>
                <code>{link.url}</code>
                <button onClick={() => copy(link.url, link.id)}>
                  {copied === link.id ? "已复制" : "复制"}
                </button>
              </div>
            ))}
            <p className="muted small">
              只统计通过推广链接进入、并由网站回传的访问。链接点击单独计数。
            </p>
          </>
        )}
      </div>
    </details>
  );
}
