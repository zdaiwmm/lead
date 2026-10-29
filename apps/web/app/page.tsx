"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Plus,
  LayoutGrid,
  MessageSquare,
  ChevronRight,
  Settings,
  Pause,
  Play,
  Globe,
  X,
  RefreshCw,
  Check,
  ExternalLink,
  PanelLeftOpen,
} from "lucide-react";
import AnalyticsPanel from "./analytics-panel";
import BrowserHistory from "./browser-history";
import ChannelCards from "./channel-cards";
import { MediaUpload, AssetPreview } from "./media-upload";

type Row = Record<string, any>;
type View = "home" | "agent" | "settings";
type Tab = "chat" | "work" | "results" | "settings";
const names: Row = {
  draft: "待启动",
  running: "自主运营中",
  paused: "已暂停",
  archived: "已归档",
  working: "正在工作",
  queued: "等待执行",
  needs_auth: "等待账号授权",
  needs_attention: "需要处理",
  degraded: "暂时异常",
  ready: "准备就绪",
  stopped: "已暂停",
  pending: "待连接",
  connected: "已连接",
  complete: "完成",
  failed: "未完成",
  succeeded: "已完成",
  reconciling: "核对结果中",
  canceled: "已取消",
  planned: "待执行",
};
function status(a: Row) {
  return a.desired_state === "running"
    ? names[a.health_state] || names.running
    : names[a.desired_state] || "待启动";
}
function time(s: string) {
  return new Date(s).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
async function api(path: string, body?: Row) {
  const r = await fetch("/api" + path, {
    method: body ? "POST" : "GET",
    headers: body
      ? { "Content-Type": "application/json", "X-Lead-Request": "1" }
      : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: "连接暂时不可用" }));
    throw Error(typeof e.detail === "string" ? e.detail : "操作未完成");
  }
  return r.json();
}
export default function Lead() {
  const [view, setView] = useState<View>("home");
  const [tab, setTab] = useState<Tab>("chat");
  const [agents, setAgents] = useState<Row[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);
  const [health, setHealth] = useState<Row | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Row[]>([]);
  const [channels, setChannels] = useState(false);
  const [platforms, setPlatforms] = useState<Row[]>([]);
  const [browser, setBrowser] = useState<Row | null>(null);
  const [image, setImage] = useState("");
  const [input, setInput] = useState("");
  const [side, setSide] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const objectUrl = useRef("");
  const sendKey = useRef<string | null>(null);
  const createKey = useRef<string | null>(null);
  const selected = useRef<string | null>(null);
  selected.current = current;
  const refresh = useCallback(async () => {
    const [a, h] = await Promise.all([api("/agents"), api("/health")]);
    setAgents(a);
    setHealth(h);
    const id = selected.current;
    if (id) {
      const d = await api("/agents/" + id);
      if (id === selected.current) setDetail(d);
    }
  }, []);
  useEffect(() => {
    let active = true;
    api("/session", {})
      .then(() => refresh())
      .then(() => {
        if (active) setReady(true);
      })
      .catch((e) => setError(e.message));
    return () => {
      active = false;
    };
  }, [refresh]);
  useEffect(() => {
    if (!ready) return;
    const events = new EventSource("/api/events");
    events.onmessage = () => refresh().catch(() => {});
    events.onopen = () => refresh().catch(() => {});
    const timer = setInterval(() => refresh().catch(() => {}), 15000);
    return () => {
      events.close();
      clearInterval(timer);
    };
  }, [ready, refresh]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [detail?.messages?.length]);
  useEffect(() => {
    if (!channels) return;
    api("/platforms")
      .then(setPlatforms)
      .catch((e) => setError(e.message));
  }, [channels]);
  async function run(action: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await action();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作未完成");
    } finally {
      setBusy(false);
    }
  }
  async function select(id: string) {
    selected.current = id;
    setCurrent(id);
    setDetail(null);
    setView("agent");
    setTab("chat");
    setSide(false);
    setText("");
    setAttachments([]);
    setError("");
    try {
      const d = await api("/agents/" + id);
      if (selected.current === id) setDetail(d);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function create(initial?: string) {
    createKey.current = createKey.current || crypto.randomUUID();
    const a = await api("/agents", { client_id: createKey.current });
    createKey.current = null;
    selected.current = a.id;
    setAttachments([]);
    setCurrent(a.id);
    setView("agent");
    setTab("chat");
    setSide(false);
    if (initial) {
      sendKey.current = sendKey.current || crypto.randomUUID();
      await api("/agents/" + a.id + "/messages", {
        text: initial,
        client_message_id: sendKey.current,
      });
      sendKey.current = null;
    }
    setText("");
    setDetail(await api("/agents/" + a.id));
  }
  async function send() {
    if ((!text.trim() && !attachments.length) || busy) return;
    const value = text.trim() || "我上传了素材，供后续运营使用。";
    await run(async () => {
      if (!current) {
        await create(value);
        return;
      }
      sendKey.current = sendKey.current || crypto.randomUUID();
      await api("/agents/" + current + "/messages", {
        text: value,
        client_message_id: sendKey.current,
        asset_ids: attachments.map((a) => a.id),
      });
      sendKey.current = null;
      if (selected.current === current) {
        setText("");
        setAttachments([]);
      }
    });
  }
  async function control(action: string) {
    if (!current) return;
    await run(async () => {
      await api("/agents/" + current + "/control", { action });
    });
  }
  async function shot() {
    if (!current) return;
    const r = await fetch("/api/agents/" + current + "/browser/screenshot", {
      cache: "no-store",
    });
    if (!r.ok) throw Error("浏览器画面暂不可用");
    const url = URL.createObjectURL(await r.blob());
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    objectUrl.current = url;
    setImage(url);
  }
  async function connect(p: Row) {
    if (!current) return;
    await run(async () => {
      const b = await api("/agents/" + current + "/connect", {
        platform: p.id,
      });
      setBrowser({ ...b, name: p.name });
      setChannels(false);
      await shot();
    });
  }
  async function browserInput(body: Row) {
    if (!current) return;
    await api("/agents/" + current + "/browser/input", body);
    await shot();
  }
  useEffect(() => {
    if (!browser) return;
    const timer = setInterval(() => shot().catch(() => {}), 3000);
    return () => clearInterval(timer);
  }, [browser, current]); // eslint-disable-line react-hooks/exhaustive-deps
  const running = agents.filter((a) => a.desired_state === "running").length;
  const measured = agents.filter((a) => a.metrics?.qualified_visits != null);
  const needs = agents.filter((a) =>
    ["needs_auth", "needs_attention", "degraded"].includes(a.health_state),
  ).length;
  const pending = detail?.messages?.some(
    (m: Row) =>
      m.role === "user" && ["pending", "processing"].includes(m.job_status),
  );
  function composer(first = false) {
    return (
      <div className={"composer " + (first ? "first" : "")}>
        {!!attachments.length && (
          <div className="attachment-list">
            {attachments.map((asset) => (
              <div key={asset.id}>
                <AssetPreview
                  aid={current!}
                  assetId={asset.id}
                  kind={asset.metadata.kind}
                />
                <button
                  className="icon"
                  aria-label="移除此素材"
                  onClick={() =>
                    setAttachments(attachments.filter((a) => a.id !== asset.id))
                  }
                >
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          aria-label={first ? "产品网址或介绍" : "发消息给 Agent"}
          placeholder={
            first ? "输入产品网址，或简单介绍你的业务" : "说说你的想法…"
          }
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            sendKey.current = null;
          }}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              send();
            }
          }}
          rows={first ? 3 : 2}
        />
        <div className="composer-bottom">
          <span>
            {current && (
              <MediaUpload
                aid={current}
                disabled={busy}
                onError={(message) => {
                  if (selected.current === current) setError(message);
                }}
                onUpload={(asset) => {
                  if (selected.current !== current) return;
                  setAttachments((old) =>
                    old.some((a) => a.id === asset.id) ? old : [...old, asset],
                  );
                  sendKey.current = null;
                }}
              />
            )}
          </span>
          <button
            aria-label="发送"
            className="send"
            disabled={(!text.trim() && !attachments.length) || busy || !ready}
            onClick={send}
          >
            <ArrowUp size={18} />
          </button>
        </div>
      </div>
    );
  }
  function connections() {
    return (
      <section className="connections">
        <div className="section-head">
          <h2>运营账号</h2>
          <button onClick={() => setChannels(true)}>
            <Plus size={14} />
            连接账号
          </button>
        </div>
        {detail?.accounts?.filter((a: Row) => a.platform !== "lab").length ? (
          detail.accounts
            .filter((a: Row) => a.platform !== "lab")
            .map((a: Row) => (
              <div className="simple-row" key={a.id}>
                <span>
                  {platforms.find((p) => p.id === a.platform)?.name ||
                    a.platform}
                </span>
                <span className="muted">{names[a.state] || a.state}</span>
              </div>
            ))
        ) : (
          <p className="muted small">尚未连接账号</p>
        )}
      </section>
    );
  }
  return (
    <div className="shell">
      <aside className={"sidebar " + (side ? "open" : "")}>
        <button
          className="wordmark"
          onClick={() => {
            setView("home");
            setSide(false);
          }}
        >
          lead<span>·</span>
        </button>
        <button
          className="new"
          onClick={() => run(() => create())}
          disabled={busy || !ready}
        >
          <Plus size={16} />
          新建 Agent
        </button>
        <nav>
          <button
            className={view === "home" ? "active" : ""}
            onClick={() => {
              setView("home");
              setCurrent(null);
              setDetail(null);
              setText("");
              setSide(false);
            }}
          >
            <LayoutGrid size={17} />
            概览
          </button>
          <div className="nav-label">
            我的 Agent <span>{agents.length}</span>
          </div>
          {agents.map((a) => (
            <button
              key={a.id}
              className={
                current === a.id && view === "agent"
                  ? "active agent-nav"
                  : "agent-nav"
              }
              onClick={() => select(a.id)}
            >
              <span
                className={
                  "dot " + (a.desired_state === "running" ? "green" : "")
                }
              />
              <span className="ellipsis">{a.name}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button
            className={view === "settings" ? "active" : ""}
            onClick={() => {
              setView("settings");
              setSide(false);
            }}
          >
            <Settings size={16} />
            设置
          </button>
          <div className="local">
            <span
              className={
                "dot " + (health?.worker && health?.browser ? "green" : "")
              }
            />
            本地工作空间
          </div>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button
            className="icon mobile"
            aria-label="打开导航"
            onClick={() => setSide(!side)}
          >
            <PanelLeftOpen size={19} />
          </button>
          <span className="breadcrumb">
            {view === "home" ? (
              "工作空间"
            ) : view === "settings" ? (
              "工作空间 / 设置"
            ) : (
              <>
                我的 Agent <ChevronRight size={13} />
                {detail?.name || "Agent"}
              </>
            )}
          </span>
          <span className="top-right">
            {health?.worker ? "服务已连接" : "正在连接服务"}
            <span className={"dot " + (health?.worker ? "green" : "")} />
          </span>
        </header>
        {error && (
          <div className="error" role="alert">
            <span>{error}</span>
            <button
              aria-label="关闭提示"
              className="icon"
              onClick={() => setError("")}
            >
              <X size={15} />
            </button>
          </div>
        )}
        {!ready && !error ? (
          <div className="loading">
            <div />
            <div />
            <div />
          </div>
        ) : view === "home" ? (
          <main className={"home " + (!agents.length ? "empty" : "")}>
            {!agents.length ? (
              <div className="welcome">
                <div className="brand-icon">
                  l<span>·</span>
                </div>
                <div className="eyebrow">YOUR AI SOCIAL TEAM</div>
                <h1>你想推广什么？</h1>
                <p>把业务告诉我，剩下的我们一起开始。</p>
                {composer(true)}
              </div>
            ) : (
              <>
                <div className="page-head">
                  <div>
                    <p className="eyebrow">WORKSPACE</p>
                    <h1>你的运营团队</h1>
                  </div>
                  <button
                    className="primary"
                    onClick={() => run(() => create())}
                  >
                    <Plus size={16} />
                    雇用 Agent
                  </button>
                </div>
                <div className="overview-line">
                  <span>
                    <strong>{running}</strong> 正在运营
                  </span>
                  <span>
                    <strong>{needs}</strong> 需要处理
                  </span>
                  <span>
                    <strong>
                      {measured.length
                        ? measured.reduce(
                            (sum, a) => sum + a.metrics.qualified_visits,
                            0,
                          )
                        : "—"}
                    </strong>{" "}
                    有效访问
                  </span>
                </div>
                <div className="agent-table">
                  <div className="table-head">
                    <span>运营人员</span>
                    <span>状态</span>
                    <span>最近工作</span>
                    <span />
                  </div>
                  {agents.map((a) => (
                    <button
                      className="agent-row"
                      onClick={() => select(a.id)}
                      key={a.id}
                    >
                      <span className="agent-identity">
                        <span className="avatar">{a.name.slice(0, 1)}</span>
                        <span>
                          <strong>{a.name}</strong>
                          <small>
                            {a.product_url
                              ? new URL(a.product_url).hostname
                              : a.description?.slice(0, 35) ||
                                "等待了解你的业务"}
                          </small>
                        </span>
                      </span>
                      <span className="status">
                        <i
                          className={
                            "dot " +
                            (a.desired_state === "running" ? "green" : "")
                          }
                        />
                        {status(a)}
                      </span>
                      <span className="muted">
                        {a.latest_work?.title || "等待开始"}
                      </span>
                      <ChevronRight size={16} />
                    </button>
                  ))}
                </div>
              </>
            )}
          </main>
        ) : view === "settings" ? (
          <main className="settings-page">
            <p className="eyebrow">WORKSPACE</p>
            <h1>设置</h1>
            <section>
              <h2>模型服务</h2>
              <div className="setting-row">
                <span>DeepSeek</span>
                <span className="pill">
                  {health?.model_configured ? "已配置" : "等待配置"}
                </span>
              </div>
              <p className="muted">
                在项目 .env 中填写 DEEPSEEK_API_KEY 后即可使用。
              </p>
              <div className="setting-row">
                <span>模型</span>
                <code>{health?.model || "deepseek-flash"}</code>
              </div>
            </section>
            <section>
              <h2>运行服务</h2>
              {[
                ["database", "数据库"],
                ["worker", "任务调度"],
                ["browser", "浏览器"],
              ].map(([k, label]) => (
                <div className="setting-row" key={k}>
                  <span>{label}</span>
                  <span className="status">
                    <i className={"dot " + (health?.[k] ? "green" : "")} />
                    {health?.[k] ? "正常" : "未连接"}
                  </span>
                </div>
              ))}
              <div className="setting-row">
                <span>同时运行的浏览器</span>
                <span>
                  {health?.active_profiles || 0} /{" "}
                  {health?.browser_capacity || 3}
                </span>
              </div>
            </section>
          </main>
        ) : (
          <>
            <div className="agent-header">
              <div>
                <h1>{detail?.name || "Agent"}</h1>
                <span className="status">
                  <i
                    className={
                      "dot " +
                      (detail?.desired_state === "running" ? "green" : "")
                    }
                  />
                  {detail ? status(detail) : "正在读取"}
                </span>
              </div>
              <div className="agent-actions">
                <button
                  onClick={() => setChannels(true)}
                  disabled={!detail || busy}
                >
                  <Plus size={15} />
                  连接账号
                </button>
                {detail?.desired_state === "running" ? (
                  <button onClick={() => control("pause")} disabled={busy}>
                    <Pause size={14} />
                    暂停
                  </button>
                ) : (
                  <button
                    className="primary"
                    onClick={() =>
                      control(
                        detail?.desired_state === "paused" ? "resume" : "start",
                      )
                    }
                    disabled={busy || !detail}
                  >
                    <Play size={14} />
                    {detail?.desired_state === "paused" ? "恢复" : "免费启动"}
                  </button>
                )}
              </div>
            </div>
            <div className="tabs" role="tablist" aria-label="Agent 页面">
              {(
                [
                  ["chat", "对话"],
                  ["work", "工作记录"],
                  ["results", "获客效果"],
                  ["settings", "更多"],
                ] as [Tab, string][]
              ).map(([key, label]) => (
                <button
                  role="tab"
                  aria-selected={tab === key}
                  key={key}
                  onClick={() => setTab(key)}
                  className={tab === key ? "selected" : ""}
                >
                  {label}
                </button>
              ))}
            </div>
            {!detail ? (
              <div className="loading">
                <div />
                <div />
              </div>
            ) : tab === "chat" ? (
              <main className="chat">
                <div className="messages">
                  {detail.messages.map((m: Row) => (
                    <div className={"message " + m.role} key={m.id}>
                      {m.role === "assistant" && (
                        <div className="assistant-mark">
                          l<span>·</span>
                        </div>
                      )}
                      <div className="message-content">
                        <div className="bubble">{m.content.text}</div>
                        {!!m.content.asset_ids?.length && (
                          <div className="attachment-list">
                            {m.content.asset_ids.map((id: string) => (
                              <AssetPreview
                                key={id}
                                aid={current!}
                                assetId={id}
                                kind={
                                  detail.assets?.find((a: Row) => a.id === id)
                                    ?.metadata.kind
                                }
                              />
                            ))}
                          </div>
                        )}
                        {!!m.source_refs?.length && (
                          <BrowserHistory
                            aid={current!}
                            sources={m.source_refs}
                          />
                        )}
                        {m.content.cards?.includes("connect") && (
                          <button
                            className="inline-action"
                            onClick={() => setChannels(true)}
                          >
                            <Globe size={17} />
                            连接运营账号
                            <ChevronRight size={15} />
                          </button>
                        )}
                        <ChannelCards
                          aid={current!}
                          onComplete={refresh}
                          cards={m.content.cards || []}
                          states={detail.cards || []}
                          busy={busy}
                          start={() => control("start")}
                          respond={(card, accepted) =>
                            run(async () => {
                              await api(
                                "/agents/" +
                                  current +
                                  "/cards/" +
                                  card.id +
                                  "/respond",
                                { version: card.version, accepted },
                              );
                            })
                          }
                        />
                        {m.error_code && (
                          <div className="message-error">
                            {(
                              {
                                MODEL_NOT_CONFIGURED: "等待配置 DeepSeek 密钥",
                                MODEL_AUTH_FAILED: "DeepSeek 密钥验证失败",
                                MODEL_CREDITS_REQUIRED: "DeepSeek 账户余额不足",
                                MODEL_RATE_LIMITED: "DeepSeek 请求过于频繁",
                                BUDGET_EXCEEDED: "今日模型用量已达上限",
                              } as Row
                            )[m.error_code] || "模型请求未完成"}
                            <button
                              disabled={busy}
                              onClick={() =>
                                run(async () => {
                                  await api(
                                    "/agents/" + current + "/retry",
                                    {},
                                  );
                                })
                              }
                            >
                              <RefreshCw size={12} />
                              重试
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                  {pending && (
                    <div className="thinking">
                      <span />
                      {detail.browser_runs?.some(
                        (r: Row) => r.state === "running",
                      ) && <small>正在浏览网页</small>}
                      <span />
                      <span />
                    </div>
                  )}
                  <div ref={end} />
                </div>
                <div className="chat-input">{composer()}</div>
              </main>
            ) : tab === "work" ? (
              <main className="content-page">
                <div className="section-head">
                  <h2>近期工作</h2>
                  <span className="muted small">
                    {detail.activity.length} 条
                  </span>
                </div>
                {detail.activity.length ? (
                  detail.activity.map((a: Row) => (
                    <div className="activity-row" key={a.id}>
                      <span className="activity-check">
                        <Check size={14} />
                      </span>
                      <div>
                        <strong>{a.title}</strong>
                        {a.detail && <p>{a.detail}</p>}
                      </div>
                      <time>{time(a.created_at)}</time>
                    </div>
                  ))
                ) : (
                  <div className="empty-section">
                    <MessageSquare size={24} />
                    <p>开始运营后，工作会记录在这里。</p>
                  </div>
                )}
                {detail.tasks.length > 0 && (
                  <section>
                    <h2>任务</h2>
                    {detail.tasks.map((t: Row) => (
                      <div className="simple-row" key={t.id}>
                        <span>
                          {t.type === "lab.publish" ? "本地测试发布" : t.type}
                        </span>
                        <span className="muted">
                          {names[t.state] || t.state}
                        </span>
                      </div>
                    ))}
                  </section>
                )}
              </main>
            ) : tab === "results" ? (
              <main className="content-page">
                <div className="section-head">
                  <h2>带来的访问</h2>
                  <span className="muted small">近 30 天</span>
                </div>
                <div className="metrics">
                  {[
                    ["有效访问", "qualified_visits"],
                    ["访问人数", "visits"],
                    ["转化", "conversions"],
                  ].map(([label, key]) => (
                    <div key={key}>
                      <span>{label}</span>
                      <strong>{detail.metrics[key] ?? "—"}</strong>
                    </div>
                  ))}
                </div>
                {detail.metrics.coverage === "not_connected" && (
                  <p className="muted">尚未接入访问统计</p>
                )}
                <div className="simple-row">
                  <span>推广链接点击</span>
                  <strong>{detail.metrics.clicks}</strong>
                </div>
                <div className="simple-row">
                  <span>已确认的社媒发布</span>
                  <strong>{detail.metrics.published}</strong>
                </div>
                <AnalyticsPanel
                  aid={current!}
                  url={detail.business?.product_url}
                  request={api}
                  onError={(message) => {
                    if (selected.current === current) setError(message);
                  }}
                />
              </main>
            ) : (
              <main className="content-page">
                {connections()}
                <section>
                  <h2>业务资料</h2>
                  {detail.business ? (
                    <>
                      <p>{detail.business.description}</p>
                      {detail.business.product_url && (
                        <a
                          href={detail.business.product_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {detail.business.product_url}
                          <ExternalLink size={12} />
                        </a>
                      )}
                    </>
                  ) : (
                    <p className="muted">在对话中介绍你的业务</p>
                  )}
                </section>
                <section>
                  <h2>运营偏好</h2>
                  {detail.memory.length ? (
                    detail.memory.map((m: Row) => (
                      <div className="simple-row" key={m.id}>
                        {m.body.text}
                        <button
                          className="icon"
                          aria-label="移除此偏好"
                          disabled={busy}
                          onClick={() =>
                            run(async () => {
                              await api(
                                `/agents/${current}/memory/${m.id}/remove`,
                                { expected_revision: detail.config_revision },
                              );
                            })
                          }
                        >
                          <X size={14} />
                        </button>
                      </div>
                    ))
                  ) : (
                    <p className="muted">直接在对话中告诉我</p>
                  )}
                </section>
                <details className="advanced">
                  <summary>
                    高级设置
                    <ChevronRight size={14} />
                  </summary>
                  <div className="advanced-content">
                    <div className="setting-row">
                      <span>浏览器</span>
                      <code>{detail.profile.provider}</code>
                    </div>
                    <div className="setting-row">
                      <span>环境</span>
                      <span>{detail.profile.id.slice(0, 8)}</span>
                    </div>
                    <div className="setting-row">
                      <span>网络出口</span>
                      <span>
                        {detail.network.state === "verified"
                          ? "已验证"
                          : "未验证独立 IP"}
                      </span>
                    </div>
                    <button
                      disabled={busy}
                      onClick={() =>
                        run(async () => {
                          const checked = await api(
                            "/agents/" + current + "/identity/check",
                            {},
                          );
                          if (!checked.verified)
                            setError("当前环境尚未通过固定独立出口验证");
                        })
                      }
                    >
                      检查环境
                    </button>
                    <details className="lab">
                      <summary>本地浏览器测试</summary>
                      <p className="muted small">
                        测试站使用真实浏览器，不计入运营和获客数据。
                      </p>
                      <button
                        disabled={busy}
                        onClick={() =>
                          run(async () => {
                            await api(
                              "/agents/" + current + "/lab/connect",
                              {},
                            );
                          })
                        }
                      >
                        授权测试站
                      </button>
                      <button
                        disabled={busy}
                        onClick={() =>
                          run(async () => {
                            await api("/agents/" + current + "/lab/publish", {
                              body:
                                "lead 浏览器验证 " +
                                new Date().toLocaleTimeString(),
                              key: crypto.randomUUID(),
                            });
                          })
                        }
                      >
                        执行测试发布
                      </button>
                      <button
                        disabled={busy}
                        onClick={() =>
                          connect({ id: "lab", name: "本地测试站" })
                        }
                      >
                        打开测试浏览器
                      </button>
                    </details>
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={() =>
                        run(async () => {
                          await api("/agents/" + current + "/control", {
                            action: "archive",
                          });
                          setCurrent(null);
                          setDetail(null);
                          setView("home");
                        })
                      }
                    >
                      归档 Agent
                    </button>
                  </div>
                </details>
              </main>
            )}
          </>
        )}
      </div>
      {channels && (
        <div
          className="drawer-backdrop"
          onClick={() => !busy && setChannels(false)}
        >
          <aside
            className="drawer"
            role="dialog"
            aria-modal="true"
            aria-label="连接账号"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="drawer-head">
              <h2>连接账号</h2>
              <button
                className="icon"
                aria-label="关闭"
                onClick={() => setChannels(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p className="muted small">登录会保存在此 Agent 的浏览器中。</p>
            <div className="platform-list">
              {platforms.map((p) => (
                <button key={p.id} onClick={() => connect(p)} disabled={busy}>
                  <span className="platform-symbol">{p.name[0]}</span>
                  <span>
                    {p.name}
                    <small>
                      {p.automation ? "可自主运营" : "可登录 · 自动运营待接入"}
                    </small>
                  </span>
                  <ChevronRight size={16} />
                </button>
              ))}
            </div>
          </aside>
        </div>
      )}
      {browser && (
        <div
          className="browser-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="账号浏览器"
        >
          <div className="browser-panel">
            <header>
              <span>
                <Globe size={16} />
                {browser.name}
              </span>
              <div>
                {browser.platform !== "lab" && (
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        await api(
                          "/agents/" + current + "/accounts/verify-login",
                          { platform: browser.platform },
                        );
                        setBrowser(null);
                        setImage("");
                      })
                    }
                  >
                    登录完成
                  </button>
                )}
                <button onClick={() => run(shot)} disabled={busy}>
                  <RefreshCw size={14} />
                  刷新画面
                </button>
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      await api("/agents/" + current + "/takeover/end", {});
                      setBrowser(null);
                      setImage("");
                    })
                  }
                >
                  结束接管
                </button>
              </div>
            </header>
            <div className="browser-image">
              {image ? (
                <img
                  src={image}
                  alt="Agent 浏览器当前画面，点击可操作"
                  onClick={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect();
                    const x =
                      ((e.clientX - rect.left) * e.currentTarget.naturalWidth) /
                      rect.width;
                    const y =
                      ((e.clientY - rect.top) * e.currentTarget.naturalHeight) /
                      rect.height;
                    run(() => browserInput({ kind: "click", x, y }));
                  }}
                />
              ) : (
                <p>正在读取浏览器…</p>
              )}
            </div>
            <footer>
              <input
                aria-label="在浏览器中输入"
                value={input}
                placeholder="输入文字"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && input)
                    run(async () => {
                      await browserInput({ kind: "text", text: input });
                      setInput("");
                    });
                }}
              />
              <button
                disabled={busy || !input}
                onClick={() =>
                  run(async () => {
                    await browserInput({ kind: "text", text: input });
                    setInput("");
                  })
                }
              >
                输入
              </button>
              {["Enter", "Tab", "Backspace"].map((k) => (
                <button
                  key={k}
                  disabled={busy}
                  onClick={() =>
                    run(() => browserInput({ kind: "key", text: k }))
                  }
                >
                  {k === "Backspace" ? "删除" : k === "Enter" ? "回车" : "Tab"}
                </button>
              ))}
              <button
                disabled={busy}
                onClick={() =>
                  run(() => browserInput({ kind: "scroll", y: 600 }))
                }
              >
                向下滚动
              </button>
            </footer>
            <p className="browser-note">
              接管期间 Agent 已暂停。登录不会自动开启发布权限。
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
