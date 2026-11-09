"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, RefreshCw } from "lucide-react";
import { api } from "./api-client";
import DialogPanel from "./dialog-panel";
import PromotionLink from "./promotion-link";
import { socialNames } from "./social-icon";
import styles from "./analytics-summary.module.css";

type Row = Record<string, any>;
const names: Record<string, string> = {
  CompleteRegistration: "注册成功",
  Lead: "提交线索",
  Purchase: "购买",
  StartTrial: "开始试用",
  Subscribe: "订阅",
  Activation: "有效使用",
};
const channelName = (s: string) =>
  s === "unattributed" ? "未归因" : socialNames[s] || s;
// One selection drives every query in the acquisition card and its details.
export function trafficParams(selection: string) {
  const site = selection === "site" || selection === "unattributed";
  const params = new URLSearchParams({ traffic: site ? "site" : "promotion" });
  if (selection !== "site" && selection !== "promotion")
    params.set("channel", selection);
  return params;
}
const num = (n: number | null | undefined) =>
  n == null ? "—" : n.toLocaleString("zh-CN");
const money = (value: string) => {
  const [whole, fraction = ""] = value.split(".");
  const decimals = fraction.replace(/0+$/, "");
  return (
    BigInt(whole).toLocaleString("zh-CN") + (decimals ? "." + decimals : "")
  );
};
const stamp = (s: string | null | undefined) =>
  s
    ? new Date(s).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "尚无更新";
function dates() {
  const end = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
  }).format(new Date());
  return {
    end,
    start: new Date(new Date(end + "T00:00:00Z").getTime() - 29 * 86400000)
      .toISOString()
      .slice(0, 10),
  };
}

// Results carry the complete Agent/query identity, including during render
// before effect cleanup. A late response cannot populate another Agent's UI.
export function useScopedData(aid: string, path: string) {
  const [result, setResult] = useState<{ path: string; value: Row } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);
  const current = useRef(path);
  current.current = path;
  useEffect(() => {
    let active = true,
      sequence = 0;
    async function load() {
      const request = ++sequence;
      setLoading(true);
      try {
        const value = await api(path);
        if (!active || current.current !== path || sequence !== request) return;
        if (value.agent_id !== aid) throw Error("数据归属不匹配，请刷新重试");
        setResult({ path, value });
        setError("");
      } catch (e) {
        if (active && current.current === path && sequence === request)
          setError(e instanceof Error ? e.message : "数据暂时无法读取");
      } finally {
        if (active && current.current === path && sequence === request)
          setLoading(false);
      }
    }
    void load();
    const changed = () => {
      if (!document.hidden) void load();
    };
    window.addEventListener("lead:updated", changed);
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 30000);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("lead:updated", changed);
    };
  }, [aid, path, version]);
  return {
    data:
      result?.path === path && result.value.agent_id === aid
        ? result.value
        : null,
    error,
    loading,
    refresh: () => setVersion((v) => v + 1),
  };
}

function DataTable({
  label,
  head,
  rows,
}: {
  label: string;
  head: string[];
  rows: (string | number | null | undefined)[][];
}) {
  return (
    <div
      className={styles.scroll}
      role="region"
      aria-label={label}
      tabIndex={0}
    >
      <table>
        <thead>
          <tr>
            {head.map((s) => (
              <th key={s} scope="col">
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((s, j) =>
                j === 0 ? (
                  <th key={j} scope="row">
                    {s}
                  </th>
                ) : (
                  <td key={j}>{typeof s === "number" ? num(s) : (s ?? "—")}</td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AnalyticsSummary({
  aid,
  readOnly,
  promotion,
  initialChannel = "promotion",
  onSetup,
}: {
  aid: string;
  readOnly: boolean;
  promotion: Row;
  initialChannel?: string;
  onSetup: () => void;
}) {
  const [range, setRange] = useState(dates),
    [channel, setChannel] = useState(initialChannel),
    [health, setHealth] = useState(false);
  const params = trafficParams(channel);
  params.set("start_date", range.start);
  params.set("end_date", range.end);
  const { data, error, loading, refresh } = useScopedData(
    aid,
    `/agents/${aid}/analytics/summary?${params}`,
  );
  const channels = new Set([...Object.keys(socialNames),
    ...(data?.channels || []).map((r: Row) => String(r.channel)), channel]);
  for (const reserved of ["site", "promotion", "unattributed"]) channels.delete(reserved);
  const m = data?.summary || {},
    site = data?.coverage.site;
  const metric = site && m.pv != null ? "pv" : "clicks";
  const series: Row[] = data?.series || [],
    max = Math.max(1, ...series.map((r) => Number(r[metric]) || 0));
  const x = (i: number) => 12 + i * (576 / Math.max(1, series.length - 1)),
    y = (r: Row) => 112 - ((Number(r[metric]) || 0) / max) * 96;
  const labels = [
    ["推广点击", "clicks"],
    ["页面浏览 PV", "pv"],
    ["独立访客 UV", "visitors"],
    ["访问会话", "sessions"],
    ["事件次数", "event_count"],
    ["服务端转化", "conversions"],
  ];
  const missing = (key: string) =>
    !data
      ? "正在读取"
      : (data.aggregation.unbuilt_days || data.aggregation.outdated_days)
        ? "汇总中"
        : key === "conversions"
          ? data.coverage.server
            ? "暂不可用"
            : "未接入"
          : !site
            ? "未接入"
            : "暂不可用";
  return (
    <div className={styles.summary} data-analytics-agent={aid}>
      <PromotionLink
        aid={aid}
        config={data?.promotion || promotion}
        readOnly={readOnly}
        onSaved={refresh}
      />
      <div className={styles.filters}>
        <label>
          开始日期
          <input
            type="date"
            value={range.start}
            max={range.end}
            onChange={(e) => {
              if (e.target.value)
                setRange((v) => ({ ...v, start: e.target.value }));
            }}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={range.end}
            min={range.start}
            max={dates().end}
            onChange={(e) => {
              if (e.target.value)
                setRange((v) => ({ ...v, end: e.target.value }));
            }}
          />
        </label>
        <label>
          来源渠道
          <select value={channel} onChange={(e) => setChannel(e.target.value)}>
            <option value="promotion">全部推广渠道</option>
            <option value="site">全站流量</option>
            <option value="unattributed">未归因</option>
            {Array.from(channels).map((s) => (
              <option key={s} value={s}>
                {channelName(s)}
              </option>
            ))}
          </select>
        </label>
        <button
          className="text-button"
          onClick={refresh}
          disabled={loading}
          aria-label="刷新获客统计"
        >
          <RefreshCw size={14} />
          刷新
        </button>
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
          <button className="text-button" onClick={refresh}>
            重试
          </button>
        </p>
      )}
      {!data ? (
        !error && (
          <div className={styles.skeleton} role="status">
            正在读取当前 Agent 的统计…
          </div>
        )
      ) : (
        <>
          <dl className={styles.metrics}>
            {labels.map(([label, key]) => (
              <div key={key} data-metric={key}>
                <dt>{label}</dt>
                <dd className={m[key] == null ? styles.unknown : undefined}>
                  {m[key] == null ? missing(key) : num(m[key])}
                </dd>
              </div>
            ))}
          </dl>
          <p className={styles.definition}>
            浏览按本次访问来源统计，转化按归因窗口内的有效推广触点统计。北京时间按日去重访客。服务端转化目标为
            {names[data.conversion_event] || data.conversion_event}。
            <button className="text-button" onClick={() => setHealth(true)}>
              数据状态 <ArrowUpRight size={13} />
            </button>
          </p>
          {!site && (
            <p className={styles.coverage}>
              短链点击可独立查看。接入网站像素后，查看浏览、访客和事件。
              <button className="text-button" onClick={onSetup}>
                接入网站像素 <ArrowUpRight size={13} />
              </button>
            </p>
          )}
          <div className={styles.chart}>
            <div className={styles.subhead}>
              <h3>{metric === "pv" ? "页面浏览趋势" : "推广点击趋势"}</h3>
              <span>按日 · 北京时间</span>
            </div>
            {series.length >= 2 && !data.aggregation.unbuilt_days ? (
              <svg
                viewBox="0 0 600 140"
                role="img"
                aria-label={
                  metric === "pv" ? "页面浏览按日趋势" : "推广点击按日趋势"
                }
              >
                <line x1="12" x2="588" y1="112" y2="112" stroke="var(--line)" />
                <polyline
                  points={series.map((r, i) => `${x(i)},${y(r)}`).join(" ")}
                  fill="none"
                  stroke="var(--data)"
                  strokeWidth="2"
                />
                {series.map((r, i) => (
                  <circle
                    key={r.bucket}
                    cx={x(i)}
                    cy={y(r)}
                    r="3"
                    fill="var(--data)"
                  >
                    <title>
                      {stamp(r.bucket)}：{num(r[metric])}
                    </title>
                  </circle>
                ))}
                <text x="12" y="135">
                  {stamp(series[0].bucket).split(" ")[0]}
                </text>
                <text x="588" y="135" textAnchor="end">
                  {stamp(series.at(-1)?.bucket).split(" ")[0]}
                </text>
              </svg>
            ) : (
              <p className={styles.chartEmpty}>
                {(data.aggregation.unbuilt_days || data.aggregation.outdated_days)
                  ? "正在汇总已接收的数据"
                  : m[metric] > 0
                    ? `所选范围已记录 ${num(m[metric])} 次${metric === "pv" ? "页面浏览" : "推广点击"}，更多日期有数据后展示趋势。`
                    : "所选范围暂无可展示的趋势"}
              </p>
            )}
          </div>
          <div className={styles.subhead}>
            <h3>渠道明细</h3>
          </div>
          <DataTable
            label="渠道统计，可横向滚动"
            head={["渠道", "推广点击", "浏览 PV", "访问会话", "服务端转化"]}
            rows={data.channels.map((r: Row) => [
              channelName(r.channel),
              r.clicks,
              r.pv,
              r.sessions,
              r.conversions,
            ])}
          />
          {!data.channels.length && (
            <p className={styles.empty}>所选范围暂无渠道记录。</p>
          )}
          <div className={styles.subhead}>
            <h3>国家与地区</h3>
            <span>按 IP 近似识别</span>
          </div>
          <DataTable
            label="国家与地区统计，可横向滚动"
            head={["地区", "推广点击", "浏览 PV", "服务端转化"]}
            rows={(data.countries || []).map((r: Row) => [
              r.country === "ZZ"
                ? "未知／未解析"
                : new Intl.DisplayNames(["zh-CN"], { type: "region" }).of(
                    r.country,
                  ) || r.country,
              r.clicks,
              site ? r.pv : "未接入",
              data.coverage.server ? r.conversions : "未接入",
            ])}
          />
          {!(data.countries || []).length && (
            <p className={styles.empty}>所选范围暂无地域记录。</p>
          )}
          <p className={styles.definition}>
            {data.geography?.state === "ready"
              ? "地域库已就绪。"
              : data.geography?.state === "stale"
                ? "地域库需要更新，当前仍使用已安装版本。"
                : "地域库未就绪，无法解析的访问保留为未知。"}
            IP
            地域不代表用户真实所在地。服务端回传沿用有效点击的地域，不使用业务服务器
            IP。{" "}
            <a href="https://db-ip.com" target="_blank" rel="noreferrer">
              IP Geolocation by DB-IP
            </a>
          </p>
          <div className={styles.subhead}>
            <h3>事件明细</h3>
            <span>重复业务事件保留接收次数，计入次数去重</span>
          </div>
          <DataTable
            label="事件统计，可横向滚动"
            head={["事件", "接收次数", "计入次数", "涉及用户"]}
            rows={data.events.map((r: Row) => [
              names[r.event_name] || r.event_name,
              r.observed,
              r.count,
              r.users,
            ])}
          />
          {!data.events.length && (
            <p className={styles.empty}>
              {site || data.coverage.server
                ? "所选范围尚无业务事件。"
                : "接入像素或服务端回传后展示事件。"}
            </p>
          )}
          <div className={styles.revenue}>
            <span>服务端成交金额</span>
            {data.amounts.length ? (
              data.amounts.map((r: Row) => (
                <strong key={r.currency}>
                  {r.currency} {money(r.amount)}
                </strong>
              ))
            ) : (
              <span>
                {data.coverage.server
                  ? "所选范围尚无成交金额"
                  : "服务端回传未接入"}
              </span>
            )}
          </div>
          <p className={styles.updated} role="status">
            {data.aggregation.failed_days
              ? "部分日期汇总失败，可在数据状态中重试。"
              : data.aggregation.pending_days || data.aggregation.outdated_days
                ? "数据更新中，已完成的统计继续保留。"
                : "统计已更新"}{" "}
            · {stamp(data.aggregation.updated_at)}
          </p>
        </>
      )}
      {health && (
        <AnalyticsHealth
          key={aid}
          aid={aid}
          readOnly={readOnly}
          start={range.start}
          end={range.end}
          onClose={() => {
            setHealth(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function AnalyticsHealth({
  aid,
  readOnly,
  start,
  end,
  onClose,
}: {
  aid: string;
  readOnly: boolean;
  start: string;
  end: string;
  onClose: () => void;
}) {
  const { data, error, loading, refresh } = useScopedData(
    aid,
    `/agents/${aid}/analytics/ingestion`,
  );
  const [actionError, setActionError] = useState(""),
    [busy, setBusy] = useState("");
  async function run(path: string, body: Row, key: string) {
    setBusy(key);
    setActionError("");
    try {
      await api(path, body);
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "操作暂时失败");
    } finally {
      setBusy("");
    }
  }
  const count = (s: string) =>
    data?.counts.find((r: Row) => r.state === s)?.count || 0;
  return (
    <DialogPanel
      title="数据状态"
      description="当前 Agent 的正式数据。测试会话独立统计。"
      onClose={onClose}
      footer={<button onClick={onClose}>完成</button>}
    >
      <div className={styles.health} data-analytics-health-agent={aid}>
        {(error || actionError) && (
          <p role="alert" className={styles.error}>
            {error || actionError}
          </p>
        )}
        {!data ? (
          <p role="status">正在读取处理状态…</p>
        ) : (
          <>
            <dl className={styles.healthMetrics}>
              {[
                ["待处理", count("pending")],
                ["已处理", count("processed")],
                ["已隔离", count("quarantined")],
                ["处理失败", count("failed")],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{num(Number(value))}</dd>
                </div>
              ))}
            </dl>
            <p className={styles.definition}>
              待投递 {num(data.outbox.pending)} 条 · 最近 30 天，95%
              的事件处理耗时不超过{" "}
              {data.backlog.processing_p95_seconds == null
                ? "暂无记录"
                : `${Number(data.backlog.processing_p95_seconds).toFixed(2)} 秒`}
            </p>
            {data.backlog.oldest_pending_seconds != null && (
              <p className={styles.definition}>
                最早待处理事件已等待{" "}
                {Math.max(0, Math.floor(data.backlog.oldest_pending_seconds))}{" "}
                秒。
              </p>
            )}
            <AnalyticsPolicy
              key={aid}
              aid={aid}
              readOnly={readOnly || !data.can_manage}
            />
            <div className={styles.subhead}>
              <h3>统计更新</h3>
              <button
                className="text-button"
                disabled={loading || !!busy}
                onClick={refresh}
              >
                刷新
              </button>
            </div>
            <p>
              {data.reporting_backlog.pending_days
                ? `${data.reporting_backlog.pending_days} 个日期待汇总，其中 ${data.reporting_backlog.failed_days} 个失败。`
                : "统计更新队列为空。"}
            </p>
            {!readOnly && data.can_manage && (
              <button
                disabled={!!busy}
                onClick={() =>
                  void run(
                    `/agents/${aid}/analytics/summary/rebuild`,
                    { start_date: start, end_date: end },
                    "rebuild",
                  )
                }
              >
                {busy === "rebuild" ? "正在提交…" : "重新汇总所选日期"}
              </button>
            )}
            <div className={styles.subhead}>
              <h3>异常事件</h3>
              <span>最近 50 条</span>
            </div>
            {!data.failures.length && (
              <p className={styles.empty}>暂无隔离或处理失败的事件。</p>
            )}
            {data.failures.map((r: Row) => (
              <div className={styles.failure} key={r.id}>
                <div>
                  <strong>{names[r.event_name] || r.event_name}</strong>
                  <span>{stamp(r.received_at)}</span>
                  <p>
                    {r.state === "quarantined"
                      ? "事件校验未通过，请检查上报内容后使用新事件编号。"
                      : "处理暂时失败，可重试原事件。"}
                  </p>
                  <code>{r.error_code}</code>
                </div>
                {!readOnly && data.can_manage && r.retryable && (
                  <button
                    disabled={!!busy}
                    onClick={() =>
                      void run(
                        `/agents/${aid}/analytics/ingestion/${r.id}/retry`,
                        {},
                        r.id,
                      )
                    }
                  >
                    {busy === r.id ? "正在提交…" : "重试"}
                  </button>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </DialogPanel>
  );
}

function AnalyticsPolicy({
  aid,
  readOnly,
}: {
  aid: string;
  readOnly: boolean;
}) {
  const { data, error, refresh } = useScopedData(
    aid,
    `/agents/${aid}/analytics/policy`,
  );
  const [draft, setDraft] = useState<{ policy: Row; revision: number } | null>(
    null,
  );
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const fields: [string, string, number, number][] = [
    ["events_per_minute", "每分钟事件数", 1, 1000000],
    ["event_burst", "允许的瞬时事件数", 20, 100000],
    ["requests_per_minute", "每分钟请求数", 1, 1000000],
    ["request_burst", "允许的瞬时请求数", 1, 100000],
    ["ip_events_per_minute", "单个 IP 每分钟事件数（0 为不限）", 0, 1000000],
    ["ip_event_burst", "单个 IP 瞬时事件数", 20, 100000],
    ["daily_events", "每天接收尝试上限（0 为不限）", 0, 1000000000000],
    ["redirect_per_minute", "每分钟短链点击采集数", 1, 1000000],
    ["redirect_burst", "允许的瞬时短链点击数", 1, 100000],
    ["network_days", "IP 和原始设备信息保留天数", 1, 30],
    ["payload_days", "原始事件保留天数", 1, 90],
    ["clean_days", "清洗明细归档前保留天数", 30, 365],
    ["backlog_seconds", "积压告警等待秒数", 10, 86400],
    ["delivery_failure_threshold", "连续投递失败告警次数", 1, 1000],
  ];
  useEffect(() => {
    if (data && !draft)
      setDraft({ policy: { ...data.policy }, revision: data.revision });
  }, [data, draft]);
  const counters = data?.counters.find(
    (r: Row) => r.environment === "production",
  );
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!draft || !data || readOnly) return;
    setBusy(true);
    setNotice("");
    try {
      const result = await api(`/agents/${aid}/analytics/policy`, {
        ...draft.policy,
        expected_revision: draft.revision,
      });
      if (result.agent_id !== aid) throw Error("数据归属不匹配，请刷新");
      setDraft({ policy: result.policy, revision: result.revision });
      refresh();
      setNotice("当前 Agent 的采集设置已保存。");
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "设置未保存");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className={styles.policy} aria-label="采集保护与保留期">
      {!!(counters?.denied_events || counters?.skipped_redirects) && (
        <p role="status" className={styles.error}>
          今日有 {num(counters?.denied_events)} 次事件上报触发保护，
          {num(counters?.skipped_redirects)}{" "}
          次短链点击未采集。重试成功的事件按原编号去重。
        </p>
      )}
      <details
        onToggle={(e) => {
          if (e.currentTarget.open && data && !draft)
            setDraft({ policy: { ...data.policy }, revision: data.revision });
        }}
      >
        <summary>采集保护与保留期</summary>
        <p className={styles.definition}>
          仅作用于当前
          Agent，正式与测试额度分别计算。上限计算接收尝试，含重试；超过短链保护上限时仍会跳转。
        </p>
        <p className={styles.definition}>
          到期删除原始事件及 IP
          信息；清洗明细转入归档，汇总和去重凭据继续保留。设置变更对新接收事件生效。
        </p>
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {!data && !error && <p role="status">正在读取采集设置…</p>}
        {draft && (
          <form onSubmit={save}>
            <fieldset
              disabled={readOnly || busy}
              className={styles.policyFields}
            >
              <legend className="sr-only">当前 Agent 采集设置</legend>
              {fields.map(([key, label, min, max]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    min={min}
                    max={max}
                    step="1"
                    required
                    value={draft.policy[key]}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        policy: {
                          ...draft.policy,
                          [key]:
                            e.target.value === "" ? "" : Number(e.target.value),
                        },
                      })
                    }
                  />
                </label>
              ))}
            </fieldset>
            {!readOnly && (
              <button type="submit" disabled={busy}>
                {busy ? "正在保存…" : "保存采集设置"}
              </button>
            )}
          </form>
        )}
        {notice && <p role="status">{notice}</p>}
      </details>
    </section>
  );
}
