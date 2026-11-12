"use client";
import { useState } from "react";
import { CircleAlert, Pause, Play, Plus, ChevronRight } from "lucide-react";
import SocialIcon, { socialNames } from "./social-icon";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";
type Row = Record<string, any>;

export default function SocialAccounts({ agent, platforms, readOnly, onConnect, onComplete }: {
  agent: Row; platforms: Row[]; readOnly: boolean; onConnect: (p: Row) => void; onComplete: () => Promise<void>;
}) {
  const [open, setOpen] = useState("");
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const accounts: Row[] = agent.accounts.filter((a: Row) => a.platform !== "lab" && a.state !== "disconnected");
  const channels = Object.entries({ ...socialNames, web: "网站" }).map(([id, name]) => ({ ...platforms.find(p => p.id === id), id, name }));
  const status = (a?: Row) => !a ? "未连接" : a.state === "needs_auth" ? "需重新登录" : a.state === "restricted" ? "账号受限"
    : a.operating_intent?.desired === "paused" ? "已暂停" : a.operating_intent?.desired === "revoked" ? "运营已撤销"
    : a.operating_intent?.desired === "running" ? agent.desired_state === "paused" ? "项目已暂停" : "自动推进中" : "连接待核对";
  function show(id: string) { setOpen(id); setConfirm(false); setError(""); setPicker(false); }
  async function control(account: Row) {
    setBusy("control"); setError("");
    try {
      const kind = account.operating_intent?.desired === "running" ? "account.pause" : "account.resume";
      await api(`/agents/${agent.id}/growth/commands`, { command_id: crypto.randomUUID(), kind,
        expected_revision: account.operating_intent?.revision || 0, payload: { account_id: account.id } });
      await onComplete();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function disconnect(account: Row) {
    setBusy("disconnect"); setError("");
    try {
      await api(`/agents/${agent.id}/accounts/${account.id}/disconnect`, { expected_verified_at: account.verified_at });
      await onComplete(); setOpen(""); setConfirm(false);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  return <section id="social-accounts" className="identity-channels" aria-label="社媒账号">
    <div className="social-grid">{channels.map(p => {
      const available = accounts.filter(a => a.platform === p.id);
      const challenge = Object.values(agent.onboarding_state?.browser_challenges || {}).find((v: any) => v.platform === p.id && v.state === "waiting_user") as Row | undefined;
      const account = available.find(a => a.id === challenge?.account_id) || available.find(a => a.id === choice[p.id]) || available[0];
      const active = available.some(a => a.state === "connected" && a.operating_intent?.desired === "running" && agent.desired_state === "running");
      return <div className="social-item" key={p.id}>
        <button data-platform={p.id} data-state={account?.state || "offline"} title={`${p.name} · ${status(account)}`}
          className={`social-trigger ${account ? "is-connected" : ""}`} aria-label={`${p.name}，${status(account)}`}
          aria-haspopup="dialog" onClick={() => show(p.id)}>
          <span className="social-symbol"><SocialIcon platform={p.id} connected={!!account} size={18} />
            {account && <span className={`social-state ${active ? "active" : ""}`}>{!active && (challenge || account.state === "needs_auth" ? <CircleAlert size={10} /> : <Pause size={8} />)}</span>}
          </span>
        </button>
        {open === p.id && <DialogPanel title={confirm ? `解除 ${p.name} 绑定` : `${p.name} 账号`} size="compact"
          description="连接后自动核对身份和可运营位置，各账号独立推进。" onClose={() => { if (!busy) setOpen(""); }}
          footer={<button disabled={!!busy} onClick={() => setOpen("")}>关闭</button>}>
          {!account ? <><p>登录后会自动核对当前身份、权限和执行条件。暂未具备的能力会单独说明，其他工作继续。</p>
            <button className="primary" disabled={readOnly} onClick={() => { setOpen(""); onConnect(p); }}>连接账号</button></>
            : <div className="social-account-detail">
              {available.length > 1 && <select aria-label={`选择 ${p.name} 账号`} value={account.id} disabled={!!busy || !!challenge}
                onChange={event => { setChoice({ ...choice, [p.id]: event.target.value }); setConfirm(false); setError(""); }}>
                {available.map(a => <option key={a.id} value={a.id}>{a.display_name || a.external_account_id || a.connection_origin || "已连接账号"}</option>)}
              </select>}
              <strong>{account.display_name || account.external_account_id || account.connection_origin || p.name}</strong>
              <p>{confirm ? "解除后停止操作此账号，保留历史和原操作回执。其他账号继续运行。" : challenge ? "平台要求本人验证，请打开原窗口完成。" : status(account)}</p>
              {!confirm && account.state === "connected" && <p className="small">{account.operational_ready ? "已有目标与格式的核验记录，提交前仍会核对当前条件。" : account.discovery?.output?.missing_conditions?.length ? "账号已连接，平台尚未核实可执行位置或接入能力。此项由产品继续处理，其他工作可独立推进。" : account.discovery?.state === "failed" ? "可执行位置核对暂未完成，平台正在处理该项能力故障。" : account.discovery?.state === "succeeded" ? "位置核对已完成，首次业务操作前仍需核对格式和当前执行条件。" : "正在核对可执行位置与能力；连接成功不会被当作发布成功。"}</p>}
              {error && <p className="field-error" role="alert">{error}</p>}
              <div className="settings-button-row">{confirm ? <>
                <button disabled={!!busy} onClick={() => setConfirm(false)}>取消</button>
                <button className="danger" disabled={readOnly || !!busy} onClick={() => disconnect(account)}>{busy ? "正在解除…" : "确认解除绑定"}</button>
              </> : <>
                <button disabled={readOnly || !!busy || !account.operating_intent || !!challenge} onClick={() => control(account)}>
                  {account.operating_intent?.desired === "running" ? <Pause size={13} /> : <Play size={13} />}
                  {busy === "control" ? "正在更新…" : account.operating_intent?.desired === "running" ? "暂停此账号" : "恢复此账号"}
                </button>
                <button className="text-button" disabled={readOnly || !!busy} onClick={() => { setOpen(""); onConnect({ ...p, account_id: account.id, connection_origin: account.connection_origin }); }}>{challenge ? "打开原窗口验证" : "重新登录"}</button>
                <button className="text-button danger" disabled={readOnly || !!busy} onClick={() => setConfirm(true)}>解除绑定</button>
              </>}</div>
              {!confirm && <button className="text-button" disabled={readOnly || !!busy} onClick={() => { setOpen(""); onConnect({ ...p, new_account: true }); }}>连接另一个账号</button>}
            </div>}
        </DialogPanel>}
      </div>;
    })}<button className="channel-add icon" aria-label="连接渠道账号" title="连接渠道账号" aria-haspopup="dialog" onClick={() => setPicker(true)}><Plus size={16} /></button></div>
    {picker && <DialogPanel title="连接渠道账号" size="compact" onClose={() => setPicker(false)}><div className="channel-picker">{channels.map(p => <button key={p.id} onClick={() => show(p.id)}>
      <SocialIcon platform={p.id} size={18} /><span>{p.name}</span><small>{status(accounts.find(a => a.platform === p.id))}</small><ChevronRight size={14} />
    </button>)}</div></DialogPanel>}
  </section>;
}
