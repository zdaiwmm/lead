"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  ArrowUpRight,
  ChevronRight,
  ChartNoAxesCombined,
  CheckCircle2,
  Compass,
  Circle,
  Clock3,
  FileText,
  RefreshCw,
  Search,
  Send,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { api } from "./api-client";
import DialogPanel from "./dialog-panel";
import SocialIcon, { socialNames } from "./social-icon";
import AgentAvatar from "./agent-avatar";
import { ActivityHistory } from "./activity-panel";
import TelegramGroupCard from "./telegram-group-card";
import PlanWorkRecords from "./plan-work-records";
import {
  Work,
  taskChains,
  laneFor,
  stages,
  statusLabel,
  synthetic,
  timestamp,
  visibleWork,
} from "./operating-plan-model";
import { ChainRows, ChainTools, TopicBasis } from "./plan-task-chains";
import styles from "./operating-plans.module.css";

type Row = Record<string, any>;
const stageIcons = {
  audience: Search,
  content: FileText,
  distribution: Send,
  observation: ChartNoAxesCombined,
};
const focusNames: Record<string, string> = {
  audience: "验证受众需求",
  content: "优先准备可用内容",
  distribution: "优先分发现有内容，观察真实引流",
  observation: "观察真实效果并调整方向",
};
const measured = (value: number | null | undefined) =>
  value == null ? "未知" : value.toLocaleString("zh-CN");
function Stage({ stage }: { stage: string }) {
  const Icon = stageIcons[stage as keyof typeof stageIcons] || Search;
  return (
    <span className={styles.stage} data-stage={stage}>
      <Icon size={13} aria-hidden="true" />
      {stages[stage] || "业务工作"}
    </span>
  );
}
function ExecutionState({
  item,
  animate = true,
}: {
  item: Work;
  animate?: boolean;
}) {
  return (
    <span
      className={styles.executionState}
      data-state={item.state}
      data-animate={animate || undefined}
    >
      {item.state === "running" ? (
        <span className={styles.bars} aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      ) : item.state === "reconciling" ? (
        <span className={styles.spinner} aria-hidden="true" />
      ) : item.state === "done" ? (
        <CheckCircle2 size={13} aria-hidden="true" />
      ) : item.state === "ready" ? (
        <Circle size={13} aria-hidden="true" />
      ) : item.state === "blocked" ? (
        <TriangleAlert size={13} aria-hidden="true" />
      ) : item.state === "canceled" ? (
        <XCircle size={13} aria-hidden="true" />
      ) : (
        <Clock3 size={13} aria-hidden="true" />
      )}
      {statusLabel(item)}
    </span>
  );
}
function Channel({ item, agent }: { item: Work; agent: Row }) {
  const name = item.platform
    ? socialNames[item.platform] || item.platform
    : "Agent 系统";
  return (
    <span className={styles.channel} role="img" aria-label={name} title={name}>
      {item.platform ? (
        <SocialIcon platform={item.platform} size={14} />
      ) : (
        <AgentAvatar agent={agent} size={16} />
      )}
    </span>
  );
}
export default function OperatingPlans({
  aid,
  agent,
  readOnly,
}: {
  aid: string;
  agent: Row;
  readOnly: boolean;
}) {
  const [today, setToday] = useState<Row | null>(null);
  const [details, setDetails] = useState<Row | null>(null);
  const [detailError, setDetailError] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [dialog, setDialog] = useState<
    "direction" | "dependencies" | "history" | null
  >(null);
  const [selected, setSelected] = useState<Work | null>(null);
  const [platform, setPlatform] = useState("");
  const [lane, setLane] = useState("");
  const [historical, setHistorical] = useState<{
    until?: string;
    zone?: string;
  }>({});
  function openStage(item: Work, until?: string, zone?: string) {
    setHistorical({ until, zone });
    setSelected(item);
  }
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);
  const [focus, setFocus] = useState("distribution");
  const [event, setEvent] = useState("");
  const [criteria, setCriteria] = useState({first_definition: "", cost_ceiling: "", quality: ""});
  const [revision, setRevision] = useState(0);
  const activeRead = useRef<AbortController | null>(null);
  const detailRead = useRef<AbortController | null>(null);
  const lastRead = useRef(0);
  const boardId = useId();
  const detailDialog = dialog === "direction" || dialog === "dependencies";
  const data = detailDialog && details && today?.state === "group_summary"
    ? {...details, telegram_group_board: today.telegram_group_board} : today;
  const load = useCallback(async () => {
    if (activeRead.current) return;
    const controller = new AbortController();
    activeRead.current = controller;
    const timeout = window.setTimeout(() => {
      controller.abort();
      if (activeRead.current === controller) setError("读取今日执行超时，将自动重试。");
    }, 90000);
    lastRead.current = Date.now();
    setLoading(true);
    try {
      const next = await api(`/agents/${aid}/plans?today_only=true`, undefined, controller.signal);
      if (!controller.signal.aborted) {
        setToday(next);
        setError("");
      }
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      window.clearTimeout(timeout);
      if (activeRead.current === controller) {
        activeRead.current = null;
        setLoading(false);
      }
    }
  }, [aid]);
  useEffect(() => {
    setToday(null);
    setDetails(null);
    setError("");
    setSelected(null);
    setDialog(null);
    setPlatform("");
    setLane("");
    setHistorical({});
    void load();
    const timer = setInterval(() => void load(), 15000);
    let update: ReturnType<typeof setTimeout> | undefined;
    const changed = () => {
      clearTimeout(update);
      update = setTimeout(() => void load(), Math.max(500, 10000 - (Date.now() - lastRead.current)));
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("lead:updated", changed);
    return () => {
      activeRead.current?.abort();
      activeRead.current = null;
      detailRead.current?.abort();
      clearInterval(timer);
      clearTimeout(update);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("lead:updated", changed);
    };
  }, [aid, load]);
  function resetPreferences(source = data) {
    setFocus(source?.week?.focus || "distribution");
    setEvent(source?.mode?.objective_event || "");
    const strategy = source?.strategy || {};
    setCriteria({first_definition: strategy.first_definition || "", cost_ceiling: strategy.cost_ceiling || "", quality: strategy.quality_policy || ""});
    setRevision(source?.preferences_revision || 0);
    setSaveError("");
    setSaved(false);
  }
  async function openDetails(which: "direction" | "dependencies") {
    setDialog(which);
    setDetailError("");
    detailRead.current?.abort();
    if (today?.state !== "group_summary") {
      if (which === "direction") resetPreferences(today);
      return;
    }
    setDetails(null);
    const controller = new AbortController();
    detailRead.current = controller;
    try {
      const next = await api(`/agents/${aid}/plans`, undefined, controller.signal);
      if (!controller.signal.aborted) {
        setDetails(next);
        if (which === "direction") resetPreferences(next);
      }
    } catch (e) {
      if (!controller.signal.aborted) setDetailError((e as Error).message);
    }
  }
  function closeDetails() {
    detailRead.current?.abort();
    setDialog(null);
  }
  async function savePreferences(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveError("");
    setSaved(false);
    try {
      await api(`/agents/${aid}/plans/preferences`, {
        expected_revision: revision,
        focus,
        objective_event: event || null,
        acquisition_criteria: criteria,
      });
      setRevision((v) => v + 1);
      if (today?.state === "group_summary") await openDetails("direction");
      else await load();
      setSaved(true);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  const day = data?.day;
  const zone = data?.timezone || "Asia/Shanghai";
  const paused = day?.current?.state === "paused";
  const all: Work[] = (day?.queue || []).map(visibleWork);
  const chains = taskChains(all, { period: day?.period, zone }).filter(
    ({ current }) =>
      (!platform || (current.platform || "system") === platform) &&
      (!lane || laneFor(current) === lane),
  );
  const selectedWork =
    selected && !historical.until
      ? all.find((item) => item.id === selected.id) || selected
      : selected;
  const detailZone = historical.zone || zone;
  const sourceWork = selectedWork?.stage === "distribution" && synthetic(selectedWork);
  const recordsTaskId = selectedWork
    ? selectedWork.activity_task_id ||
      (sourceWork
        ? selectedWork.source_task_id || selectedWork.evidence_task_id
        : selectedWork.evidence_task_id)
    : null;
  const reportedEvents: Row[] = data?.mode?.arrival_available
    ? data.mode.events.filter(
        (item: Row) => item.ready && item.observed_count_30d > 0,
      )
    : [];
  const activity = day?.current;
  const executing = all.find(item=>item.state==="running") || all.find(item=>item.state==="reconciling");
  const working=activity?.state==="running";
  const reconciling=working && executing?.state==="reconciling";
  const activityText=paused?"已暂停运营":working?(executing?.task_title||activity.title):activity?.title;
  const direction = data?.week?.objective || "方向待定";
  return (
    <section
      className={`workspace-section ${styles.panel} ${data?.telegram_group_board ? "group-execution" : ""}`}
      aria-label="运营方向与今日执行"
      aria-busy={loading && !data}
    >
      {error && (
        <div className={styles.feedback} role="alert">
          <span>
            {data ? "更新失败，保留上次结果。" : "计划暂时无法读取。"}
            {error}
          </span>
          <button disabled={loading} onClick={() => void load()}>
            重试
          </button>
        </div>
      )}
      {!data && !error && (
        <div
          className={styles.skeleton}
          role="status"
          aria-label="正在读取运营计划"
        />
      )}
      {data?.state === "business_required" && (
        <p className={styles.muted}>请先填写业务介绍</p>
      )}
      {day && (
        <>
          <div className={`section-head ${styles.heading}`}>
            <div className={styles.titleLine}>
              <h2>今日执行</h2>
              <button type="button" className={styles.directionLink} title={direction}
                aria-label={`查看方向：${direction}`} aria-haspopup="dialog"
                onClick={()=>void openDetails("direction")}>
                <Compass size={13} aria-hidden="true"/>获客策略<ChevronRight size={12} aria-hidden="true"/>
              </button>
            </div>
            {data.telegram_group_board ? <small>最近同步 {new Date(data.telegram_group_board.at).toLocaleTimeString("zh-CN", {timeZone: zone, hour12: false})} · {zone}</small> : <ChainTools
              aid={aid}
              today={day.period}
              platform={platform}
              setPlatform={setPlatform}
              lane={lane}
              setLane={setLane}
              open={openStage}
            />}
          </div>
          {data.telegram_group_board ? <TelegramGroupCard board={data.telegram_group_board} zone={zone} history={()=>{setPlatform("telegram");setDialog("history")}}/> : <>
          {activity && (
            <div className={styles.liveStatus} role="status" aria-label="Agent 当前状态" data-state={paused?"paused":reconciling?"reconciling":working?"running":"waiting"}>
              <strong><i aria-hidden="true"/>{paused?"已暂停":reconciling?"核对回执":working?"正在处理":"待命中"}</strong>
              {!paused && <span title={activityText}>{activityText}</span>}
              {!working && !paused && activity.next_check_at && <small title={`下次检查 ${timestamp(activity.next_check_at,zone)}`}>{timestamp(activity.next_check_at,zone,false)} 检查</small>}
            </div>
          )}
          {chains.length === 0 ? (
            <div className={styles.empty}>
              {platform ? "该平台暂无符合条件的任务" : lane ? "暂无符合当前状态的任务" : "暂无进行中或今日已完成的任务"}
              {(platform || lane) && <button className="text-button" onClick={() => { setPlatform(""); setLane(""); }}>清除筛选</button>}
            </div>
          ) : <ChainRows
            today
            key={platform + ":" + lane}
            period={day.period}
            chains={chains}
            open={openStage}
            paused={paused}
            zone={zone}
          />}
          </>}
          <footer className={styles.footer}>
            <div className={styles.footerActions}>
              <button
                className="text-button"
                aria-haspopup="dialog"
                onClick={() => void openDetails("dependencies")}
              >
                平台依赖
                {data.platform_issues?.length
                  ? ` · ${data.platform_issues.length} 项`
                  : ""}
                <ArrowUpRight size={12} />
              </button>
              <button
                className="text-button"
                aria-haspopup="dialog"
                onClick={() => setDialog("history")}
              >
                更多记录
                <ArrowUpRight size={12} />
              </button>
            </div>
          </footer>
        </>
      )}
      {!day && (
        <button
          className="text-button"
          aria-haspopup="dialog"
          onClick={() => setDialog("history")}
        >
          查看工作记录 <ArrowUpRight size={12} />
        </button>
      )}
      {selectedWork && (
        <DialogPanel
          size="default"
          title={
            sourceWork
              ? selectedWork.title.replace(/^分发[：:]\s*/, "")
              : selectedWork.title
          }
          onClose={() => setSelected(null)}
        >
          <div className={styles.workDetail}>
            <div className={styles.detailTop} data-lane={laneFor(selectedWork)}>
              {selectedWork.placeholder || selectedWork.state==="basis" ? (
                <small>{statusLabel(selectedWork)}</small>
              ) : (
                <ExecutionState
                  item={selectedWork}
                  animate={!paused && !historical.until}
                />
              )}
              <Stage stage={selectedWork.stage} />
              <span className={styles.detailChannel}>
                <Channel item={selectedWork} agent={agent} />
                {selectedWork.platform
                  ? socialNames[selectedWork.platform] || selectedWork.platform
                  : "Agent 系统"}
              </span>
            </div>
            {selectedWork.state === "blocked" ? (
              <section className={styles.blocker} aria-label="受阻原因">
                <h3>{selectedWork.reason || "未记录具体原因"}</h3>
                {selectedWork.next_step && <p>{selectedWork.next_step}</p>}
              </section>
            ) : (
              selectedWork.reason && (
                <p className={styles.taskReason}>{selectedWork.reason}</p>
              )
            )}
            {sourceWork && <p className={styles.muted}>尚未为此稿安排发布时间。{selectedWork.channel_check_at && "渠道下次复查："+timestamp(selectedWork.channel_check_at,detailZone)}</p>}
            {!sourceWork && selectedWork.scheduled_at &&
              ["ready", "waiting", "blocked"].includes(selectedWork.state) && (
                <p className={styles.muted}>
                  安排时间：{timestamp(selectedWork.scheduled_at, detailZone)}
                </p>
              )}
            {selectedWork.state === "done" && selectedWork.completed_at && (
              <p className={styles.muted}>
                {selectedWork.stage === "distribution"
                  ? "公开确认"
                  : "执行完成"}
                ：{timestamp(selectedWork.completed_at, detailZone)}
              </p>
            )}
            {selectedWork.stage === "distribution" && selectedWork.receipt && <p className={styles.muted}>平台回执 · {selectedWork.receipt}</p>}
            {selectedWork.external_url &&
              selectedWork.receipt &&
              selectedWork.state === "done" && (
                <a
                  className={styles.resultLink}
                  href={selectedWork.external_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看发布结果 <ArrowUpRight size={13} />
                </a>
              )}
            {selectedWork.state==="basis" && selectedWork.basis_task_id &&
              <TopicBasis aid={aid} taskId={selectedWork.basis_task_id} until={historical.until}/>}
            {recordsTaskId && (
              <PlanWorkRecords
                key={`${aid}:${recordsTaskId}`}
                aid={aid}
                taskId={recordsTaskId}
                zone={detailZone}
                until={historical.until}
                observation={selectedWork.id.startsWith("clicks:")}
                materialOnly={selectedWork.materialOnly}
                draft={!!sourceWork || (selectedWork.stage==="content" && !selectedWork.materialOnly)}
                source={!!sourceWork}
              />
            )}
          </div>
        </DialogPanel>
      )}
      {detailDialog && data?.state === "group_summary" && (
        <DialogPanel title={dialog === "direction" ? "当前方向" : "平台依赖"} onClose={closeDetails}>
          {detailError ? <p role="alert">{detailError} <button onClick={() => void openDetails(dialog)}>重试</button></p>
            : <p role="status">正在读取详情…</p>}
        </DialogPanel>
      )}
      {dialog === "direction" && day && data?.state !== "group_summary" && (
        <DialogPanel
          size="compact"
          title="当前方向"
          onClose={closeDetails}
        >
          <div className={styles.directionDetails}>
            <div className={styles.directionLead}>
              <div className={styles.goal}>
                <Compass size={20} aria-hidden="true" />
                <strong>{direction}</strong>
              </div>
              <p>
                <span>当前重点</span>
                {focusNames[data.week.focus] || focusNames.distribution}
              </p>
            </div>
            {data.strategy && (
              <section aria-label="当前获客策略">
                <h3>当前获客策略</h3>
                <dl className={styles.facts}>
                  {[
                    ["人群与需求", [data.strategy.audience, data.strategy.trigger, data.strategy.alternative].filter(Boolean).join(" · ")],
                    ["触达与分发", data.strategy.source_label],
                    ["价值表达", [data.strategy.promise, data.strategy.proof].filter(Boolean).join(" · ")],
                    ["首次转化", [data.strategy.first_step, data.strategy.entry, data.strategy.objective_event].filter(Boolean).join(" · ")],
                    ["当前瓶颈", data.strategy.bottleneck], ["下一步", data.strategy.next_action],
                    ["复测与停止条件", [data.strategy.validation, data.strategy.stop_condition].filter(Boolean).join(" · ")],
                    ["近 30 天可核实的新增用户", measured(data.strategy.results_30d?.new_users)],
                    ["投入上限", data.strategy.cost_ceiling], ["质量观察", data.strategy.quality_check],
                  ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "待核验"}</dd></div>)}
                </dl>
              </section>
            )}
            <details className={styles.detailRow} name={`${boardId}-direction`}>
              <summary>受众与效果</summary>
              <dl className={styles.facts}>
                <div>
                  <dt>目标受众</dt>
                  <dd>{data.week.audience || "待确定"}</dd>
                </div>
                <div>
                  <dt>近 30 天</dt>
                  <dd>
                    <ul className={styles.events}>
                      <li>
                        <span>短链点击</span>
                        <span>
                          {measured(data.mode.clicks.filtered_clicks_30d)}
                        </span>
                      </li>
                      {reportedEvents.map((item) => (
                        <li key={item.name}>
                          <span>{item.label}</span>
                          <span>
                            {measured(item.observed_count_30d)} 次 · 已归因{" "}
                            {measured(item.attributed_count_30d)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </dd>
                </div>
              </dl>
            </details>
            {!readOnly && (
              <details
                className={styles.detailRow}
                name={`${boardId}-direction`}
              >
                <summary>调整偏好</summary>
                <form className={styles.preferences} onSubmit={savePreferences}>
                  <label>
                    优先业务方向
                    <select
                      aria-label="优先业务方向"
                      value={focus}
                      onChange={(e) => {
                        setFocus(e.target.value);
                        setSaved(false);
                      }}
                    >
                      <option value="distribution">引流分发</option>
                      <option value="content">内容筹备</option>
                      <option value="audience">受众与新线索</option>
                      <option value="observation">效果观察</option>
                    </select>
                  </label>
                  {reportedEvents.length > 0 && (
                    <label>
                      经营目标事件
                      <select
                        aria-label="经营目标事件"
                        value={event}
                        onChange={(e) => {
                          setEvent(e.target.value);
                        setCriteria(v => ({...v, first_definition: ""}));
                          setSaved(false);
                        }}
                      >
                        <option value="">尚未指定</option>
                        {event &&
                          !reportedEvents.some(
                            (item: Row) => item.name === event,
                          ) && (
                            <option value={event}>原目标暂无正式上报</option>
                          )}
                        {reportedEvents.map((item: Row) => (
                          <option key={item.name} value={item.name}>
                            {item.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {([
                    ["first_definition", "产品端保证仅首次上报的口径（尚未保证请留空）", 300],
                    ["cost_ceiling", "获客成本或试验预算上限（注明单位）", 150],
                    ["quality", "质量指标与观察窗口", 300],
                  ] as const).map(([key, label, length]) => <label key={key}>{label}
                    <input value={criteria[key]} maxLength={length} onChange={e => {
                      setCriteria({...criteria, [key]: e.target.value}); setSaved(false);
                    }}/>
                  </label>)}
                  <button disabled={saving} type="submit">
                    {saving ? "保存中" : "保存方向偏好"}
                  </button>
                </form>
                {saveError && (
                  <p role="alert" className={styles.error}>
                    {saveError}
                    <button className="text-button" onClick={() => resetPreferences()}>
                      重新读取偏好
                    </button>
                  </p>
                )}
                {saved && (
                  <p role="status" className={styles.muted}>
                    方向偏好已保存。
                  </p>
                )}
              </details>
            )}
            <details className={styles.detailRow} name={`${boardId}-direction`}>
              <summary>调整记录</summary>
              {data.changes?.length ? (
                <ol className={styles.changes}>
                  {data.changes.map((item: Row) => (
                    <li key={`${item.plan_id}:${item.version}`}>
                      <span>{item.reason}</span>
                      <small>{timestamp(item.at, zone)}</small>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className={styles.muted}>暂无调整记录。</p>
              )}
            </details>
          </div>
        </DialogPanel>
      )}
      {dialog === "dependencies" && day && data?.state !== "group_summary" && (
        <DialogPanel
          title="平台依赖"
          onClose={() => setDialog(null)}
          footer={<button onClick={() => setDialog(null)}>关闭</button>}
        >
          <ul className={styles.issues}>
            {data.platform_issues.map((issue: Row) => (
              <li key={issue.key}>
                <h3>{issue.title}</h3>
                <p>{issue.impact}</p>
                <small>
                  {issue.next_action}
                  {issue.retry_at
                    ? ` · ${timestamp(issue.retry_at, zone)}`
                    : ""}
                </small>
              </li>
            ))}
          </ul>
          {!data.platform_issues.length && (
            <p className={styles.muted}>暂无平台依赖</p>
          )}
        </DialogPanel>
      )}
      {dialog === "history" && (
        <ActivityHistory
          key={aid}
          agent={agent}
          initialPlatform={platform}
          onClose={() => setDialog(null)}
        />
      )}
    </section>
  );
}
