"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  Bot,
  BookOpen,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Circle,
  Clock3,
  Layers,
  ListFilter,
  Minus,
  AudioLines,
  RotateCw,
} from "lucide-react";
import { api } from "./api-client";
import DialogPanel from "./dialog-panel";
import SocialIcon, { socialNames } from "./social-icon";
import {
  Chain,
  Work,
  laneFor,
  stages,
  statusLabel,
  synthetic,
  taskChains,
  timestamp,
} from "./operating-plan-model";
import styles from "./operating-plans.module.css";

type OpenStage = (item: Work, until?: string, timezone?: string) => void;
const laneNames: Record<string, string> = {
  running: "进行中",
  done: "今日已完成",
};
function waitingForWindow(item: Work) {
  return !synthetic(item) && item.stage === "distribution" && ["waiting","ready"].includes(item.state)
    && /发布窗口|发布间隔/.test(item.reason || "");
}

function windowLabel(item: Work, zone: string, period: string) {
  const time = timestamp(item.scheduled_at, zone, false);
  if (!time || !item.scheduled_at) return "等发布窗口";
  const day = new Date(item.scheduled_at).toLocaleDateString("sv-SE", {timeZone: zone});
  return day === period ? `${time} 开窗` : timestamp(item.scheduled_at, zone);
}

export function ChainRows({
  chains,
  period,
  open,
  paused,
  today = false,
  zone,
}: {
  chains: Chain[];
  period: string;
  open: OpenStage;
  paused: boolean;
  today?: boolean;
  zone: string;
}) {
  const [extra, setExtra] = useState(0);
  if (!chains.length) return <p className={styles.empty}>暂无符合条件的任务</p>;
  return (
    <div className={styles.chainList}>
      {chains.slice(0, 6 + extra).map(({ current, title, steps }) => (
        <article key={current.id} className={styles.chainRow}>
          <div className={styles.chainTitle}>
            <span
              role="img"
              title={socialNames[current.platform || ""] || "Agent 系统"}
              aria-label={socialNames[current.platform || ""] || "Agent 系统"}
            >
              {current.platform ? (
                <SocialIcon platform={current.platform} size={17} />
              ) : (
                <Bot size={16} />
              )}
            </span>
            <strong>{title}</strong>
          </div>
          <div className={styles.track} data-today={today || undefined}>
            {steps.map((item) => {
              const Icon =
                item.state === "basis"
                  ? BookOpen
                  : item.placeholder
                    ? Minus
                    : item.state === "running"
                      ? AudioLines
                      : item.state === "done"
                        ? Check
                        : item.state === "ready"
                          ? Circle
                          : item.state === "reconciling"
                            ? RotateCw
                            : Clock3;
              const window = waitingForWindow(item);
              const inventory = synthetic(item) && item.stage === "distribution";
              const label = inventory && item.reason==="稿件语言未标记" ? "待核对语言"
                : inventory && ["ready","waiting"].includes(item.state)
                ? (item.channel_check_at ? timestamp(item.channel_check_at,zone,false)+" 复查" : "待选稿")
                : window ? windowLabel(item, zone, period) :
                !today && item.stage === "observation" && item.observed_clicks
                  ? `${item.observed_clicks} 次点击`
                  : statusLabel(item);
              return (
                <button
                  key={item.id}
                  data-state={item.state}
                  data-animate={
                    (!paused && item.state === "running") || undefined
                  }
                  aria-haspopup="dialog"
                  title={inventory && item.channel_check_at ? `渠道复查：${timestamp(item.channel_check_at,zone)}，此稿尚未排期` : window ? `等待发布窗口 ${timestamp(item.scheduled_at,zone)}` : undefined}
                  aria-label={`${title} · ${stages[item.stage]} · ${window ? "等待发布窗口 " : ""}${label}`}
                  onClick={() => open(item)}
                >
                  <i className={styles.node}>
                    <Icon size={13} />
                  </i>
                  <small>{stages[item.stage]}</small>
                  <span>{label}{today && item.state === "done" && item.completed_at && " · " + timestamp(item.completed_at, zone, false)}</span>
                </button>
              );
            })}
          </div>
          {laneFor(current) === "waiting" && !waitingForWindow(current) && !(synthetic(current) && current.state==="waiting") && (
            <p className={styles.waitNote}>
              <Clock3 size={12} />
              {current.reason || statusLabel(current)}
              {current.scheduled_at &&
                " · " + timestamp(current.scheduled_at, zone)}
            </p>
          )}
        </article>
      ))}
      {chains.length > 6 + extra && (
        <button className="text-button" onClick={() => setExtra((n) => n + 6)}>
          查看更多（剩余 {chains.length - 6 - extra} 项）
        </button>
      )}
    </div>
  );
}

type Archive = {
  day: { queue: Work[] };
  timezone: string;
  updated_at?: string;
  records: {
    id: string;
    task_id: string;
    stage: string;
    platform?: string;
    title: string;
    detail?: string;
    created_at: string;
  }[];
  has_more: boolean;
};
function useRead<T>(path: string | null) {
  const [result, setResult] = useState<{
    path: string;
    data?: T;
    error?: string;
  } | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!path) return;
    const ac = new AbortController();
    api(path, undefined, ac.signal)
      .then((data) => {
        if (!ac.signal.aborted) setResult({ path, data });
      })
      .catch((e) => {
        if (!ac.signal.aborted) setResult({ path, error: e.message });
      });
    return () => ac.abort();
  }, [path, attempt]);
  return {
    data: result?.path === path ? result?.data : undefined,
    error: result?.path === path ? result?.error : undefined,
    retry: () => {
      setResult(null);
      setAttempt((n) => n + 1);
    },
  };
}
export function TopicBasis({
  aid,
  taskId,
  until,
}: {
  aid: string;
  taskId: string;
  until?: string;
}) {
  const { data, error, retry } = useRead<{
    topic: string;
    plan?: string;
    purpose?: string;
    quotes: string[];
    business: boolean;
    sources: { title: string; url: string }[];
  }>(
    `/agents/${aid}/plans/work/${taskId}/basis${until ? "?until=" + encodeURIComponent(until) : ""}`,
  );
  if (error) return <button onClick={retry}>{error} · 重试</button>;
  if (!data) return <p role="status">正在读取选题依据…</p>;
  return (
    <section className={styles.basis}>
      <h3>{data.topic || "发布内容的来源"}</h3>
      {data.plan && <p>按渠道计划轮换选题 · {data.plan}</p>}
      {data.purpose && <p>{data.purpose}</p>}
      {data.business && <p>采用了任务关联的业务资料版本。</p>}
      {data.sources.length > 0 && (
        <>
          <h3>关联资料来源</h3>
          <ul>
            {data.sources.map((v, i) => (
              <li key={i}>
                <a href={v.url} target="_blank" rel="noreferrer">
                  {v.title} ↗
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
      <h3>稿件引用的业务资料</h3>
      {data.quotes.length ? (
        <ul>
          {data.quotes.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      ) : (
        <p>当时暂无已保存的稿件引用。</p>
      )}
    </section>
  );
}
function ArchiveDay({
  aid,
  day,
  close,
  open,
}: {
  aid: string;
  day: string;
  close: () => void;
  open: OpenStage;
}) {
  const [offset, setOffset] = useState(0);
  const path = `/agents/${aid}/plans/history/${day}`;
  const { data, error, retry } = useRead<Archive>(path);
  const page = useRead<Pick<Archive, "records" | "has_more">>(
    offset ? path + "?offset=" + offset : null,
  );
  const records = offset ? page.data : data;
  return (
    <DialogPanel title={day + " · 历史执行"} onClose={close}>
      {error ? (
        <button onClick={retry}>{error} · 重试</button>
      ) : !data ? (
        <p>正在读取历史记录…</p>
      ) : (
        <>
          {taskChains(data.day.queue).length > 0 && (
            <ChainRows
              period={day}
              chains={taskChains(data.day.queue)}
              open={(item) => open(item, data.updated_at, data.timezone)}
              paused
              zone={data.timezone}
            />
          )}
          <section className={styles.recordTrail}>
            <h3>当日执行记录</h3>
            {page.error ? (
              <button onClick={page.retry}>{page.error} · 重试</button>
            ) : !records ? (
              <p>正在读取…</p>
            ) : (
              <>
                <ol>
                  {records.records.map((r) => (
                    <li key={r.id}>
                      <time>
                        {timestamp(r.created_at, data.timezone, false)}
                      </time>
                      <div>
                        <button
                          className="text-button"
                          aria-haspopup="dialog"
                          onClick={() =>
                            open(
                              {
                                id: r.task_id,
                                evidence_task_id: r.task_id,
                                title: r.title,
                                stage: r.stage,
                                platform: r.platform,
                                state: "recorded",
                              },
                              r.created_at,
                              data.timezone,
                            )
                          }
                        >
                          {r.title} ↗
                        </button>
                        <small>{r.detail}</small>
                      </div>
                    </li>
                  ))}
                </ol>
                {!records.records.length && (
                  <p className={styles.empty}>当日暂无执行记录</p>
                )}
              </>
            )}
            {offset > 0 && (
              <button onClick={() => setOffset((n) => n - 50)}>上一页</button>
            )}
            {records?.has_more && (
              <button onClick={() => setOffset((n) => n + 50)}>下一页</button>
            )}
          </section>
        </>
      )}
    </DialogPanel>
  );
}
function shiftMonth(month: string, delta: number) {
  const date = new Date(month + "-01T12:00:00Z");
  date.setUTCMonth(date.getUTCMonth() + delta);
  return date.toISOString().slice(0, 7);
}

export function ChainTools({
  aid,
  today,
  platform,
  setPlatform,
  lane,
  setLane,
  open,
}: {
  aid: string;
  today: string;
  platform: string;
  setPlatform: (v: string) => void;
  lane: string;
  setLane: (v: string) => void;
  open: OpenStage;
}) {
  const [menu, setMenu] = useState<"filters" | "calendar" | null>(null),
    [month, setMonth] = useState(today.slice(0, 7)),
    [day, setDay] = useState("");
  const root = useRef<HTMLDivElement>(null),
    calendarButton = useRef<HTMLButtonElement>(null),
    filterButton = useRef<HTMLButtonElement>(null);
  const id = useId();
  const dates = useRead<{ dates: string[] }>(
    menu === "calendar"
      ? `/agents/${aid}/plans/history?month=${month}-01`
      : null,
  );
  useEffect(() => {
    if (!menu) return;
    const outside = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setMenu(null);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [menu]);
  const first = new Date(month + "-01T12:00:00Z"),
    last = new Date(first);
  last.setUTCMonth(last.getUTCMonth() + 1, 0);
  const count = Number(!!platform) + Number(!!lane);
  return (
    <div
      className={styles.chainTools}
      ref={root}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setMenu(null);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && menu) {
          (menu === "calendar"
            ? calendarButton
            : filterButton
          ).current?.focus();
          setMenu(null);
          e.stopPropagation();
        }
      }}
    >
      <button
        ref={filterButton}
        aria-expanded={menu === "filters"}
        aria-label="筛选渠道和状态"
        aria-controls={menu === "filters" ? id : undefined}
        onClick={() => setMenu(menu === "filters" ? null : "filters")}
      >
        {platform && platform !== "system" ? (
          <SocialIcon platform={platform} size={14} />
        ) : (
          <ListFilter size={14} />
        )}
        筛选{count || ""}
      </button>
      <button
        ref={calendarButton}
        title="历史执行"
        aria-label="查看历史执行日历"
        aria-expanded={menu === "calendar"}
        onClick={() => setMenu(menu === "calendar" ? null : "calendar")}
      >
        <CalendarDays size={16} />
      </button>
      {menu === "filters" && (
        <div id={id} className={styles.filterMenu}>
          <small>渠道</small>
          {Object.entries({
            "": "全部渠道",
            ...socialNames,
            system: "Agent 系统",
          }).map(([value, name]) => (
            <button
              key={value}
              aria-pressed={value === platform}
              onClick={() => setPlatform(value)}
            >
              {value === "system" ? (
                <Bot size={15} />
              ) : value ? (
                <SocialIcon platform={value} size={15} />
              ) : (
                <Layers size={15} />
              )}{" "}
              {name}
              {value === platform && <Check size={14} />}
            </button>
          ))}
          <small>任务状态</small>
          {Object.entries({ "": "全部状态", ...laneNames }).map(([v, n]) => (
            <button
              key={v}
              aria-pressed={v === lane}
              onClick={() => setLane(v)}
            >
              {n}
              {v === lane && <Check size={14} />}
            </button>
          ))}
        </div>
      )}
      {menu === "calendar" && (
        <div className={styles.calendar}>
          <div className={styles.month}>
            <button
              aria-label="上个月"
              onClick={() => setMonth(shiftMonth(month, -1))}
            >
              <ChevronLeft size={15} />
            </button>
            {month}
            <button
              aria-label="下个月"
              disabled={month >= today.slice(0, 7)}
              onClick={() => setMonth(shiftMonth(month, 1))}
            >
              <ChevronRight size={15} />
            </button>
          </div>
          <div className={styles.days}>
            {Array.from("一二三四五六日").map((w) => (
              <small key={w}>{w}</small>
            ))}
            {Array.from({ length: (first.getUTCDay() + 6) % 7 }, (_, i) => (
              <span key={"blank" + i} />
            ))}
            {Array.from({ length: last.getUTCDate() }, (_, i) => {
              const date = month + "-" + String(i + 1).padStart(2, "0");
              const marked = dates.data?.dates.includes(date);
              return (
                <button
                  key={date}
                  data-mark={marked || undefined}
                  aria-label={date + (marked ? "，有执行记录" : "")}
                  disabled={date >= today}
                  onClick={() => {
                    calendarButton.current?.focus();
                    setMenu(null);
                    setDay(date);
                  }}
                >
                  {i + 1}
                </button>
              );
            })}
          </div>
          {dates.error ? (
            <button onClick={dates.retry}>读取日期失败 · 重试</button>
          ) : (
            <small>{dates.data ? "● 有执行记录" : "正在读取记录日期…"}</small>
          )}
        </div>
      )}
      {day && (
        <ArchiveDay
          key={day}
          aid={aid}
          day={day}
          close={() => setDay("")}
          open={open}
        />
      )}
    </div>
  );
}
