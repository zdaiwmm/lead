"use client";
import { useId, useState } from "react";
import { Pencil, ExternalLink, Check, ArrowUpRight } from "lucide-react";
import { api } from "./api-client";
import DialogPanel from "./dialog-panel";
type Row = Record<string, any>;
export default function AgentPreferences({
  agent,
  readOnly,
  onComplete,
}: {
  agent: Row;
  readOnly: boolean;
  onComplete: () => Promise<void>;
  onError?: (s: string) => void;
}) {
  const [edit, setEdit] = useState(""),
    [view, setView] = useState(""),
    [revision, setRevision] = useState(0),
    [description, setDescription] = useState(""),
    [url, setUrl] = useState(""),
    [audience, setAudience] = useState(""),
    [preferences, setPreferences] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const formId = useId();
  const memory = agent.memory.filter((m: Row) => m.kind === "preference");
  function begin(kind: string) {
    setRevision(agent.config_revision);
    setDescription(agent.business?.description || "");
    setUrl(agent.business?.product_url || "");
    setAudience(agent.business?.facts?.audience || "");
    setPreferences(memory.map((m: Row) => m.body.text).join("\n"));
    setError("");
    setView("");
    setEdit(kind);
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api(
        `/agents/${agent.id}/${edit === "business" ? "business" : "preferences"}`,
        edit === "business"
          ? {
              expected_revision: revision,
              description,
              product_url: url.trim(),
              audience,
            }
          : {
              expected_revision: revision,
              entries: preferences.split("\n").filter((s) => s.trim()),
            },
      );
      await onComplete();
      setEdit("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function business(full = false) {
    return (
      <dl className="business-details">
        <div>
          <dt>业务介绍</dt>
          <dd>
            <p className={full ? "" : "business-description"}>
              {agent.business?.description || (
                <span className="muted">添加产品、服务与核心卖点</span>
              )}
            </p>
            {!full && agent.business?.description?.length > 110 && (
              <button
                className="text-button"
                aria-haspopup="dialog"
                onClick={() => setView("business")}
              >
                查看完整介绍
                <ArrowUpRight size={12} />
              </button>
            )}
          </dd>
        </div>
        <div>
          <dt>目标客户</dt>
          <dd>
            {agent.business?.facts?.audience || (
              <span className="muted">尚未明确，Agent 会逐步完善</span>
            )}
          </dd>
        </div>
        <div>
          <dt>产品网站</dt>
          <dd>
            {agent.business?.product_url ? (
              <a
                href={agent.business.product_url}
                target="_blank"
                rel="noreferrer"
              >
                {agent.business.product_url
                  .replace(/^https?:\/\//, "")
                  .replace(/\/$/, "")}
                <ExternalLink size={12} />
              </a>
            ) : (
              <span className="muted">待添加</span>
            )}
          </dd>
        </div>
      </dl>
    );
  }
  function preferenceList(full = false) {
    return (
      <ul
        className={
          full ? "preference-list preference-list-full" : "preference-list"
        }
      >
        {(full ? memory : memory.slice(0, 3)).map((m: Row) => (
          <li key={m.id}>
            <span
              title={
                m.confirmation === "user_explicit"
                  ? "你明确设定的偏好"
                  : "Agent 完善的偏好"
              }
            >
              {m.body.text}
            </span>
            {full && (
              <small>
                {m.confirmation === "user_explicit" ? "你设定的" : "Agent 完善"}
              </small>
            )}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <>
      <div className="preferences-grid">
        <section
          className="workspace-section preference-section"
          aria-labelledby="business-heading"
        >
          <div className="section-head">
            <h2 id="business-heading">业务偏好</h2>
            <button
              className="text-button"
              disabled={readOnly}
              aria-haspopup="dialog"
              onClick={() => begin("business")}
            >
              <Pencil size={13} />
              编辑
            </button>
          </div>
          {business()}
        </section>
        <section
          className="workspace-section preference-section"
          aria-labelledby="preferences-heading"
        >
          <div className="section-head">
            <h2 id="preferences-heading">运营偏好</h2>
            <button
              className="text-button"
              disabled={readOnly}
              aria-haspopup="dialog"
              onClick={() => begin("preferences")}
            >
              <Pencil size={13} />
              编辑
            </button>
          </div>
          {memory.length ? (
            <>
              {preferenceList()}
              {memory.length > 3 && (
                <button
                  className="text-button preference-view-all"
                  aria-haspopup="dialog"
                  onClick={() => setView("preferences")}
                >
                  查看全部 {memory.length} 条偏好
                  <ArrowUpRight size={12} />
                </button>
              )}
              <p className="preference-source muted small">
                你设定{" "}
                {
                  memory.filter((m: Row) => m.confirmation === "user_explicit")
                    .length
                }{" "}
                条 · Agent 完善{" "}
                {
                  memory.filter((m: Row) => m.confirmation !== "user_explicit")
                    .length
                }{" "}
                条
              </p>
            </>
          ) : (
            <div className="preference-empty">
              <p>让 Agent 更懂你的运营方式</p>
              <span className="muted small">
                可编辑内容方向，也可以在右侧对话中补充。后续学到的偏好会展示在这里。
              </span>
              <button
                className="text-button"
                disabled={readOnly}
                aria-haspopup="dialog"
                onClick={() => begin("preferences")}
              >
                添加运营偏好
                <Pencil size={12} />
              </button>
            </div>
          )}
        </section>
      </div>
      {edit && (
        <DialogPanel
          title={edit === "business" ? "编辑业务偏好" : "编辑运营偏好"}
          description={
            edit === "business"
              ? "调整业务介绍、目标客户和产品网站。"
              : "设定内容方向与运营边界，Agent 会在此基础上逐步完善。"
          }
          onClose={() => {
            if (!busy) setEdit("");
          }}
          footer={
            <>
              <button disabled={busy} onClick={() => setEdit("")}>
                取消
              </button>
              <button
                className="primary"
                form={formId}
                type="submit"
                disabled={busy}
              >
                <Check size={14} />
                {busy ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          <form
            id={formId}
            className="modal-edit-form"
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            {error && (
              <p className="inline-feedback error-feedback" role="alert">
                {error}
              </p>
            )}
            {edit === "business" ? (
              <>
                <label>
                  产品网站
                  <input
                    type="url"
                    maxLength={2000}
                    placeholder="https://"
                    value={url}
                    disabled={busy}
                    onChange={(e) => setUrl(e.target.value)}
                  />
                </label>
                <label>
                  业务介绍
                  <textarea
                    rows={5}
                    maxLength={10000}
                    value={description}
                    disabled={busy}
                    onChange={(e) => setDescription(e.target.value)}
                  />
                </label>
                <label>
                  目标客户
                  <textarea
                    rows={3}
                    maxLength={2000}
                    value={audience}
                    disabled={busy}
                    onChange={(e) => setAudience(e.target.value)}
                  />
                </label>
              </>
            ) : (
              <label>
                内容方向、语言、语气与运营节奏
                <textarea
                  rows={10}
                  value={preferences}
                  disabled={busy}
                  placeholder={
                    "每行一条偏好，例如：\n使用英文，保持自然、专业的语气\n重点介绍真实使用场景"
                  }
                  onChange={(e) => setPreferences(e.target.value)}
                />
                <span className="field-help">
                  每行一条偏好，保存后会用于后续自主运营。
                </span>
              </label>
            )}
          </form>
        </DialogPanel>
      )}
      {view && (
        <DialogPanel
          title={view === "business" ? "业务资料" : "全部运营偏好"}
          onClose={() => setView("")}
          footer={
            <>
              <button onClick={() => setView("")}>关闭</button>
              {!readOnly && (
                <button
                  onClick={() =>
                    begin(view === "business" ? "business" : "preferences")
                  }
                >
                  <Pencil size={14} />
                  编辑
                </button>
              )}
            </>
          }
        >
          {view === "business" ? business(true) : preferenceList(true)}
        </DialogPanel>
      )}
    </>
  );
}
