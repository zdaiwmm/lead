"use client";
import { useState } from "react";
import { api, workspaceHeaders } from "./api-client";
type Row = Record<string, any>;

export default function UsagePanel({ agent }: { agent: string }) {
  const [usage, setUsage] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const quota = usage?.quotas.find(
    (q: Row) =>
      new Date(q.starts_at).getTime() <= Date.now() &&
      new Date(q.ends_at).getTime() > Date.now(),
  );
  async function download() {
    setError("");
    setBusy(true);
    try {
      const response = await fetch(`/api/agents/${agent}/export`, {
        headers: workspaceHeaders(),
        cache: "no-store",
      });
      if (!response.ok) throw Error("导出未完成，请稍后重试");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `lead-${agent}.zip`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details
      className="advanced"
      key={agent}
      onToggle={(e) => {
        if (e.currentTarget.open) {
          setUsage(null);
          setError("");
          api(`/agents/${agent}/usage`)
            .then(setUsage)
            .catch((e) => setError(e.message));
        }
      }}
    >
      <summary>用量与数据</summary>
      <div className="advanced-content">
        {error && <p role="alert">{error}</p>}
        {usage && (
          <>
            <div className="setting-row">
              <span>近 30 天模型用量</span>
              <span>{usage.model_tokens.toLocaleString()} tokens</span>
            </div>
            <div className="setting-row">
              <span>浏览器运行</span>
              <span>
                {Math.ceil(usage.browser_seconds / 60).toLocaleString()} 分钟
              </span>
            </div>
            <div className="setting-row">
              <span>素材</span>
              <span>{(usage.asset_bytes / 1024 / 1024).toFixed(1)} MB</span>
            </div>
            {quota && (
              <div className="setting-row">
                <span>今日模型额度</span>
                <span>
                  {quota.spent_units.toLocaleString()} /{" "}
                  {quota.limit_units.toLocaleString()}
                </span>
              </div>
            )}
          </>
        )}
        <button disabled={busy} onClick={download}>
          {busy ? "正在导出…" : "导出我的数据"}
        </button>
      </div>
    </details>
  );
}
