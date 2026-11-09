"use client";

import { useState } from "react";
import { api } from "./api-client";

export type CommunityReview = {
  id: string; name: string; kind: "join" | "reply" | "promotion"; state: string;
  mid: string; quote: string; url: string; expires_at: string;
};

function Review({ aid, item, writable }: { aid: string; item: CommunityReview; writable: boolean }) {
  const [body, setBody] = useState("");
  const [permission, setPermission] = useState("");
  const [link, setLink] = useState(false);
  const [minutes, setMinutes] = useState(10);
  const [zone, setZone] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [state, setState] = useState(item.state);
  const action = { join: "入群", reply: "群内回复", promotion: "产品展示" }[item.kind];
  async function respond(accept: boolean) {
    setBusy(true);
    try {
      const saved = await api(`/agents/${aid}/community-growth/reviews/${item.id}`, { accept, body, permission, include_link: link, minutes, rule_zone: zone });
      setState(saved.state);
      setResult(accept ? "已确认并安排执行，平台回执待核对。" : "已取消这次机会。");
    } catch (e) { setResult((e as Error).message); }
    finally { setBusy(false); }
  }
  const pending = state === "pending" && item.state === "pending";
  const url = /^https:\/\/t\.me\/[a-zA-Z0-9_]+\/?$/.test(item.url) ? item.url.replace(/\/$/, "") : null;
  return <details><summary>{item.name} · {action}</summary>
    {url && <a href={url} target="_blank" rel="noreferrer">核对原群及话题</a>}
    {item.mid && <p>原消息编号 {item.mid}</p>}
    {item.quote && <p>{item.quote}</p>}
    {item.kind === "promotion" && item.mid && <p>此依据是群内邀请，请先通过 Telegram 核对发言人的管理员身份。</p>}
    {pending && writable && <form onSubmit={e => { e.preventDefault(); void respond(true); }}>
      {item.kind !== "join" && <label>自有稿件{item.kind === "promotion" ? "（请说明品牌关系）" : ""}<textarea required maxLength={700} value={body} onChange={e => setBody(e.target.value)} /></label>}
      <label>本群许可、适用板块和当前时段<input required maxLength={500} value={permission} onChange={e => setPermission(e.target.value)} /></label>
      <label>许可剩余分钟<input type="number" required min={1} max={30} value={minutes} onChange={e => setMinutes(Number(e.target.value))}/></label>
      {item.kind === "promotion" && <label>群规时区（指定星期时必填）<input value={zone} maxLength={64} placeholder="Asia/Shanghai" onChange={e => setZone(e.target.value)}/></label>}
      {item.kind === "promotion" && <label><input type="checkbox" checked={link} onChange={e => setLink(e.target.checked)} />群规允许附产品跟踪链接</label>}
      <button disabled={busy}>确认本次{action}</button>
    </form>}
    {pending && !writable && <p>执行器只读，可以跳过或等待重新核验。</p>}
    {state !== "canceled" && <button disabled={busy} onClick={() => void respond(false)}>{pending ? "跳过" : "撤回许可"}</button>}
    {result && <p role="status">{result}</p>}
  </details>;
}

export default function CommunityOperatorReviews({ aid, items, writable }: { aid: string; items: CommunityReview[]; writable: boolean }) {
  if (!items.length) return null;
  return <details><summary>群运营待办（{items.length}）</summary>{items.map(item =>
    <Review key={item.id} aid={aid} item={item} writable={writable}/>)}</details>;
}
