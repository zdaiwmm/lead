"use client";

import { useState } from "react";
import { ArrowRight, CircleDot, Hand, History } from "lucide-react";
import DialogPanel from "./dialog-panel";
import SocialIcon from "./social-icon";
import styles from "./telegram-group-card.module.css";

type GroupRecord = { name: string; at: string; detail?: string };
type Totals = { count: number; records: GroupRecord[] };
type Board = {
  metrics: Record<string, Totals>;
  participation?: { scope: string[]; policy_reported: boolean; next_eligible_at: string | null; reason: string | null };
  resident?: {
    heartbeat_at: string | null; next_check_at: string | null;
    native_search_after: string | null; write_hold: string | null;
    discovery: {phase: string | null; native_cursor: number | null; query_count: number | null;
      source_count: number | null; checkpoint_at: string | null} | null;
  } | null;
  autonomy?: { last_progress_at: string | null; progress_label: string; blocked_targets: number; blocked_accounts: number; next_check_at: string | null };
  execution?: {
    totals: Record<string, number>;
    outcomes?: Record<string, number>;
    waiting: { name: string; state: string; checked_at: string | null; next_at: string | null; check_state: string | null; reason: string | null }[];
    gates: { rule: string; reason: string; count: number }[];
    timings: { phase: string; samples: number; average_seconds: number; p95_seconds: number; failures: number }[];
  };
  progress?: {
    confirmed_at: string | null;
    oldest_due_at: string | null;
    arrived_at: string | null;
    delayed: boolean;
  };
  current: string;
  next: string;
  constraint: string | null;
  owner: string | null;
  at: string;
};
const metrics = [
  ["candidate", "进入候选", "按群入口去重，统计今日首次入选的讨论群。"],
  ["joined", "确认加入", "统计今日取得加入回执的不同群。"],
  ["pending", "待审核", "平台已明确确认申请提交，正在等待管理员批准的群。"],
  ["join_unknown", "入群待核对", "已发起加入操作，平台结果尚未确认的群。"],
  ["exit", "退出", "统计今日取得主动退出回执的不同群。"],
  ["interaction", "互动消息", "只统计有发送回执的普通群消息。"],
  ["inbox_reply", "私聊回复", "统计今日首次取得发送回执的私聊回复。"],
  ["promotion", "发布软文", "只统计有发送回执的群内产品介绍，编辑原帖不会重复计数。"],
] as const;

export default function TelegramGroupCard({ board, zone, history }: { board: Board; zone: string; history: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const metric = metrics.find(([key]) => key === selected);
  const records = metric && board.metrics[metric[0]];
  const clock = new Intl.DateTimeFormat("zh-CN", { timeZone: zone, month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit",hour12:false });
  const deviceClock = new Intl.DateTimeFormat("zh-CN", {timeZone: zone, month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  const resident = board.resident;
  const search = resident?.discovery;
  const phases: Record<string, string> = {native_collect: "原生搜索", collect: "读取公开来源",
    hybrid_collect: "检索公开来源", assess: "评估候选群", plan_queries: "规划查询"};
  function localTime(value: string | null | undefined) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? deviceClock.format(date) : null;
  }
  return <div className={styles.deck}><article className={styles.card}>
    <header className={styles.head}><div className={styles.identity}><span className={styles.icon}><SocialIcon platform="telegram" connected size={20}/></span><div><small>Telegram</small><h3>找群获客</h3></div></div>
      <button className="text-button" onClick={history}><History size={14}/>工作记录</button></header>
    <div className={styles.metrics}>{metrics.map(([key,label]) => <button key={key} aria-haspopup="dialog" onClick={()=>setSelected(key)}>
      <span>{label}</span><strong>{(board.metrics[key]?.count??0).toLocaleString("zh-CN")}<small>{key==="interaction"||key==="inbox_reply"||key==="promotion"?" 条":" 个"}</small></strong>
    </button>)}</div>
    {board.progress && <section aria-label="社区进展">
      <div className={styles.flow}>
        <div><small>最近确认发帖或回复</small><p>{board.progress.confirmed_at ? clock.format(new Date(board.progress.confirmed_at)) : "暂无回执"}</p></div>
        <div><small>最早到期未核对</small><p>{board.progress.oldest_due_at ? clock.format(new Date(board.progress.oldest_due_at)) : "暂无到期回执"}</p></div>
        <div><small>最近有效到站</small><p>{board.progress.arrived_at ? clock.format(new Date(board.progress.arrived_at)) : "暂无到站记录"}</p></div>
      </div>
      {board.progress.delayed && board.current !== "运营未启动或已暂停" &&
        <p role="status" className={styles.note}>已有可处理的社区任务，最近连续三轮安排了研究或等待。</p>}
    </section>}
    {board.autonomy && <section aria-label="有效推进与恢复">
      <p className={styles.note}><b>最近有效进展</b><span>{board.autonomy.last_progress_at ? `${board.autonomy.progress_label} · ${clock.format(new Date(board.autonomy.last_progress_at))}` : "暂无已验证进展"}</span></p>
      {!!(board.autonomy.blocked_targets + board.autonomy.blocked_accounts) && <p className={styles.note} role="status">
        {board.autonomy.blocked_targets} 个群、{board.autonomy.blocked_accounts} 个账号等待恢复，其他独立工作继续。
        {board.autonomy.next_check_at && <> 下次只读检查：{clock.format(new Date(board.autonomy.next_check_at))}。</>}
      </p>}
    </section>}
    {resident && <section aria-label="设备执行进度">
      {search && <p className={styles.note}><b>研究检查点</b><span>{phases[search.phase || ""] || "找群研究"}
        {search.native_cursor != null && <> · 原生查询已处理 {search.native_cursor}{search.query_count != null && `/${search.query_count}`} 项</>}
        {search.source_count != null && <> · 已保存 {search.source_count} 个来源</>}
        {localTime(search.checkpoint_at) && <> · {localTime(search.checkpoint_at)}</>}
      </span></p>}
      {board.current !== "运营未启动或已暂停" && <p className={styles.note}><b>执行器心跳</b><span>{localTime(resident.heartbeat_at) || "尚未收到"}
        {localTime(resident.next_check_at) && <> · 调度复查 {localTime(resident.next_check_at)}</>}
        {localTime(resident.native_search_after) && <> · 最早可搜索 {localTime(resident.native_search_after)}</>}
      </span></p>}
      {resident.write_hold && <p className={styles.note}><b>账号写入</b><span>{resident.write_hold}</span></p>}
    </section>}
    <div className={styles.note}><b>最近卡点</b><span>{board.constraint || "今日暂无已记录的受阻原因。"}</span></div>
    {board.participation && <details className={styles.execution}>
      <summary>当前参与范围</summary>
      <ul className={styles.list}>{board.participation.scope.map(item => <li key={item}>{item}</li>)}</ul>
      {localTime(board.participation.next_eligible_at) && <p className={styles.note}>操作额度最早恢复：{localTime(board.participation.next_eligible_at)}。到期后仍会重新核对群规与账号状态。</p>}
    </details>}
    <div className={styles.flow}><div><small><CircleDot/>当前行动</small><p>{board.current}</p></div><div><small><ArrowRight/>下一步行动</small><p>{board.next}</p></div><div><small><Hand/>需要你协助</small><p>{board.owner || "暂无待办"}</p></div></div>
    {board.execution && <details className={styles.execution}>
      <summary>执行进度与等待原因</summary>
      {board.execution.outcomes && <p className={styles.note}>
        今日结束的评估：跳过 {board.execution.outcomes.skipped || 0} 次，受阻 {board.execution.outcomes.blocked || 0} 次，
        暂缓 {board.execution.outcomes.deferred || 0} 次，无变化 {board.execution.outcomes.no_change || 0} 次。以上均不计为确认发言或获客成果。
      </p>}
      <dl className={styles.counts}>{[
        ["assessed","已评估群"],["joins_started","入群已发起"],["restricted","已加入但不可发言"],
        ["drafts_accepted","文案通过"],["drafts_repaired","尝试修正文案"],["drafts_rejected","文案校验未通过"],
        ["sends_failed","平台确认发送失败"],["tasks_failed","今日失败任务"],
      ].map(([key,label])=><div key={key}><dt>{label}</dt><dd>{board.execution!.totals[key]??0}</dd></div>)}</dl>
      {!!board.execution.waiting.length && <ul className={styles.list}>{board.execution.waiting.map((item,index)=>
        <li key={index}><strong>{item.name}</strong><p>{item.state}</p>
          <p>最近核对 {item.checked_at ? clock.format(new Date(item.checked_at)) : "等待首次核对"} ·
            下次检查 {item.next_at ? clock.format(new Date(item.next_at)) : "等待排期"}</p>
          {item.reason && <p>{item.check_state==="read_failed" ? "页面读取失败，已安排复查。" : item.reason}</p>}
        </li>)}</ul>}
      {!!board.execution.gates.length && <ul className={styles.list} aria-label="今日跳过和受阻原因">
        {board.execution.gates.map((item,index)=><li key={index}>{item.reason} · {item.count} 次</li>)}
      </ul>}
      {!!board.execution.timings.length && <div className={styles.timings}>
        <table><caption>今日步骤耗时</caption><thead><tr><th>步骤</th><th>次数</th><th>平均</th><th>95% 完成时间</th><th>失败</th></tr></thead>
          <tbody>{board.execution.timings.map(item=><tr key={item.phase}>
            <td>{({
              model:"内容判断与修正",browser_receipt:"核对回执",browser_observe:"读取群资料",
              browser_join:"发起加入",browser_reply:"发送消息",browser_scan_pending:"检查群列表",
              browser_profile_bio:"维护账号资料",browser_leave_restricted:"退出受限群",
            } as Record<string,string>)[item.phase] || "其他执行步骤"}</td>
            <td>{item.samples}</td><td>{item.average_seconds} 秒</td><td>{item.p95_seconds} 秒</td><td>{item.failures}</td>
          </tr>)}</tbody></table>
      </div>}
    </details>}
    {metric && <DialogPanel title={metric[1]} onClose={()=>setSelected(null)}><p>{metric[2]}</p><p className="muted small">截至 {clock.format(new Date(board.at))} · {zone} · 已确认记录 {records?.count??0} 项</p>
      {!!records?.records.length && <ul className={styles.list}>{records.records.map((r,i)=><li key={i}><strong>{r.name}</strong><time>{clock.format(new Date(r.at))}</time>{r.detail&&<p>{r.detail}</p>}</li>)}</ul>}
      {records && records.count>records.records.length && <p>展示最近 {records.records.length} 项。</p>}
    </DialogPanel>}
  </article></div>;
}
