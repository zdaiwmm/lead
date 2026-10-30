"use client";
import { useEffect, useId, useState } from "react";
import { Code2, ArrowUpRight } from "lucide-react";
import AnalyticsPanel, { PixelTask } from "./analytics-panel";
import { api } from "./api-client";
import PromotionLink from "./promotion-link";
import SocialIcon, { socialNames } from "./social-icon";
type Row = Record<string, any>;
export default function AgentResults({
  agent,
  readOnly,
}: {
  agent: Row;
  readOnly: boolean;
}) {
  const [task, setTask] = useState<PixelTask>(null),
    [pixelData, setPixelData] = useState<Row | null>(null),
    [trend, setTrend] = useState<Row | null>(null),
    [error, setError] = useState("");
  const gradient = useId().replaceAll(":", "");
  const connectedPlatforms = [
    ...new Set<string>(
      (agent.accounts || [])
        .filter(
          (account: Row) =>
            account.state === "connected" && socialNames[account.platform],
        )
        .map((account: Row) => account.platform),
    ),
  ]
    .sort()
    .join(",");
  useEffect(() => {
    let active = true;
    const load = () =>
      api(`/agents/${agent.id}/analytics/trend`)
        .then((d) => {
          if (active) {
            setTrend(d);
            setError("");
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    load();
    const timer = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [agent.id, connectedPlatforms]);
  const m = agent.metrics,
    covered = m.coverage === "connected";
  const series: Row[] = trend?.days || [];
  const channels: Row[] = (trend?.channels || []).filter(
    (channel: Row) =>
      socialNames[channel.platform] &&
      agent.accounts?.some(
        (account: Row) =>
          account.platform === channel.platform &&
          account.state === "connected",
      ),
  );
  const max = Math.max(2, ...series.map((d) => d.qualified_visits));
  const top = Math.ceil(max / 2) * 2;
  const points = series
    .map(
      (d, i) =>
        `${40 + i * (700 / Math.max(1, series.length - 1))},${164 - (d.qualified_visits / top) * 144}`,
    )
    .join(" ");
  const pixel =
    pixelData && pixelData.agent_id === agent.id ? pixelData.pixel : null;
  const panel = (
    <AnalyticsPanel
      key={agent.id}
      aid={agent.id}
      url={agent.business?.product_url}
      readOnly={readOnly}
      task={task}
      onTask={setTask}
      onData={setPixelData}
    />
  );
  if (!pixel?.completed) {
    const step = pixel?.installed ? 2 : pixel?.embedded ? 1 : 0;
    const labels = ["嵌入代码", "安装检测", "事件测试"];
    return (
      <section
        className="workspace-section results-section"
        aria-labelledby="results-heading"
      >
        <div className="section-head">
          <h2 id="results-heading">获客效果</h2>
          <span className="muted small">
            {pixelData ? (step ? step + " / 3" : "未接入") : "正在读取…"}
          </span>
        </div>
        <ol className="pixel-onboarding-progress" aria-label="像素接入进度">
          {labels.map((label, index) => (
            <li
              key={label}
              className={index < step ? "done" : index === step ? "active" : ""}
              aria-current={index === step ? "step" : undefined}
            >
              <span>{index < step ? "✓" : index + 1}</span>
              {label}
            </li>
          ))}
        </ol>
        <div className="pixel-current-step">
          <h3>{["安装网站像素", "检测像素安装", "设置网站事件"][step]}</h3>
          <button
            className="primary"
            aria-haspopup="dialog"
            onClick={() =>
              setTask((["embed", "detect", "events"] as const)[step])
            }
          >
            {readOnly
              ? "查看接入状态"
              : [
                  pixelData?.connection ? "查看像素代码" : "获取像素代码",
                  "检测安装",
                  "事件设置与测试",
                ][step]}
          </button>
        </div>
        {panel}
      </section>
    );
  }
  return (
    <section
      className="workspace-section results-section"
      aria-labelledby="results-heading"
    >
      <div className="section-head">
        <div className="heading-with-meta">
          <h2 id="results-heading">获客效果</h2>
          <span className="muted small">近 30 天</span>
        </div>
        <details className="pixel-settings-menu">
          <summary>像素设置 ↗</summary>
          <div>
            {(
              [
                ["embed", "嵌入指引"],
                ["detect", "安装检测"],
                ["events", "事件设置与测试"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                onClick={(e) => {
                  e.currentTarget.closest("details")?.removeAttribute("open");
                  setTask(value);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </details>
      </div>
      <PromotionLink
        aid={agent.id}
        config={
          trend?.promotion || {
            destination_url:
              agent.onboarding_state?.promotion?.destination_url ||
              agent.business?.product_url ||
              "",
            configured: !!agent.onboarding_state?.promotion?.destination_url,
            revision: agent.onboarding_state?.promotion?.revision || 0,
          }
        }
        readOnly={readOnly}
        onSaved={(promotion) =>
          setTrend((previous) =>
            previous ? { ...previous, promotion } : { promotion },
          )
        }
      />
      <dl className="results-metrics pixel-results-metrics">
        {[
          ["独立访客", "visitors"],
          ["访问会话", "sessions"],
        ].map(([label, key]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{pixel.report[key]?.toLocaleString() ?? "—"}</dd>
          </div>
        ))}
        <div>
          <dt>
            <button
              className="pixel-event-metric"
              aria-label="事件，查看各事件详细数据"
              onClick={() => setTask("details")}
            >
              <span>
                事件 <ArrowUpRight size={13} />
              </span>
              <strong aria-hidden="true">
                {pixel.report.event_count?.toLocaleString() ?? "—"}
              </strong>
            </button>
          </dt>
          <dd className="pixel-sr-only">
            {pixel.report.event_count?.toLocaleString() ?? "—"}
          </dd>
        </div>
      </dl>
      <div className="trend-chart">
        {covered && series.length && !error ? (
          <svg
            viewBox="0 0 760 200"
            role="img"
            aria-label={`近 30 天有效访问趋势，合计 ${m.qualified_visits} 次`}
          >
            <defs>
              <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="var(--data)" stopOpacity=".1" />
                <stop offset="1" stopColor="var(--data)" stopOpacity="0" />
              </linearGradient>
            </defs>
            {[0, 0.5, 1].map((v) => (
              <g key={v}>
                <line
                  x1="40"
                  x2="740"
                  y1={164 - v * 144}
                  y2={164 - v * 144}
                  className="chart-grid"
                />
                <text x="0" y={168 - v * 144}>
                  {top * v}
                </text>
              </g>
            ))}
            <path
              d={`M40,164 L${points.replaceAll(" ", " L")} L740,164 Z`}
              fill={`url(#${gradient})`}
            />
            <polyline
              points={points}
              fill="none"
              stroke="var(--data)"
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
            />
            {[0, Math.floor(series.length / 2), series.length - 1].map(
              (i, n) => (
                <text
                  key={n}
                  x={40 + i * (700 / (series.length - 1))}
                  y="194"
                  textAnchor={n === 0 ? "start" : n === 2 ? "end" : "middle"}
                >
                  {series[i].date.slice(5).replace("-", "/")}
                </text>
              ),
            )}
          </svg>
        ) : (
          <div className="chart-empty">
            <Code2 size={22} />
            <p>
              {error
                ? "趋势暂时无法读取"
                : covered
                  ? "正在读取访问趋势"
                  : m.coverage === "stale"
                    ? "最近 24 小时未收到网站回传"
                    : "让每一次有效访问都有迹可循"}
            </p>
            <span className="muted small">
              {error ||
                (covered
                  ? "请稍候"
                  : "接入网站像素后，查看 Agent 带来的真实访问趋势")}
            </span>
            {!covered && (
              <button className="text-button" onClick={() => setTask("embed")}>
                配置网站像素 <ArrowUpRight size={13} />
              </button>
            )}
          </div>
        )}
      </div>
      <div className="coverage-note">
        <span>有效访问：来自推广链接，且前台活跃至少 10 秒</span>
        <span>
          <i className={`dot ${covered ? "green" : ""}`} />
          {covered
            ? "像素已接入"
            : m.coverage === "stale"
              ? "像素回传待检查"
              : "像素未接入"}
        </span>
      </div>
      {channels.length > 0 && (
        <div className="channel-results">
          <div className="channel-results-heading">
            <h3>社媒明细</h3>
            <span className="muted small">{channels.length} 个已连接</span>
          </div>
          <div
            className="channel-results-scroll"
            role="region"
            aria-label="已连接社媒的获客明细，可滚动查看"
            tabIndex={0}
          >
            <table className="channel-results-table">
              <thead>
                <tr>
                  <th scope="col">社媒</th>
                  <th scope="col">有效访问</th>
                  <th scope="col">访问会话</th>
                  <th scope="col" title="仅统计经过推广跳转链接的点击">
                    推广点击
                  </th>
                  <th scope="col">累计发布</th>
                </tr>
              </thead>
              <tbody>
                {channels.map((channel) => (
                  <tr key={channel.platform}>
                    <th scope="row">
                      <span className="channel-result-name">
                        <SocialIcon platform={channel.platform} size={15} />
                        {socialNames[channel.platform]}
                      </span>
                    </th>
                    {["qualified_visits", "visits", "clicks", "published"].map(
                      (key) => (
                        <td
                          key={key}
                          title={
                            channel[key] == null
                              ? key === "conversions"
                                ? "转化回传未接入或已过期"
                                : "网站回传未接入或已过期"
                              : undefined
                          }
                        >
                          {channel[key]?.toLocaleString() ?? "—"}
                        </td>
                      ),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {!!connectedPlatforms && !channels.length && (
        <p className="muted small promotion-hint" role="status">
          {error ? "社媒明细暂时无法读取，稍后自动重试。" : "正在读取社媒明细…"}
        </p>
      )}
      {panel}
    </section>
  );
}
