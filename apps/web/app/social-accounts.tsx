"use client";
import { useState } from "react";
import { Pause, Play } from "lucide-react";
import SocialIcon, { socialNames } from "./social-icon";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";
type Row = Record<string, any>;
export default function SocialAccounts({
  agent,
  platforms,
  readOnly,
  onConnect,
  onComplete,
}: {
  agent: Row;
  platforms: Row[];
  readOnly: boolean;
  onConnect: (p: Row) => void;
  onComplete: () => Promise<void>;
}) {
  const [open, setOpen] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const accounts = agent.accounts.filter((a: Row) => a.platform !== "lab");
  const unique: Row[] = Object.keys(socialNames).map((id) => ({
    id,
    name: socialNames[id],
    ...platforms.find((p) => p.id === id),
  }));
  const connected = unique.filter((p) =>
    accounts.some((a: Row) => a.platform === p.id && a.state === "connected"),
  );
  const operating = connected.filter(
    (p) =>
      accounts.some((a: Row) => a.platform === p.id && a.automation_enabled) &&
      agent.desired_state === "running",
  );
  async function toggle(id: string, enabled: boolean) {
    setBusy(true);
    setError("");
    try {
      await api(`/agents/${agent.id}/channels/${id}/control`, { enabled });
      await onComplete();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      id="social-accounts"
      className="workspace-section social-section"
      aria-labelledby="social-heading"
    >
      <div className="section-head">
        <h2 id="social-heading">社媒账号</h2>
        <span className="muted small">
          {connected.length} 已连接 · {operating.length} 运营中
        </span>
      </div>
      <div className="social-grid">
        {unique.map((p) => {
          const a = accounts.find(
            (a: Row) => a.platform === p.id && a.state === "connected",
          );
          const enabled =
            !!a && a.automation_enabled && agent.desired_state === "running";
          return (
            <div className="social-item" key={p.id}>
              <button
                data-platform={p.id}
                className={`social-trigger ${a ? "is-connected" : ""}`}
                disabled={!a && readOnly}
                aria-label={`${p.name}，${a ? (enabled ? "运营中" : "已连接，已暂停") : "连接账号"}`}
                aria-haspopup="dialog"
                onClick={() => {
                  setError("");
                  a ? setOpen(p.id) : onConnect(p);
                }}
              >
                <span className="social-symbol">
                  <SocialIcon platform={p.id} connected={!!a} />
                  {a && (
                    <span className={`social-state ${enabled ? "active" : ""}`}>
                      {!enabled && <Pause size={8} />}
                    </span>
                  )}
                </span>
                <span className="social-name">{p.name}</span>
                <span
                  className={`social-caption ${enabled ? "success" : "muted"}`}
                >
                  {a ? (enabled ? "运营中" : "已暂停") : "连接"}
                </span>
              </button>
              {a && open === p.id && (
                <DialogPanel
                  title={`${p.name} 账号`}
                  description="管理账号连接与此渠道的自主运营。"
                  size="compact"
                  onClose={() => {
                    if (!busy) setOpen("");
                  }}
                  footer={
                    <button disabled={busy} onClick={() => setOpen("")}>
                      完成
                    </button>
                  }
                >
                  <div className="social-account-detail">
                    <SocialIcon platform={p.id} connected size={32} />
                    <p>{a.display_name || a.username || "账号已连接"}</p>
                    <p className="muted small">
                      {!p.automation
                        ? "已支持登录，自主运营尚未开放"
                        : enabled
                          ? a.operational_ready
                            ? "正在自主运营"
                            : agent.health_state === "needs_attention"
                              ? "筹备需要处理，请查看工作记录"
                              : a.target_id
                                ? "Agent 正在验证运营环境"
                                : "Agent 正在准备运营环境"
                          : "启动后，Agent 将自动完成运营准备"}
                    </p>
                    {error && (
                      <p className="field-error" role="alert">
                        {error}
                      </p>
                    )}
                    <div className="settings-button-row">
                      <button
                        disabled={readOnly || busy || !p.automation}
                        onClick={() => toggle(p.id, !enabled)}
                      >
                        {enabled ? <Pause size={13} /> : <Play size={13} />}{" "}
                        {busy
                          ? "正在更新…"
                          : enabled
                            ? "暂停此社媒"
                            : "启动此社媒"}
                      </button>
                      <button
                        className="text-button"
                        disabled={readOnly || busy}
                        onClick={() => {
                          setOpen("");
                          onConnect(p);
                        }}
                      >
                        重新登录
                      </button>
                    </div>
                  </div>
                </DialogPanel>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
