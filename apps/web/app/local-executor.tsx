"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, Laptop, RefreshCw } from "lucide-react";
import { api, getAccess } from "./api-client";
import { useScopedData } from "./analytics-summary";
import styles from "./local-executor.module.css";

type Row = Record<string, any>;
const terminal = new Set(["completed", "failed", "interrupted", "execution_unknown", "canceled"]);
const labels: Record<string, string> = {
  queued: "等待本机接收", running: "执行中", waiting_input: "需要你补充",
  completed: "执行结束", failed: "执行未完成", interrupted: "已中断",
  execution_unknown: "正在核对原执行", canceled: "已取消",
};
const when = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", {
  timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
}) : "尚未上线";

function NativeQuestion({ item, disabled, respond }: {
  item: Row; disabled: boolean; respond: (item: Row, result: Row) => Promise<void>;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const native = item.params || {};
  const params = native.params || {};
  const questions: Row[] = params.questions || [];
  const method = String(native.method || "");
  const elicitation = method.includes("elicitation");
  const properties: Record<string, Row> = params.requestedSchema?.properties || {};
  const fields: Row[] = questions.length ? questions.map(field => ({ ...field, required: true })) : Object.entries(properties).map(([id, field]) => ({
    id, question: field.title || id, options: field.enum?.map((label: string) => ({ label })),
    isSecret: field.format === "password", type: field.type, required: params.requestedSchema?.required?.includes(id),
  }));
  const isApproval = method.endsWith("requestApproval");
  const permissions = method.includes("permissions/");
  const send = () => {
    if (elicitation) {
      const content = Object.fromEntries(fields.filter(field => answers[field.id] !== undefined && answers[field.id] !== "").map(field => [field.id,
        field.type === "boolean" ? answers[field.id] === "true" :
        ["integer", "number"].includes(field.type) ? Number(answers[field.id]) : answers[field.id]]));
      return respond(item, { action: "accept", content });
    }
    return respond(item, { answers: Object.fromEntries(fields.map(field => [field.id, { answers: [answers[field.id]] }])) });
  };
  return <article className={styles.question}>
    <p>{params.message || params.reason || (isApproval ? "继续这项工作需要你确认本次操作。" : "继续前，请补充以下信息。")}</p>
    {params.command && <details><summary>查看本次操作</summary><pre className={styles.command}>{Array.isArray(params.command) ? params.command.join(" ") : params.command}</pre></details>}
    {fields.length > 0 ? <form onSubmit={event => { event.preventDefault(); void send(); }}>
      {fields.map(field => <label className={styles.field} key={field.id}>
        <span>{field.question || field.header || "请补充"}</span>
        {field.options?.length ? <select required={field.required} disabled={disabled} value={answers[field.id] || ""}
          onChange={event => setAnswers(previous => ({ ...previous, [field.id]: event.target.value }))}>
          <option value="" disabled>请选择</option>
          {field.options.map((option: Row) => <option key={option.label} value={option.label}>{option.label}</option>)}
        </select> : field.type === "boolean" ? <select required={field.required} disabled={disabled} value={answers[field.id] || ""}
          onChange={event => setAnswers(previous => ({ ...previous, [field.id]: event.target.value }))}>
          <option value="" disabled>请选择</option><option value="true">是</option><option value="false">否</option>
        </select> : <input required={field.required} type={field.isSecret ? "password" : ["number", "integer"].includes(field.type) ? "number" : "text"}
          value={answers[field.id] || ""} disabled={disabled} autoComplete="off"
          onChange={event => setAnswers(previous => ({ ...previous, [field.id]: event.target.value }))} />}
      </label>)}
      <button className="primary" disabled={disabled || fields.some(field => field.required && !answers[field.id]?.trim())}>提交补充</button>
      {elicitation && <button type="button" disabled={disabled} onClick={() => void respond(item, { action: "decline" })}>暂不提供</button>}
    </form> : isApproval ? <div className={styles.actions}>
      <button className="primary" disabled={disabled} onClick={() => void respond(item,
        permissions ? { permissions: params.permissions || {}, scope: "turn" } : { decision: "accept" })}>允许本次操作</button>
      <button disabled={disabled} onClick={() => void respond(item,
        permissions ? { permissions: {}, scope: "turn" } : { decision: "decline" })}>不允许</button>
    </div> : elicitation ? <div className={styles.actions}>
      {params.url && /^https?:\/\//.test(params.url) && <a href={params.url} target="_blank" rel="noreferrer">在原服务完成连接</a>}
      <button disabled={disabled} onClick={() => void respond(item, { action: "accept" })}>已完成，继续</button>
      <button disabled={disabled} onClick={() => void respond(item, { action: "decline" })}>暂不连接</button>
    </div> : <p className="muted">请在原服务完成这项确认，或中断当前执行后调整要求。</p>}
  </article>;
}

export default function LocalExecutor({ aid, readOnly }: { aid: string; readOnly: boolean }) {
  const { data, error, refresh } = useScopedData(aid, `/agents/${aid}/executor`);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const [manage, setManage] = useState(false);
  const [invitation, setInvitation] = useState<Row | null>(null);
  const [issue, setIssue] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [extra, setExtra] = useState("");
  const [target, setTarget] = useState("");
  const access = getAccess();
  const canControl = !readOnly && (access?.role === "owner" || access?.capabilities?.includes("agents.control"));
  const canChat = !readOnly && (access?.role === "owner" || access?.capabilities?.includes("agents.chat"));
  const devices: Row[] = (data?.devices || []).filter((device: Row) => device.state === "active");
  const preferred = devices.find(device => device.id === data?.device_id);
  const active: Row[] = (data?.requests || []).filter((request: Row) => !terminal.has(request.state));
  const pending: Row[] = data?.pending_inputs || [];
  useEffect(() => {
    if (!invitation && !active.length) return;
    const timer = setInterval(() => { if (!document.hidden) refreshRef.current(); }, 5000);
    return () => clearInterval(timer);
  }, [invitation, active.length]);

  async function action(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setIssue("");
    try { await work(); refresh(); }
    catch (failure) { setIssue(failure instanceof Error ? failure.message : "操作未完成，请重试"); }
    finally { setBusy(false); }
  }
  async function connect() {
    setManage(true);
    await action(async () => {
      setInvitation(await api(`/agents/${aid}/executor/pairing`, {}));
      setCopied(false);
    });
  }
  async function command(requestId: string, kind: string, payload: Row) {
    await api(`/agents/${aid}/executor/requests/${requestId}/commands`, {
      kind, payload, command_id: crypto.randomUUID(),
    });
  }
  async function respond(item: Row, result: Row) {
    await action(() => command(item.request_id, "respond", { id: item.params.id, result }));
  }
  const expires = invitation && new Date(invitation.expires_at).getTime() <= Date.now();
  const state = !data ? "正在读取设备状态…" : data.mode !== "codex" ? "本机执行尚未启用" :
    preferred?.online ? `${preferred.name} 已连接` : "等待本机上线";
  return <section className={`workspace-section ${styles.section}`} aria-labelledby="local-executor-heading">
    <div className="section-head">
      <h2 id="local-executor-heading"><Laptop size={17} aria-hidden="true" />本机 Codex</h2>
      <button className="text-button" onClick={() => setManage(value => !value)} aria-expanded={manage}>设备与连接</button>
    </div>
    <div className={styles.summary}>
      <p role="status"><span className={`dot ${preferred?.online && data?.mode === "codex" ? "green" : ""}`} />{state}</p>
      {!devices.length && <button className="primary" disabled={busy || !canControl} onClick={() => void connect()}>连接本机 Codex</button>}
    </div>
    <p className={styles.help}>使用你本人已登录的 Codex，无需模型 API key。关闭网页后继续运行；电脑休眠或离线时等待恢复。</p>
    {(issue || error) && <p className={styles.error} role="alert">{issue || error} <button className="text-button" onClick={refresh}><RefreshCw size={13} />重新读取</button></p>}
    {manage && <div className={styles.manage}>
      <p><a href="/api/executor/download" download>下载本机连接器</a><span className="muted"> · 安装后，在本机完成一次连接。</span></p>
      <p className={styles.help}>首次下载后，在本机终端安装：<code>npm install -g ~/Downloads/lead-local-executor.tgz</code>。使用已有的 Codex 登录；每台设备分别保留自己的连接状态。</p>
      {devices.length > 0 && <ul className={styles.devices}>{devices.map(device => <li key={device.id}>
        <div><strong>{device.name}</strong><span>{device.online ? "在线" : `离线 · 最近上线 ${when(device.last_seen_at)}`}</span></div>
        <button disabled={busy || !canControl} onClick={() => void action(() => api(`/agents/${aid}/executor/devices/${device.id}/revoke`, {}))}>解除连接</button>
      </li>)}</ul>}
      <div className={styles.actions}>
        <button disabled={busy || !canControl} onClick={() => void connect()}>{devices.length ? "连接另一台设备" : "生成连接指令"}</button>
        {devices.length > 0 && <button disabled={busy || !canControl} onClick={() => void action(() => api(`/agents/${aid}/executor/mode`, {
          mode: data?.mode === "codex" ? "legacy" : "codex",
        }))}>{data?.mode === "codex" ? "切换为原执行方式" : "使用本机 Codex"}</button>}
      </div>
      {data?.mode === "legacy" && devices.length > 0 && <p className={styles.help}>当前沿用服务器原有执行配置。选择“使用本机 Codex”后，新任务由本机承接。</p>}
      {invitation && <div className={styles.pairing}>
        {expires ? <p>本次连接指令已过期，请重新生成。</p> : <>
          <p>在需要运行任务的电脑上执行以下连接指令，有效至 {when(invitation.expires_at)}。</p>
          <pre className={styles.command}>{invitation.connect_command}</pre>
          <button disabled={busy || !canControl} onClick={() => void action(async () => {
            await navigator.clipboard.writeText(invitation.connect_command); setCopied(true);
          })}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "已复制" : "复制连接指令"}</button>
        </>}
        <button className="text-button" onClick={() => setInvitation(null)}>收起指令</button>
      </div>}
      <p className={styles.help}>当前使用 GPT-6.1 Sol，中等推理。设备上的官方 Codex 管理登录和模型使用，原账号与发送记录继续保留。</p>
    </div>}
    {active.length > 0 && <div className={styles.work}>
      <div className={styles.workHeading}><strong>本机正在处理</strong><span>{active.length} 项</span></div>
      <ul className={styles.devices}>{active.slice(0, 4).map(request => <li key={request.id}>
        <div><span>{labels[request.state] || "推进中"}</span><small>{when(request.created_at)}</small>
          {request.usage?.total?.totalTokens != null && <small>本线程累计 {Number(request.usage.total.totalTokens).toLocaleString("zh-CN")} tokens，非 API 账单</small>}
        </div>
        <div className={styles.actions}>
          <button disabled={busy || !canChat} onClick={() => setTarget(target === request.id ? "" : request.id)}>追加要求</button>
          <button disabled={busy || !canControl} onClick={() => void action(() => command(request.id, "interrupt", {}))}>中断</button>
        </div>
      </li>)}</ul>
      {active.some(request => request.id === target) && <form className={styles.followup} onSubmit={event => {
        event.preventDefault(); void action(async () => { await command(target, "steer", { input: extra.trim() }); setExtra(""); setTarget(""); });
      }}>
        <label htmlFor={`executor-followup-${aid}`}>补充当前工作的要求</label>
        <textarea id={`executor-followup-${aid}`} value={extra} disabled={busy || !canChat} onChange={event => setExtra(event.target.value)} rows={2} required />
        <button className="primary" disabled={busy || !canChat || !extra.trim()}>发送补充</button>
      </form>}
    </div>}
    {pending.length > 0 && <div className={styles.pending}>
      <h3>需要你补充或确认</h3>
      {pending.map(item => <NativeQuestion key={item.event_id} item={item} disabled={busy || !canControl} respond={respond} />)}
    </div>}
    {data?.requests?.some((request: Row) => request.state === "execution_unknown") &&
      <p className={styles.help}>有执行结果尚未确认，将沿原会话核对；原发布记录不会因此重新发送。</p>}
  </section>;
}
