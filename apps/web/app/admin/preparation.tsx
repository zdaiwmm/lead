"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../api-client";
import { Row } from "./config-editor";

function ContentPreview({ body }: { body: Row }) {
  return (
    <div className="admin-content-preview">
      {body.platform ? (
        <>
          <p>{body.cadence}</p>
          <p>{body.conversion_path}</p>
          {body.plans.map((plan: Row) => (
            <section key={plan.id}>
              <h4>{plan.name}</h4>
              <p>{plan.hypothesis}</p>
              <p>{plan.first_batch.join("；")}</p>
              <p>{plan.template}</p>
              <p className="muted">{plan.iterate}</p>
            </section>
          ))}
        </>
      ) : body.fields ? (
        <>
          <p>{body.steps.join(" → ")}</p>
          {Object.entries(body.fields).map(([key, value]) => {
            const field = value as Row;
            return (
              <section key={key}>
                <h4>{field.label}</h4>
                <p>{field.help}</p>
                <p className="muted">示例：{field.placeholder}</p>
                {field.default && <p>默认：{field.default}</p>}
              </section>
            );
          })}
        </>
      ) : (
        <>
          <p>
            {body.name ||
              "通用规则适用于各业务，具体品牌名称在 Agent 配置中维护。"}
          </p>
          <p>{body.voice}</p>
          {body.positioning && <p>{body.positioning}</p>}
          {body.documents.map((document: Row) => (
            <section key={document.id}>
              <h4>{document.title}</h4>
              <p>{document.content}</p>
              <small>
                已确认 ·{" "}
                {document.valid_until
                  ? `有效至 ${document.valid_until}`
                  : "持续适用"}{" "}
                · {document.source}
              </small>
            </section>
          ))}
        </>
      )}
    </div>
  );
}

export function ContentLibrary({ readOnly }: { readOnly: boolean }) {
  const [data, setData] = useState<Row | null>(null),
    [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState(""),
    [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const pack = await api("/admin/content-library");
      setData(pack);
      setSelected(
        pack.entries
          .filter((e: Row) => e.state === "missing")
          .map((e: Row) => e.key),
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  async function initialize() {
    if (!data || !selected.length) return;
    setBusy(true);
    setFeedback("");
    setError("");
    try {
      const result = await api("/admin/content-library/initialize", {
        content_digest: data.content_digest,
        expected_versions: Object.fromEntries(
          data.entries
            .filter((e: Row) => selected.includes(e.key))
            .map((e: Row) => [e.key, e.version]),
        ),
      });
      setFeedback(
        `已发布 ${result.applied.length} 项初始化配置，保留 ${result.preserved.length} 项已有配置。版本和来源已写入变更记录。`,
      );
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <section className="admin-service-status">
        <div>
          <strong>从已有研究填充可用配置</strong>
          <p>
            渠道方法来自既有运营研究，创建引导提供通用说明。业务专属知识仅匹配对应产品；已有编辑和草稿会保留。
          </p>
          <small>
            初始化会直接发布所选配置，用于后续工作；品牌变更会撤回旧方向的排队内容。模板不会扩大账号授权。
          </small>
        </div>
      </section>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
          <button type="button" disabled={busy} onClick={() => void load()}>
            重新读取资料包
          </button>
        </p>
      )}
      {feedback && (
        <p role="status" className="admin-feedback">
          {feedback}
        </p>
      )}
      {!data && !error && <p role="status">正在读取资料包…</p>}
      {data && (
        <section className="admin-editor">
          <header className="admin-section-heading">
            <div>
              <h2>研究与配置资料包</h2>
              <p>
                版本 {data.pack_version} · 来源核对于 {data.observed_on}
              </p>
            </div>
            <button type="button" disabled={busy} onClick={() => void load()}>
              刷新预览
            </button>
          </header>
          <ul className="admin-content-list">
            {data.entries.map((entry: Row) => (
              <li key={entry.key}>
                <div className="admin-content-heading">
                  <label className="admin-checkbox">
                    <input
                      type="checkbox"
                      disabled={readOnly || busy || entry.state !== "missing"}
                      checked={selected.includes(entry.key)}
                      onChange={(e) =>
                        setSelected((previous) =>
                          e.target.checked
                            ? [...previous, entry.key]
                            : previous.filter((key) => key !== entry.key),
                        )
                      }
                    />
                    {entry.title}
                  </label>
                  <span className="muted small">
                    {entry.state === "missing"
                      ? "待初始化"
                      : entry.published_version
                        ? `已发布 v${entry.published_version}，保留`
                        : "已有草稿，保留"}
                  </span>
                </div>
                <p className="admin-help">
                  {entry.body.plans
                    ? `${entry.body.plans.length} 套方案，${entry.body.plans.reduce((n: number, p: Row) => n + p.first_batch.length, 0)} 个首批选题`
                    : entry.body.documents
                      ? `${entry.body.documents.length} 条有来源资料`
                      : "6 项字段说明、输入示例及运营方向"}
                </p>
                <details>
                  <summary>预览资料包内容与来源</summary>
                  <ContentPreview body={entry.body} />
                  <ul className="admin-source-list">
                    {entry.sources.map((source: string) => (
                      <li key={source}>
                        {source.startsWith("https://") ? (
                          <a href={source} target="_blank" rel="noreferrer">
                            {source}
                          </a>
                        ) : (
                          <span>{source}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </details>
              </li>
            ))}
          </ul>
          {!readOnly && (
            <div className="admin-library-actions">
              <p>
                {selected.length
                  ? `已选择 ${selected.length} 项。发布后可在对应页面编辑或通过变更记录回滚。`
                  : "当前没有选中待初始化内容。已有配置可在对应页面继续维护。"}
              </p>
              <button
                type="button"
                className="primary"
                disabled={busy || !selected.length}
                onClick={() => void initialize()}
              >
                {busy ? "正在发布…" : "发布所选初始化内容"}
              </button>
            </div>
          )}
        </section>
      )}
    </>
  );
}

const stateNames: Row = {
  ready: "已就绪",
  needs_attention: "需要处理",
  not_connected: "尚无近期数据",
};
export function Readiness({
  navigate,
}: {
  navigate: (section: string) => void;
}) {
  const [data, setData] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [query, setQuery] = useState("");
  const load = useCallback(async () => {
    try {
      setData(await api("/admin/readiness"));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <>
      <div className="admin-toolbar">
        <label className="admin-search">
          查找 Agent
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="按名称查找"
          />
        </label>
        <button onClick={() => void load()}>重新检查</button>
      </div>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {!data && !error && <p role="status">正在核对当前配置与实际状态…</p>}
      {data && (
        <>
          <section className="admin-service-status">
            <div>
              <strong>
                {data.missing_templates
                  ? `${data.missing_templates} 项资料尚未发布`
                  : "研究资料已发布到配置"}
              </strong>
              <p>
                按真实资料、凭据、账号权限及回传状态检查。配置完整不代表已经发布或带来获客。
              </p>
              <small>
                检查于{" "}
                {new Date(data.checked_at).toLocaleString("zh-CN", {
                  timeZone: "Asia/Shanghai",
                })}{" "}
                · 数据回传按各 Agent 已发布的新鲜度窗口核对。
              </small>
            </div>
            <button onClick={() => navigate("content")}>查看资料初始化</button>
          </section>
          {data.agents
            .filter((a: Row) =>
              a.name.toLowerCase().includes(query.toLowerCase()),
            )
            .map((agent: Row) => (
              <section className="admin-editor" key={agent.id}>
                <header className="admin-section-heading">
                  <div>
                    <h2>{agent.name}</h2>
                    <p>
                      {agent.setup_pending
                        ? "创建流程尚未完成"
                        : "当前业务的配置与接入情况"}
                    </p>
                  </div>
                  <a href={`/?agent=${agent.id}`}>打开工作台</a>
                </header>
                <ul className="admin-readiness-list">
                  {agent.checks.map((check: Row) => (
                    <li key={check.code}>
                      <div>
                        <strong>{check.title}</strong>
                        <span
                          className={`admin-readiness-state ${check.state}`}
                        >
                          {stateNames[check.state]}
                        </span>
                        <p>{check.detail}</p>
                      </div>
                      {check.section !== "workbench" && (
                        <button onClick={() => navigate(check.section)}>
                          管理配置
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
                {!!agent.channels.length && (
                  <details className="admin-channel-readiness">
                    <summary>渠道授权与验证明细</summary>
                    <ul>
                      {agent.channels.map((channel: Row, index: number) => (
                        <li
                          key={`${channel.platform}/${channel.target_id}/${index}`}
                        >
                          <strong>
                            {channel.name || channel.platform} ·{" "}
                            {channel.platform}
                          </strong>
                          <p>
                            {channel.ready
                              ? `已验证格式：${channel.verified_formats.join("、")}；${channel.automation_enabled ? "渠道运营已启用" : "渠道运营已暂停"}`
                              : channel.reasons.join("；")}
                          </p>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </section>
            ))}
          {!data.agents.length && (
            <p className="admin-empty">
              尚无 Agent。可以先填充通用资料，再从工作台创建。
            </p>
          )}
          {data.agents.length >= data.agent_limit && (
            <p className="admin-help">
              当前显示最近 {data.agent_limit} 个 Agent。
            </p>
          )}
        </>
      )}
    </>
  );
}
