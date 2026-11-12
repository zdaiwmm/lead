"use client";

import { useEffect, useState } from "react";
import { ArrowUpRight, RefreshCw } from "lucide-react";
import { api } from "./api-client";
import { useScopedData } from "./analytics-summary";
import AgentResults from "./agent-results";
import DialogPanel from "./dialog-panel";
import { AssetPreview } from "./media-upload";
import SocialIcon, { socialNames } from "./social-icon";
import styles from "./growth-status.module.css";

type Row = Record<string, any>;
const states: Row = { planned: "待执行", queued: "排队中", running: "执行中", reconciling: "核对原结果", waiting_user: "需要本人处理", succeeded: "已完成", failed: "未完成", canceled: "已结束" };
const reasons: Row = { dependency_pending: "等待必要资料", account_paused: "该账号已暂停", resource_wait: "等待此账号环境空闲", work_leased: "原任务仍在执行", unknown_receipt: "正在核对原操作，结果尚未确认", login_in_progress: "等待登录完成", signed_out: "尚未登录", challenge: "需要在原窗口完成验证", ambiguous: "需要确认当前账号身份", temporarily_unavailable: "平台正在恢复账号识别", daily_token_limit: "等待当前用量额度恢复", question_limit: "这条验证路径已达到投入上限", model_call_billing_unknown: "平台正在核对原调用费用" };

function WorkEvidence({ aid, item, onClose }: { aid: string; item: Row; onClose: () => void }) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api(`/agents/${aid}/work/${item.id}/basis`, undefined, controller.signal), api(`/agents/${aid}/work/${item.id}/activity`, undefined, controller.signal)])
      .then(([basis, activity]) => { if (!controller.signal.aborted) setData({ basis, activity }); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [aid, item.id]);
  const output = data?.activity.output?.output;
  return <DialogPanel title={item.title} description="查看这项工作的依据、产物和实际回执。" onClose={onClose} footer={<button onClick={onClose}>完成</button>}>
    {error ? <p role="alert">{error}</p> : !data ? <p className="muted" role="status">正在读取原始记录…</p> : <div className={styles.evidence}>
      <p>{data.basis.experiment?.specification?.question || data.basis.completion_rule || "按原任务核对执行记录"}</p>
      {data.basis.experiment?.specification?.hypothesis && <p className="muted">待验证：{data.basis.experiment.specification.hypothesis}</p>}
      {data.activity.content?.body && <div className={styles.copy}>{data.activity.content.body}</div>}
      {(data.activity.content?.media || []).map((asset: Row) => <AssetPreview key={asset.id} aid={aid} assetId={asset.id} kind={asset.kind} />)}
      {(output?.pages || []).map((page: Row) => <p key={page.document_id}><a href={page.url} target="_blank" rel="noreferrer">{page.title || page.url}</a></p>)}
      {(data.activity.actions || []).map((action: Row) => <p key={action.id}>{states[action.state] || (action.state === "confirmed" ? "已确认平台接受" : "结果尚未确认")}{action.external_url && <> · <a href={action.external_url} target="_blank" rel="noreferrer">查看原内容</a></>}</p>)}
      {(data.activity.records || []).map((record: Row, i: number) => <p key={i}>{record.title}{record.detail && <small>{record.detail}</small>}</p>)}
      {!data.activity.content && !output?.pages?.length && !data.activity.actions?.length && !data.activity.records?.length && <p className="muted">{states[data.activity.task.state] || "等待可验证的产物"}，尚无可展示的外部成果。</p>}
    </div>}
  </DialogPanel>;
}

export default function GrowthStatus({ aid, agent, readOnly }: { aid: string; agent: Row; readOnly: boolean }) {
  const { data, error, refresh } = useScopedData(aid, `/agents/${aid}/growth`);
  const [selected, setSelected] = useState<Row | null>(null);
  const [history, setHistory] = useState(false);
  const current = data?.current_learning?.find((item: Row) => ["active", "observing"].includes(item.state));
  const work: Row[] = [...(data?.current_work || []), ...(data?.waiting || [])];
  const shown = history ? [...work, ...(data?.recent_work || [])] : work.slice(0, 4);
  return <div className={styles.overview}>
    <AgentResults agent={agent} readOnly={readOnly} growth={data?.results} />
    <section className={`workspace-section ${styles.work}`} aria-labelledby="growth-work-heading">
      <div className="section-head"><h2 id="growth-work-heading">自主工作</h2><button className="text-button" onClick={() => setHistory(!history)} aria-expanded={history}>{history ? "查看进行中" : "查看近期记录"}<ArrowUpRight size={14} /></button></div>
      {error && <p role="alert">{error} <button className="text-button" onClick={refresh}><RefreshCw size={14} />重新读取</button></p>}
      {data ? <>
        <p className={styles.objective}>{data.objective.description}</p>
        {current && <div className={styles.learning}><span className="muted small">当前验证</span><p>{current.question}</p><small>观察依据：{current.learning_signal}</small></div>}
        {shown.length ? <ul className={styles.rows}>{shown.map(item => <li key={item.id}>
          {item.platform && <SocialIcon platform={item.platform} size={16} />}
          <div><button className={styles.openWork} onClick={() => setSelected(item)}>{item.title}<ArrowUpRight size={13} /></button>
            <span>{item.platform && `${socialNames[item.platform] || item.platform} · `}{item.wait ? reasons[item.wait.reason] || (item.wait.responsibility === "owner" ? "需要本人处理账号事项" : "平台正在处理这项等待") : states[item.state] || "推进中"}</span>
          </div><small>{item.wait?.check_at ? `下次检查 ${new Date(item.wait.check_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : ""}</small>
        </li>)}</ul> : <p className={styles.empty}>{agent.desired_state === "paused" ? "项目已暂停。原操作的结果核对仍会继续。" : "当前没有待执行的工作。新资料、账号变化或真实结果会触发下一步判断。"}</p>}
        {!!data.owner_actions?.length && <p className={styles.owner}>有 {data.owner_actions.length} 项账号事项需要你处理，可从上方对应账号进入原窗口。</p>}
      </> : !error && <p className={styles.empty} role="status">正在读取工作与等待状态…</p>}
    </section>
    {selected && <WorkEvidence aid={aid} item={selected} onClose={() => setSelected(null)} />}
  </div>;
}
