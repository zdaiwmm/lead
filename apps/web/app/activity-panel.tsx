"use client";
import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  CircleAlert,
  Clock3,
  Check,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import SocialIcon, { socialNames } from "./social-icon";
import AgentAvatar from "./agent-avatar";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";
type Row = Record<string, any>;
function PlatformFilter({
  value,
  onChange,
  history = false,
  disabled = false,
}: {
  value: string;
  onChange: (v: string) => void;
  history?: boolean;
  disabled?: boolean;
}) {
  return (
    <label className="activity-filter">
      {value && value !== "system" && <SocialIcon platform={value} size={14} />}
      <select
        aria-label={history ? "按社媒筛选全部工作记录" : "按社媒筛选工作记录"}
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">全部社媒</option>
        {Object.entries(socialNames).map(([id, name]) => (
          <option key={id} value={id}>
            {name}
          </option>
        ))}
        <option value="system">Agent 系统</option>
      </select>
    </label>
  );
}
function ActivityList({
  rows,
  agent,
  full = false,
}: {
  rows: Row[];
  agent: Row;
  full?: boolean;
}) {
  return (
    <ol className={`activity-list ${full ? "activity-list-full" : ""}`}>
      {rows.map((a) => {
        const failed =
          a.level === "error" ||
          a.task_state === "failed" ||
          /失败|异常|待处理|未完成/.test(a.title);
        const pending = ["running", "working"].includes(a.task_state);
        const waiting = ["planned", "queued", "reconciling"].includes(
          a.task_state,
        );
        return (
          <li key={a.id}>
            <span className="activity-platform">
              {a.platform ? (
                <SocialIcon platform={a.platform} size={18} />
              ) : (
                <AgentAvatar agent={agent} size={22} />
              )}
            </span>
            <div className="activity-copy">
              <p>{a.title}</p>
              {full && a.detail && a.detail !== a.platform && (
                <span className="muted small">{a.detail}</span>
              )}
            </div>
            <span
              className={`activity-indicator ${failed ? "danger-text" : "muted"}`}
              title={
                failed
                  ? "需要处理"
                  : pending
                    ? "执行中"
                    : waiting
                      ? "等待执行"
                      : "已记录"
              }
            >
              {failed ? (
                <CircleAlert size={13} />
              ) : pending ? (
                <LoaderCircle size={13} className="spin" />
              ) : waiting ? (
                <Clock3 size={13} />
              ) : a.task_state === "succeeded" ? (
                <Check size={13} />
              ) : null}
            </span>
            <time
              dateTime={a.created_at}
              title={new Date(a.created_at).toLocaleString("zh-CN")}
            >
              {new Date(a.created_at).toLocaleString("zh-CN", {
                month: "numeric",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </time>
          </li>
        );
      })}
    </ol>
  );
}
function ActivityHistory({
  agent,
  initialPlatform,
  onClose,
}: {
  agent: Row;
  initialPlatform: string;
  onClose: () => void;
}) {
  const [platform, setPlatform] = useState(initialPlatform),
    [rows, setRows] = useState<Row[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [more, setMore] = useState(false),
    [revision, setRevision] = useState(0);
  const [failedOffset, setFailedOffset] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setRows([]);
    api(
      `/agents/${agent.id}/activity?platform=${encodeURIComponent(platform)}&limit=50&offset=0`,
    )
      .then((d) => {
        if (active) {
          setRows(d);
          setMore(d.length === 50);
        }
      })
      .catch((e) => {
        if (active) {
          setError(e.message);
          setFailedOffset(0);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [agent.id, platform, revision]);
  async function next() {
    const offset = error ? failedOffset : rows.length;
    setLoading(true);
    setError("");
    try {
      const d = await api(
        `/agents/${agent.id}/activity?platform=${encodeURIComponent(platform)}&limit=50&offset=${offset}`,
      );
      setRows((old) =>
        offset === 0
          ? d
          : [...old, ...d.filter((a: Row) => !old.some((b) => b.id === a.id))],
      );
      setMore(d.length === 50);
    } catch (e) {
      setError((e as Error).message);
      setFailedOffset(offset);
    } finally {
      setLoading(false);
    }
  }
  return (
    <DialogPanel
      title="全部工作记录"
      description="按社媒查看 Agent 的执行记录，可继续加载更早的工作。"
      size="wide"
      onClose={onClose}
      footer={
        <div className="history-dialog-footer">
          <span className="muted small">已显示 {rows.length} 条记录</span>
          <button onClick={onClose}>关闭</button>
        </div>
      }
    >
      <div className="history-toolbar">
        <PlatformFilter
          value={platform}
          onChange={setPlatform}
          history
          disabled={loading}
        />
        <button
          className="text-button"
          disabled={loading}
          onClick={() => setRevision((n) => n + 1)}
        >
          <RefreshCw size={13} />
          刷新记录
        </button>
      </div>
      {error && (
        <div className="inline-feedback error-feedback" role="alert">
          <span>{error}</span>
          <button disabled={loading} onClick={next}>
            重试
          </button>
        </div>
      )}
      {rows.length ? (
        <ActivityList rows={rows} agent={agent} full />
      ) : !loading && !error ? (
        <p className="muted empty-copy">
          {platform
            ? `${socialNames[platform] || "Agent 系统"} 暂无工作记录。`
            : "暂无工作记录。"}
        </p>
      ) : null}
      {loading && <p className="muted empty-copy">正在读取工作记录…</p>}
      {more && !loading && !error && (
        <div className="history-load-more">
          <button onClick={next}>加载更早记录</button>
        </div>
      )}
    </DialogPanel>
  );
}
export default function ActivityPanel({ agent }: { agent: Row }) {
  const [open, setOpen] = useState(false),
    [platform, setPlatform] = useState(""),
    [filtered, setFiltered] = useState<Row[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!platform) {
      setLoading(false);
      setError("");
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    api(
      `/agents/${agent.id}/activity?platform=${encodeURIComponent(platform)}&limit=50`,
    )
      .then((d) => {
        if (active) setFiltered(d);
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [agent.id, platform, agent.activity?.[0]?.id, retry]);
  const rows = platform ? filtered : agent.activity || [];
  return (
    <>
      <section
        className="workspace-section activity-section"
        aria-labelledby="activity-heading"
      >
        <div className="section-head">
          <div className="heading-with-meta">
            <h2 id="activity-heading">工作记录</h2>
            <span className="muted small">
              <i className="dot green" />
              实时
            </span>
          </div>
          <div className="activity-actions">
            <PlatformFilter value={platform} onChange={setPlatform} />
            <button
              className="text-button"
              aria-haspopup="dialog"
              onClick={() => setOpen(true)}
            >
              查看全部
              <ArrowUpRight size={13} />
            </button>
          </div>
        </div>
        <div className="activity-preview">
          {error ? (
            <div className="inline-feedback error-feedback" role="alert">
              <span>{error}</span>
              <button onClick={() => setRetry((n) => n + 1)}>重试</button>
            </div>
          ) : loading ? (
            <p className="muted empty-copy">正在筛选工作记录…</p>
          ) : rows.length ? (
            <ActivityList rows={rows.slice(0, 3)} agent={agent} />
          ) : (
            <p className="muted empty-copy">
              {platform
                ? `${socialNames[platform] || "Agent 系统"} 暂无工作记录。`
                : "启动运营后，Agent 的工作会实时记录在这里。"}
            </p>
          )}
        </div>
      </section>
      {open && (
        <ActivityHistory
          agent={agent}
          initialPlatform={platform}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}
