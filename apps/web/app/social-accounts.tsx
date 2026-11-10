"use client";
import { useState } from "react";
import {
  CircleAlert,
  LockKeyhole,
  Pause,
  Play,
  Plus,
  ChevronRight,
} from "lucide-react";
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
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState("");
  const [confirmDisconnect, setConfirmDisconnect] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const accounts = agent.accounts.filter((a: Row) => a.platform !== "lab");
  const unique: Row[] = Object.keys(socialNames).map((id) => ({
    id,
    name: socialNames[id],
    ...platforms.find((p) => p.id === id),
  }));
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
      className="identity-channels"
      aria-label="社媒账号"
    >
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
          const canOperate = !!p.automation || !!a?.operational_ready || !!a?.editorial_preparation;
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
                data-state={a?.state || "offline"}
                title={`${p.name} · ${caption}`}
                className={`social-trigger ${a ? "is-connected" : ""}`}
                aria-label={`${p.name}，${a ? caption : "连接账号"}`}
                aria-haspopup="dialog"
                onClick={() => {
                  setError("");
                  setConfirmDisconnect(null);
                  setOpen(p.id);
                }}
              >
                <span className="social-symbol">
                  <SocialIcon
                    platform={p.id}
                    connected={isConnected}
                    size={18}
                  />
                  {a && (
                    <span className={`social-state ${enabled ? "active" : ""}`}>
                      {!enabled &&
                        (a.state === "needs_auth" ? (
                          <CircleAlert size={10} />
                        ) : a.state === "restricted" ? (
                          <LockKeyhole size={9} />
                        ) : (
                          <Pause size={8} />
                        ))}
                    </span>
                  )}
                </span>
              </button>
              {!a && open === p.id && (
                <DialogPanel
                  title={`激活 ${p.name}`}
                  size="compact"
                  onClose={() => setOpen("")}
                  footer={
                    <button
                      className="primary"
                      disabled={readOnly}
                      onClick={() => {
                        setOpen("");
                        onConnect({ ...p, activate: !!canOperate });
                      }}
                    >
                      {p.automation ? "连接并激活" : "连接账号"}
                    </button>
                  }
                >
                  <p>
                    {readOnly
                      ? "当前为只读，请由有管理权限的成员连接账号。"
                      : p.automation
                        ? "登录并验证此渠道账号后，启动该渠道运营。"
                        : "可先绑定账号，自主运营尚未开放。"}
                  </p>
                </DialogPanel>
              )}
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
                          : !canOperate
                            ? "已支持登录，自主运营尚未开放"
                            : enabled
                              ? a.operational_ready
                                ? "正在自主运营"
                                : a.editorial_preparation
                                  ? a.editorial_review
                                    ? `正在筹备社区讨论 · 近 7 天已确认发布 ${a.editorial_review.confirmed_posts} 条，${a.editorial_review.links} 个跟踪链接记录了 ${a.editorial_review.clicks} 次点击`
                                    : "正在筹备社区讨论，等待首次工作记录"
                                  : agent.health_state === "needs_attention"
                                  ? "筹备需要处理，请查看工作记录"
                                  : a.target_id
                                    ? "Agent 正在验证运营环境"
                                    : "Agent 正在准备运营环境"
                              : "启动后，Agent 将自动完成运营准备"}
                    </p>
                    {isConnected && !confirmDisconnect && a?.editorial_review && (
                      <div className="small">
                        {a.editorial_review.editorial_brief && (
                          <details>
                            <summary>查看社区讨论草稿</summary>
                            <p>{a.editorial_review.editorial_brief.title}</p>
                            <p>{a.editorial_review.editorial_brief.body}</p>
                          </details>
                        )}
                        {enabled && <p>下次效果复核 {new Date(a.editorial_review.next_check_at).toLocaleString()}</p>}
                      </div>
                    )}
                    {error && (
                      <p className="field-error" role="alert">
                        {error}
                      </p>
                    )}
                    {!confirmDisconnect && (
                      <div className="settings-button-row">
                        <button
                          disabled={
                            readOnly ||
                            !!busy ||
                            (!canOperate && !enabled) ||
                            !isConnected
                          }
                          onClick={() => toggle(p.id, !enabled)}
                        >
                          {enabled ? <Pause size={13} /> : <Play size={13} />}{" "}
                          {busy === "control"
                            ? "正在更新…"
                            : enabled
                              ? "暂停此社媒"
                              : "激活此渠道"}
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
        <button
          className="channel-add icon"
          aria-label="激活渠道"
          title="激活渠道"
          aria-haspopup="dialog"
          onClick={() => setPicker(true)}
        >
          <Plus size={16} />
        </button>
      </div>
      {picker && (
        <DialogPanel
          title="激活渠道"
          size="compact"
          onClose={() => setPicker(false)}
        >
          <div className="channel-picker">
            {unique.map((p) => {
              const account = accounts.find(
                (a: Row) =>
                  a.platform === p.id &&
                  ["connected", "needs_auth", "restricted"].includes(a.state),
              );
              const active =
                account?.state === "connected" &&
                account.automation_enabled &&
                agent.desired_state === "running";
              return (
                <button
                  key={p.id}
                  onClick={() => {
                    setPicker(false);
                    setConfirmDisconnect(null);
                    setError("");
                    setOpen(p.id);
                  }}
                >
                  <SocialIcon platform={p.id} size={18} />
                  <span>{p.name}</span>
                  <small>
                    {active
                      ? "已激活"
                      : !account
                        ? "未连接"
                        : account.state !== "connected"
                          ? "需重新登录"
                          : p.automation || account?.operational_ready || account?.editorial_preparation
                            ? "待激活"
                            : "仅支持连接"}
                  </small>
                  <ChevronRight size={14} />
                </button>
              );
            })}
          </div>
        </DialogPanel>
      )}
    </section>
  );
}
