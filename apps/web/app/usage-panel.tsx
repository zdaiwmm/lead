"use client";
import { ReactNode, useEffect, useState } from "react";
import { ArrowUpRight, Download } from "lucide-react";
import { api, workspaceHeaders } from "./api-client";
import TokenBudget from "./token-budget";
import DialogPanel from "./dialog-panel";
import ModelUsage from "./model-usage";
type Row = Record<string, any>;
export default function UsagePanel({
  agent,
  readOnly = false,
  canManageBudget = true,
  children,
}: {
  agent: string;
  readOnly?: boolean;
  canManageBudget?: boolean;
  children?: ReactNode;
}) {
  const [dialogError, setDialogError] = useState("");
  const [usage, setUsage] = useState<Row | null>(null),
    [budget, setBudget] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [more, setMore] = useState(false);
  useEffect(() => {
    let active = true;
    const load = () =>
      Promise.all([
        api(`/agents/${agent}/usage`),
        api(`/agents/${agent}/budget`),
      ])
        .then(([u, b]) => {
          if (active) {
            setUsage(u);
            setBudget(b);
            setError("");
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    load();
    const t = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [agent]);
  const quota = usage?.quotas.find(
    (q: Row) =>
      new Date(q.starts_at).getTime() <= Date.now() &&
      new Date(q.ends_at).getTime() > Date.now(),
  );
  const limit = quota?.limit_units ?? budget?.daily_token_limit;
  const spent = quota?.spent_units ?? (budget ? 0 : null);
  const costs = Object.entries(usage?.model_costs || {});
  const partial = !!usage?.unpriced_calls;
  async function download() {
    setDialogError("");
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
      setDialogError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="workspace-section usage-section"
      aria-labelledby="usage-heading"
    >
      <div className="section-head">
        <h2 id="usage-heading">高级设置</h2>
        <button
          className="text-button"
          aria-haspopup="dialog"
          onClick={() => {
            setDialogError("");
            setMore(true);
          }}
        >
          更多
          <ArrowUpRight size={13} />
        </button>
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <dl className="usage-summary">
        <div>
          <dt>近 30 天 Tokens</dt>
          <dd>{usage?.model_tokens.toLocaleString() ?? "—"}</dd>
          <small>
            已知输入 {usage?.input_tokens.toLocaleString() ?? "—"} · 输出{" "}
            {usage?.output_tokens.toLocaleString() ?? "—"}
          </small>
        </div>
        <div>
          <dt>预估模型费用</dt>
          <dd>
            {costs.length
              ? costs
                  .map(([c, v]) => `${c} ${Number(v).toFixed(2)}`)
                  .join(" / ")
              : "暂无法估算"}
          </dd>
          <small>
            {costs.length
              ? partial
                ? `部分计价，${usage?.unpriced_calls} 次未计价`
                : "按已记录计价版本计算"
              : partial
                ? "存在未计价调用"
                : "暂无可计价记录"}
          </small>
        </div>
        <div>
          <dt>今日额度</dt>
          <dd>
            {spent?.toLocaleString() ?? "—"}
            <span className="muted"> / {limit?.toLocaleString() ?? "—"}</span>
          </dd>
          <small className="quota-line">
            <progress
              aria-label="今日 Token 额度使用比例"
              max={Math.max(1, limit ?? 1)}
              value={Math.min(limit ?? 0, spent ?? 0)}
            />
            {limit
              ? `${Math.round(((spent ?? 0) / limit) * 100)}%`
              : limit === 0
                ? "已关闭"
                : "—"}
          </small>
        </div>
        <div>
          <dt>浏览器运行</dt>
          <dd>
            {usage
              ? `${Math.floor(usage.browser_seconds / 3600)} 小时 ${Math.floor((usage.browser_seconds % 3600) / 60)} 分`
              : "—"}
          </dd>
          <small>近 30 天累计</small>
        </div>
      </dl>
      {more && (
        <DialogPanel
          title="高级设置"
          description="管理模型用量、查看费用与数据，以及 Agent 的运行环境。"
          onClose={() => setMore(false)}
          footer={<button onClick={() => setMore(false)}>完成</button>}
        >
          <div className="settings-dialog-content">
            {dialogError && (
              <p className="inline-feedback error-feedback" role="alert">
                {dialogError}
              </p>
            )}
            {!readOnly && canManageBudget && (
              <section className="settings-group">
                <div className="settings-group-heading">
                  <h3>额度设置</h3>
                  <p>控制 Agent 每天可以使用的模型用量。</p>
                </div>
                <TokenBudget
                  agent={agent}
                  onSaved={() => {
                    Promise.all([
                      api(`/agents/${agent}/usage`),
                      api(`/agents/${agent}/budget`),
                    ])
                      .then(([u, b]) => {
                        setUsage(u);
                        setBudget(b);
                      })
                      .catch((e) => setDialogError(e.message));
                  }}
                />
              </section>
            )}
            <section className="settings-group">
              <div className="settings-group-heading">
                <h3>按模型查看 token 用量</h3>
                <p>区分各模型的实际消耗与待核对预留。</p>
              </div>
              <ModelUsage rows={usage?.by_model || []} />
            </section>
            <section className="settings-group">
              <div className="settings-group-heading">
                <h3>费用与数据</h3>
                <p>
                  模型费用不含浏览器、代理与存储。存在未计价调用时，仅展示已知费用。
                </p>
              </div>
              {!!usage?.pricing_versions?.length && (
                <div className="pricing-versions">
                  {usage.pricing_versions.map((p: string[], i: number) => (
                    <p className="muted small" key={i}>
                      {p[0]} · {p[1]} · {p[2]}
                      <br />
                      生效时间：{p[3]}
                    </p>
                  ))}
                </div>
              )}
              <div className="settings-action-row">
                <div>
                  <p>Agent 数据</p>
                  <span className="muted small">
                    素材占用{" "}
                    {usage ? (usage.asset_bytes / 1024 / 1024).toFixed(1) : "—"}{" "}
                    MB，可导出保存。
                  </span>
                </div>
                <button disabled={busy} onClick={download}>
                  <Download size={14} />
                  {busy ? "正在导出…" : "导出数据"}
                </button>
              </div>
            </section>
            {children}
          </div>
        </DialogPanel>
      )}
    </section>
  );
}
