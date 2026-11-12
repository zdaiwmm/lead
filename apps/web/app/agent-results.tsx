"use client";
import { useState } from "react";
import AnalyticsPanel, { PixelTask } from "./analytics-panel";
import AnalyticsSummary, { trafficParams, useScopedData } from "./analytics-summary";
import { socialNames } from "./social-icon";
import PlatformFilter from "./platform-filter";
import DialogPanel from "./dialog-panel";
import styles from "./acquisition-card.module.css";

type Row = Record<string, any>;
export default function AgentResults({
  agent,
  readOnly,
  growth,
}: {
  agent: Row;
  readOnly: boolean;
  growth?: Row | null;
}) {
  const [task, setTask] = useState<PixelTask>(null);
  const [pixelData, setPixelData] = useState<Row | null>(null);
  const pixel =
    pixelData && pixelData.agent_id === agent.id ? pixelData.pixel : null;
  const promotion = agent.onboarding_state?.promotion;
  const [channel, setChannel] = useState("promotion");
  const [country, setCountry] = useState("");
  const [range, setRange] = useState(30);
  const [metric, setMetric] = useState("clicks");
  const [resumeEvent, setResumeEvent] = useState("");
  const [copied, setCopied] = useState(false);
  const [details, setDetails] = useState(false);
  const query = trafficParams(channel);
  if (country) query.set("country", country);
  const { data, error, refresh } = useScopedData(
    agent.id,
    `/agents/${agent.id}/analytics/overview?${query}`,
  );
  const rows: Row[] = (data?.series || []).slice(-range);
  const today = rows.at(-1);
  const updatedAt = today?.updated_at ? new Date(today.updated_at).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
  }) : null;
  const metrics = [
    ["clicks", "推广点击 PV"],
    ["devices", "点击设备数"],
  ];
  if (pixel?.installed)
    metrics.push(
      ["pv", "推广浏览 PV"],
      ["site_devices", "推广访客设备"],
      ...Object.entries(data?.events || {}).map(([k, v]) => [
        `event:${k}`,
        String(v),
      ]),
    );
  const selected = metrics.some(([k]) => k === metric) ? metric : "clicks";
  function value(row: Row | undefined, key: string): number | null {
    if (!row) return null;
    if (key.startsWith("event:"))
      return row.events == null ? null : row.events[key.slice(6)] || 0;
    return row[key] ?? null;
  }
  const values = rows.map((r) => value(r, selected));
  const peak = Math.max(1, ...values.map((v) => v ?? 0));
  const path = values
    .map((v, i) =>
      v == null
        ? ""
        : `${i && values[i - 1] != null ? "L" : "M"}${4 + (i * 392) / Math.max(values.length - 1, 1)},${89 - (80 * v) / peak}`,
    )
    .join(" ");
  const counts = pixel?.event_counts || (pixel && Array.isArray(pixel.events) ? {
    total: pixel.events.length, tested: pixel.events.filter((event: Row) => event.tested).length,
  } : null);
  const setup = () =>
    setTask(pixel?.installed ? "events" : pixel?.embedded ? "detect" : "embed");
  return (
    <section
      className={`workspace-section results-section ${styles.card}`}
      aria-labelledby="results-heading"
    >
      <header className={styles.header}>
        <h2 id="results-heading">获客效果</h2>
        <div className={styles.filters}>
          <PlatformFilter
            title="获客渠道"
            value={channel}
            options={[
              ["promotion", "全部推广渠道"],
              ...Object.entries(socialNames),
            ]}
            onChange={(next) => {
              setChannel(next);
              setCountry("");
            }}
          />
          <PlatformFilter
            title="获客国家或地区"
            icons={false}
            value={country}
            onChange={setCountry}
            options={[
              ["", "全部地区"],
              ...(data?.countries || (country ? [country] : [])).map(
                (k: string) => [
                  k,
                  k === "ZZ"
                    ? "未知地区"
                    : new Intl.DisplayNames(["zh-CN"], { type: "region" }).of(
                        k,
                      ) || k,
                ],
              ),
            ]}
          />
        </div>
      </header>
      <div className={styles.goal} aria-label="真实业务新增">
        <span>全项目核心价值新增{growth?.unit ? ` · ${{user:"用户",pair:"关系对",order:"订单",organization:"组织"}[growth.unit as string] || growth.unit}` : ""}</span>
        <strong>{growth?.new_units == null ? "未知" : growth.new_units.toLocaleString("zh-CN")}</strong>
        <small>{growth?.measurement === "healthy" ? "近 30 天，按当前服务端业务口径去重" : growth?.measurement === "stale" ? "业务回传已过期，当前结果可能不完整" : "业务结果尚未接通，访问数据仅作为学习线索"}</small>
      </div>
      <div className={styles.overview}><div><h3>今日访问线索</h3>
      <div className={styles.metrics} aria-label="今日获客指标">
        {metrics
          .filter(
            ([k]) =>
              !k.startsWith("event:") ||
              k ===
                (selected.startsWith("event:")
                  ? selected
                  : metrics.find(([id]) => id.startsWith("event:"))?.[0]),
          )
          .map(([k, n]) => (
            <button
              key={k}
              title={
                k === "devices"
                  ? "启用后按匿名浏览器每日去重，历史点击仅计入 PV"
                  : k === "site_devices"
                    ? "按北京时间，同一匿名浏览器每天计为一台设备"
                    : undefined
              }
              aria-pressed={selected === k}
              onClick={() => setMetric(k)}
            >
              <span>{n} · 今日</span>
              <strong>
                {value(today, k)?.toLocaleString("zh-CN") ??
                  (today?.report_state === "failed" ? "待恢复" : today?.report_state === "pending" ? "待汇总" : today ? "未采集" : "—")}
              </strong>
            </button>
          ))}
      </div>
      {today && (updatedAt || today.report_state === "pending" || today.report_state === "failed") &&
        <small role="status">{today.report_state === "failed" ? "部分数据处理失败" :
          today.report_state === "pending" ? "有数据待汇总" : "汇总完成"}
          {updatedAt && ` · 最近汇总 ${updatedAt}`}</small>}
      </div><div>
      <div className={styles.chartHead}>
        <span>{metrics.find(([k]) => k === selected)?.[1]}趋势</span>
        {pixel?.installed && Object.keys(data?.events || {}).length > 1 && (
          <PlatformFilter
            title="转化事件"
            icons={false}
            value={selected}
            onChange={setMetric}
            options={Object.entries(data?.events || {}).map(([k, v]) => [
              `event:${k}`,
              String(v),
            ])}
          />
        )}
        <PlatformFilter
          title="趋势范围"
          icons={false}
          value={String(range)}
          onChange={(v) => setRange(Number(v))}
          options={[
            ["7", "近 7 日"],
            ["30", "近 30 日"],
          ]}
        />
      </div>
      {error && !data ? (
        <button onClick={refresh}>数据暂不可用 · 重试</button>
      ) : (
        <div className={styles.plot}>
          <svg
            viewBox="0 0 400 100"
            preserveAspectRatio="none"
            role="img"
            aria-label={`近 ${range} 日趋势，最高 ${Math.max(0, ...values.map((v) => v ?? 0))}`}
          >
            {[9, 49, 89].map((y) => (
              <line key={y} x1="0" x2="400" y1={y} y2={y} />
            ))}
            <path d={path} />
          </svg>
          {!values.some((v) => v != null) && (
            <span>{data ? "暂无可计量数据" : "读取中…"}</span>
          )}
        </div>
      )}
      {error && data && <button className="text-button" onClick={refresh}>趋势更新失败，显示上次结果 · 重试</button>}
      <div className={styles.axis}>
        <span>{rows[0]?.day.slice(5)}</span>
        <span>{today?.day.slice(5)}</span>
      </div>
      </div><div className={styles.setup}><h3>数据接入</h3>
        <div><span>网站像素 <small>{!pixel ? "待核实" : pixel.installed ? "已验证" : pixel.embedded ? "待检测" : "待安装"}</small></span>
          <button className="text-button" onClick={()=>setTask(pixel?.embedded && !pixel.installed ? "detect" : "embed")}>{copied&&!pixel?.installed?"继续安装":"安装指引"} ↗</button></div>
        <div><span>业务事件 <small>{counts ? counts.total ? `${counts.tested} / ${counts.total} 已验证` : "待配置" : "待核实"}</small></span>
          <button className="text-button" onClick={setup}>{resumeEvent?`继续验证${resumeEvent}`:"配置指引"} ↗</button></div>
      </div></div>
      <footer>
        <button className="text-button" onClick={() => setDetails(true)}>
          数据明细 ↗
        </button>
      </footer>
      {details && (
        <DialogPanel
          title="获客明细"
          size="wide"
          onClose={() => setDetails(false)}
        >
          <AnalyticsSummary
            initialChannel={channel}
            aid={agent.id}
            readOnly={readOnly}
            promotion={{
              destination_url:
                promotion?.destination_url || agent.business?.product_url || "",
              configured: !!promotion?.destination_url,
              revision: promotion?.revision || 0,
            }}
            onSetup={() => {
              setDetails(false);
              setup();
            }}
          />
        </DialogPanel>
      )}
      <AnalyticsPanel
        key={`pixel:${agent.id}`}
        aid={agent.id}
        url={agent.business?.product_url}
        readOnly={readOnly}
        task={task}
        onTask={setTask}
        onData={setPixelData}
        onResumeEvent={setResumeEvent}
        onCodeCopied={() => setCopied(true)}
      />
    </section>
  );
}
