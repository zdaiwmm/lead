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
import { api, enableManagedScope } from "./api-client";
import CadenceStatus from "./execution-cadence";
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
  const [accountChoice, setAccountChoice] = useState<Record<string, string>>({});
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
  async function authorizeManaged(account: Row) {
    setBusy("managed-scope");
    setError("");
    try {
      await enableManagedScope(agent.id, account, agent.control_version);
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
          const verification = Object.values(agent.onboarding_state?.browser_challenges || {}).find((v: any) => v.state === "waiting_user" && v.platform === p.id) as Row | undefined;
          const needsVerification = Boolean(verification);
          const available = accounts.filter((row: Row) => row.platform === p.id && ["connected", "needs_auth", "restricted"].includes(row.state));
          const a = (needsVerification ? available.find((row: Row) => row.id === verification?.account_id) : available.find((row: Row) => row.id === accountChoice[p.id])) || available[0];
          const isConnected = a?.state === "connected";
          const enabled =
            isConnected &&
            !needsVerification &&
            a.automation_enabled &&
            agent.desired_state === "running";
          const enabledCount = available.filter((account: Row) => account.state === "connected" && account.automation_enabled && agent.desired_state === "running").length;
          const platformEnabled = enabledCount > 0;
          const hasConnected = available.some((account: Row) => account.state === "connected");
          const canOperate = !!p.automation || !!a?.operational_ready || !!a?.editorial_preparation || !!a?.managed_scope_authorized || !!a?.member_scope_authorized;
          const waiting = a?.channel_waits?.find((wait: Row) => ["waiting_time", "waiting_event", "receipt_only", "unverified"].includes(wait.execution_cadence?.state));
          const waitLabel: Record<string, string> = { waiting_event: "运营已启用，外部操作等待条件变化", waiting_time: "运营已启用，等待已记录的期限", receipt_only: "运营已启用，只核对原操作结果", unverified: "运营已启用，当前执行条件待核实" };
          const caption = needsVerification ? "等待真人验证" : !a
            ? "连接"
            : a.state === "needs_auth"
              ? "登录失效"
              : a.state === "restricted"
                ? "账号受限"
                : enabled
                  ? "运营已启用"
                  : p.id === "facebook" && a.managed_scope_available && !a.managed_scope_authorized && !a.operational_ready ? "已连接，待授权" : "已暂停";
          const triggerCaption = available.length > 1 ? `${enabledCount}/${available.length} 个账号已启用` : caption;
          const displayed = confirmDisconnect || a;
          return (
            <div className="social-item" key={p.id}>
              <button
                data-platform={p.id}
                data-state={a?.state || "offline"}
                title={`${p.name} · ${triggerCaption}`}
                className={`social-trigger ${a ? "is-connected" : ""}`}
                aria-label={`${p.name}，${a ? triggerCaption : "连接账号"}`}
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
                    connected={hasConnected}
                    size={18}
                  />
                  {a && (
                    <span className={`social-state ${platformEnabled ? "active" : ""}`}>
                      {!platformEnabled &&
                        (needsVerification || a.state === "needs_auth" ? (
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
                    {!confirmDisconnect && available.length > 1 && <label className="small">当前账号
                      <select aria-label={`选择 ${p.name} 账号`} value={a.id} disabled={!!busy || needsVerification} onChange={(event) => { setError(""); setAccountChoice({ ...accountChoice, [p.id]: event.target.value }); }}>
                        {available.map((account: Row) => <option key={account.id} value={account.id}>{account.display_name || account.external_account_id || "已连接账号"}</option>)}
                      </select>
                    </label>}
                    <SocialIcon platform={p.id} connected size={32} />
                    <p>
                      {displayed.display_name ||
                        displayed.username ||
                        displayed.external_account_id ||
                        (needsVerification ? "等待完成验证" : "账号已连接")}
                    </p>
                    <p className="muted small">
                      {confirmDisconnect
                        ? "此 Agent 将停止操作该账号，未提交的相关任务会取消，历史记录保留。其他 Agent 可重新登录并绑定，运营授权需重新确认。浏览器登录状态不会转移。"
                        : needsVerification ? "平台要求真人验证，原页面和任务已保留。完成验证后可继续原任务。" : !isConnected
                          ? `${caption}。可重新登录，或解除绑定后交给其他 Agent。`
                          : a.managed_scope_available && !a.managed_scope_authorized && !a.operational_ready && p.id === "facebook"
                            ? "账号已连接，可单独授权管理目标的自动核验与文字运营。"
                            : !canOperate
                            ? "已支持登录，自主运营尚未开放"
                            : enabled
                              ? waiting
                                ? waitLabel[waiting.execution_cadence.state]
                                : a.operational_ready
                                ? "目标和格式已有核验记录，执行前继续核对当前权限。"
                                : a.editorial_preparation
                                  ? a.editorial_review
                                    ? `已有社区讨论记录 · 近 7 天已确认发布 ${a.editorial_review.confirmed_posts} 条，${a.editorial_review.links} 个跟踪链接记录了 ${a.editorial_review.clicks} 次点击`
                                    : "已有社区讨论筹备入口，等待首次工作记录"
                                  : agent.health_state === "needs_attention"
                                  ? "筹备需要处理，请查看工作记录"
                                  : a.target_id
                                    ? "已有目标记录，当前执行条件待核实"
                                    : "运营已启用，等待可核实的工作记录"
                              : "启动后，Agent 将自动完成运营准备"}
                    </p>
                    {isConnected && !confirmDisconnect && ["facebook", "reddit"].includes(p.id) && <div className="small">
                      {a.managed_scope_authorized ? <p>此账号已获管理目标的文字运营授权。来源、目标和发布条件仍会分别核验。</p>
                        : a.member_scope_authorized ? <p>仅按已授权的社区参与范围投稿，发前仍核验社区规则和平台条件。</p>
                        : a.managed_scope_available ? <>
                          <p>可授权此账号核验并运营有管理权限的目标，仅发布文字，普通成员发帖不在此范围。来源许可和平台条件仍需满足。</p>
                          <button disabled={readOnly || !!busy} onClick={() => authorizeManaged(a)}>{busy === "managed-scope" ? "正在授权并登记…" : "授权此账号运营管理目标"}</button>
                        </> : <p>此账号的托管运营条件待核实，连接成功不代表已具备发布条件。</p>}
                      {a.channel_waits?.map((wait: Row, index: number) => <div key={index}><p>{wait.lane === "research" ? "机会研究" : "内容发布"}</p><CadenceStatus value={wait.execution_cadence} zone={agent.onboarding_state?.operating_timezone || "UTC"} /></div>)}
                    </div>}
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
                            (!canOperate && !platformEnabled) ||
                            !hasConnected || needsVerification
                          }
                          onClick={() => toggle(p.id, !platformEnabled)}
                        >
                          {platformEnabled ? <Pause size={13} /> : <Play size={13} />}{" "}
                          {busy === "control"
                            ? "正在更新…"
                            : platformEnabled
                              ? (available.length > 1 ? "暂停此平台全部账号" : "暂停此社媒")
                              : (available.length > 1 ? "启动此平台全部账号" : "激活此渠道")}
                        </button>
                        <button
                          className="text-button"
                          disabled={readOnly || !!busy}
                          onClick={() => {
                            setOpen("");
                            onConnect({ ...p, account_id: verification?.account_id || a.id });
                          }}
                        >
                          {needsVerification ? "打开原窗口验证" : "重新登录"}
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
