"use client";

import { useEffect, useState } from "react";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import { api } from "../api-client";
import { Row } from "./config-editor";
import { Navigate } from "./navigation";

const actions: Record<string, string> = {
  business: "补充业务介绍",
  models: "选择可用模型",
  brand: "补充品牌资料",
  knowledge_review: "核对知识资料",
  channels: "连接或检查账号",
};

export default function Overview({
  agents,
  navigate,
  revision,
}: {
  agents: Row[];
  navigate: Navigate;
  revision: number;
}) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setData(null);
    setError("");
    api("/admin/readiness")
      .then((value) => {
        if (active) setData(value);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [revision, retry]);
  const runtimeIssues = agents.filter(
    (a) => a.health_state === "needs_attention" || a.remaining === 0,
  );
  const configurationIssues = (data?.agents || []).filter((a: Row) =>
    a.checks.some((c: Row) => c.state === "needs_attention"),
  );
  const needsAttention = new Set(
    [...runtimeIssues, ...configurationIssues].map((a) => a.id),
  ).size;
  return (
    <>
      <section
        className="admin-overview admin-status-overview"
        aria-label="运行概况"
      >
        <dl>
          <div>
            <dt>当前 Agent</dt>
            <dd>{agents.length}</dd>
          </div>
          <div>
            <dt>已开启运营</dt>
            <dd>
              {agents.filter((a) => a.desired_state === "running").length}
            </dd>
          </div>
          <div>
            <dt>已暂停</dt>
            <dd>{agents.filter((a) => a.desired_state === "paused").length}</dd>
          </div>
          <div>
            <dt>需要关注的 Agent</dt>
            <dd>{data ? needsAttention : "待检查"}</dd>
          </div>
        </dl>
        <p className="admin-help">
          已开启表示允许自动运营；实际进展和获客结果在各 Agent 工作台查看。
        </p>
      </section>
      <section className="admin-editor">
        <header className="admin-section-heading">
          <div>
            <h2>需要你处理</h2>
            <p>运行异常优先显示，资料缺口按 Agent 列出下一步。</p>
          </div>
          <button onClick={() => navigate("readiness")}>查看检查明细</button>
        </header>
        {error && (
          <p className="admin-feedback error-feedback" role="alert">
            配置检查暂时不可用，当前缺口尚未确认。{error}
            <button onClick={() => setRetry((v) => v + 1)}>重新检查</button>
          </p>
        )}
        {!data && !error && (
          <p role="status" className="admin-loading">
            正在检查资料、模型和账号…
          </p>
        )}
        <ul className="admin-attention-list">
          {runtimeIssues.map((agent) => (
            <li key={agent.id}>
              <div>
                <strong>
                  {agent.name} ·{" "}
                  {agent.remaining === 0 ? "额度不足" : "运行需要处理"}
                </strong>
                <p>
                  {agent.remaining === 0
                    ? "今日剩余额度为零，或已被调用中预留占满。"
                    : "前往工作台查看具体原因与恢复入口。"}
                </p>
              </div>
              {agent.remaining === 0 ? (
                <button onClick={() => navigate("budgets", agent.id)}>
                  查看额度
                </button>
              ) : (
                <a href={`/?agent=${agent.id}`}>
                  查看运行情况 <ArrowRight size={14} aria-hidden="true" />
                </a>
              )}
            </li>
          ))}
          {configurationIssues.map((agent: Row) => {
            const checks = agent.checks.filter(
              (c: Row) => c.state === "needs_attention",
            );
            const check =
              checks.find((c: Row) => c.code === "models") || checks[0];
            return (
              <li key={`config-${agent.id}`}>
                <div>
                  <strong>
                    {agent.name} · {check.title}
                  </strong>
                  <p>{check.detail}</p>
                  {checks.length > 1 && (
                    <small className="muted">
                      另有 {checks.length - 1} 项待完善，可在检查明细中查看。
                    </small>
                  )}
                </div>
                {check.section === "workbench" ? (
                  <a href={`/?agent=${agent.id}`}>
                    {actions[check.code] || "打开工作台"}{" "}
                    <ArrowRight size={14} aria-hidden="true" />
                  </a>
                ) : (
                  <button onClick={() => navigate(check.section, agent.id)}>
                    {actions[check.code] || "处理此项"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
        {data && agents.length > 0 && !needsAttention && (
          <p className="admin-empty">
            <CheckCircle2 size={18} aria-hidden="true" />{" "}
            当前没有运行异常或配置缺口。运营进展请在工作台查看。
          </p>
        )}
        {!agents.length && (
          <p className="admin-empty">
            还没有 Agent。<a href="/">前往工作台介绍业务并创建</a>
            ，随后连接首个社媒账号。
          </p>
        )}
      </section>
      <div className="admin-overview-footer">
        <p>
          日常只需补充业务信息、连接账号和查看结果；已有默认配置可以继续使用。
        </p>
        <a href="/">
          返回运营工作台 <ArrowRight size={15} aria-hidden="true" />
        </a>
      </div>
    </>
  );
}
