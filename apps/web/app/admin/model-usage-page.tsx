"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../api-client";
import ModelUsage from "../model-usage";
import { Row } from "./config-editor";

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
              <h2>按模型统计</h2>
              <p>已发生的消耗按当时的模型保留，切换模型不会转移历史用量。</p>
            </div>
          </header>
          <ModelUsage rows={data.by_model} />
        </section>
      )}
    </>
  );
}
