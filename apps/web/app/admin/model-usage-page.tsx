"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../api-client";
import ModelUsage from "../model-usage";
import { Row } from "./config-editor";
import Disclosure from "./disclosure";

export default function ModelUsagePage({ agents }: { agents: Row[] }) {
  const [aid, setAid] = useState(""),
    [archived, setArchived] = useState(false),
    [data, setData] = useState<Row | null>(null),
    [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      setData(
        await api(
          `/admin/model-usage?include_archived=${archived}${aid ? "&aid=" + aid : ""}`,
        ),
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, [aid, archived]);
  useEffect(() => {
    setData(null);
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, [load]);
  const rows: Row[] = data?.by_model || [];
  const costs = rows.reduce<Record<string, number>>((total, row) => {
    for (const [currency, value] of Object.entries(row.costs || {})) {
      total[currency] = (total[currency] || 0) + Number(value);
    }
    return total;
  }, {});
  const unpriced = rows.reduce(
    (sum, row) => sum + (row.unpriced_calls || 0),
    0,
  );
  const amount = (value: number) =>
    new Intl.NumberFormat("zh-CN").format(value);
  return (
    <>
      <div className="admin-toolbar">
        <label className="admin-search">
          统计范围
          <select value={aid} onChange={(e) => setAid(e.target.value)}>
            <option value="">工作室当前 Agent</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => void load()}>刷新用量</button>
      </div>
      {!aid && (
        <label className="admin-checkbox admin-usage-archive">
          <input
            type="checkbox"
            checked={archived}
            onChange={(e) => setArchived(e.target.checked)}
          />
          包含已归档 Agent 的历史记录
        </label>
      )}
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {!data && !error && <p role="status">正在读取实际模型用量…</p>}
      {data && (
        <section className="admin-editor">
          <header className="admin-section-heading">
            <div>
              <h2>实际用量与费用</h2>
              <p>近 30 天统计；今日用量在北京时间每天 08:00 重置。</p>
            </div>
          </header>
          <dl className="admin-usage-summary">
            <div>
              <dt>今日已用 token</dt>
              <dd>
                {amount(rows.reduce((sum, row) => sum + row.today_tokens, 0))}
              </dd>
            </div>
            <div>
              <dt>近 30 天已用 token</dt>
              <dd>{amount(rows.reduce((sum, row) => sum + row.tokens, 0))}</dd>
            </div>
            <div>
              <dt>
                近 30 天费用估算
                {unpriced > 0 && Object.keys(costs).length > 0
                  ? "（部分）"
                  : ""}
              </dt>
              <dd>
                {Object.keys(costs).length
                  ? Object.entries(costs).map(([currency, value]) => (
                      <span key={currency}>
                        {currency}{" "}
                        {value.toLocaleString("zh-CN", {
                          maximumFractionDigits: 8,
                        })}
                      </span>
                    ))
                  : "暂无法估算"}
              </dd>
            </div>
          </dl>
          {unpriced > 0 && (
            <p className="admin-help">
              {unpriced} 次调用尚未完整计价。
              {Object.keys(costs).length
                ? "当前估算只包含已覆盖部分。"
                : "缺少完整计价依据，费用保留未知。"}
            </p>
          )}
          {!rows.length && (
            <p className="admin-empty">
              此范围尚无用量记录，发生实际调用后会自动更新。
            </p>
          )}
          <Disclosure title="查看模型、缓存与待核对明细">
            <ModelUsage rows={rows} />
          </Disclosure>
        </section>
      )}
    </>
  );
}
