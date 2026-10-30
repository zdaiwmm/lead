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
  const [busy, setBusy] = useState("");
  const [confirmDisconnect, setConfirmDisconnect] = useState<Row | null>(null);
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
    setBusy("control");
    setError("");
    try {
      await api(`/agents/${agent.id}/channels/${id}/control`, { enabled });
      await onComplete();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  async function disconnect(account: Row) {
    setBusy("disconnect");
    setError("");
    try {
      await api(`/agents/${agent.id}/accounts/${account.id}/disconnect`, {
        expected_verified_at: account.verified_at,
      });
      await onComplete();
      setOpen("");
      setConfirmDisconnect(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
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
            (a: Row) =>
              a.platform === p.id &&
              ["connected", "needs_auth", "restricted"].includes(a.state),
          );
          const isConnected = a?.state === "connected";
          const enabled =
            isConnected &&
            a.automation_enabled &&
            agent.desired_state === "running";
          const caption = !a
            ? "连接"
            : a.state === "needs_auth"
              ? "登录失效"
              : a.state === "restricted"
                ? "账号受限"
                : enabled
                  ? "运营中"
                  : "已暂停";
          const displayed = confirmDisconnect || a;
          return (
            <div className="social-item" key={p.id}>
              <button
                data-platform={p.id}
                className={`social-trigger ${a ? "is-connected" : ""}`}
                disabled={!a && readOnly}
                aria-label={`${p.name}，${a ? caption : "连接账号"}`}
                aria-haspopup="dialog"
                onClick={() => {
                  setError("");
                  setConfirmDisconnect(null);
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
                  {caption}
                </span>
              </button>
              {a && open === p.id && (
                <DialogPanel
                  title={
                    confirmDisconnect ? `解除 ${p.name} 绑定` : `${p.name} 账号`
                  }
                  description="管理账号连接与此渠道的自主运营。"
                  size="compact"
                  onClose={() => {
                    if (!busy) setOpen("");
                  }}
                  footer={
                    confirmDisconnect ? (
                      <>
                        <button
                          key="cancel-disconnect"
                          autoFocus
                          disabled={!!busy}
                          onClick={() => {
                            setConfirmDisconnect(null);
                            setError("");
                          }}
                        >
                          取消
                        </button>
                        <button
                          className="danger"
                          disabled={readOnly || !!busy}
                          onClick={() => disconnect(confirmDisconnect!)}
                        >
                          {busy === "disconnect" ? "正在解除…" : "确认解除绑定"}
                        </button>
                      </>
                    ) : (
                      <button disabled={!!busy} onClick={() => setOpen("")}>
                        完成
                      </button>
                    )
                  }
                >
                  <div className="social-account-detail">
                    <SocialIcon platform={p.id} connected size={32} />
                    <p>
                      {displayed.display_name ||
                        displayed.username ||
                        displayed.external_account_id ||
                        "账号已连接"}
                    </p>
                    <p className="muted small">
                      {confirmDisconnect
                        ? "此 Agent 将停止操作该账号，未提交的相关任务会取消，历史记录保留。其他 Agent 可重新登录并绑定，运营授权需重新确认。浏览器登录状态不会转移。"
                        : !isConnected
                          ? `${caption}。可重新登录，或解除绑定后交给其他 Agent。`
                          : !p.automation
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
                    {!confirmDisconnect && (
                      <div className="settings-button-row">
                        <button
                          disabled={
                            readOnly || !!busy || !p.automation || !isConnected
                          }
                          onClick={() => toggle(p.id, !enabled)}
                        >
                          {enabled ? <Pause size={13} /> : <Play size={13} />}{" "}
                          {busy === "control"
                            ? "正在更新…"
                            : enabled
                              ? "暂停此社媒"
                              : "启动此社媒"}
                        </button>
                        <button
                          className="text-button"
                          disabled={readOnly || !!busy}
                          onClick={() => {
                            setOpen("");
                            onConnect(p);
                          }}
                        >
                          重新登录
                        </button>
                        <button
                          className="text-button danger"
                          disabled={readOnly || !!busy}
                          onClick={() => {
                            setError("");
                            setConfirmDisconnect(a);
                          }}
                        >
                          解除绑定
                        </button>
                      </div>
                    )}
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
