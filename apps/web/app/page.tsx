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
  PanelLeftOpen,
} from "lucide-react";
import BrowserHistory from "./browser-history";
import ChannelCards from "./channel-cards";
import { MediaUpload, AssetPreview } from "./media-upload";
import { api, setWorkspace, getWorkspace } from "./api-client";
import WorkspaceSettings from "./workspace-settings";
import UsagePanel from "./usage-panel";
import AgentAvatar from "./agent-avatar";
import AgentIdentity from "./agent-identity";
import AgentPreferences from "./agent-preferences";
import SocialAccounts from "./social-accounts";
import AgentResults from "./agent-results";
import ActivityPanel from "./activity-panel";
import OperatingPanel from "./operating-panel";
import AccountBrowser from "./account-browser";
import SocialIcon from "./social-icon";
import NewAgentDialog from "./new-agent-dialog";
import NotificationIndicator from "./notification-indicator";

type Row = Record<string, any>;
type View = "home" | "agent" | "settings";
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
    ? (["ready", "queued"].includes(a.health_state)
        ? names.running
        : names[a.health_state]) || names.running
    : names[a.desired_state] || "待启动";
}
export default function Lead() {
  const [view, setView] = useState<View>("home");
  const [chatOpen, setChatOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createInitial, setCreateInitial] = useState("");
  const [agents, setAgents] = useState<Row[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);
  const [health, setHealth] = useState<Row | null>(null);
  const [ready, setReady] = useState(false);
  const [loginUrl, setLoginUrl] = useState("");
  const [user, setUser] = useState<Row | null>(null);
  const readOnly = user?.role === "viewer";
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Row[]>([]);
  const [channels, setChannels] = useState(false);
  const [platforms, setPlatforms] = useState<Row[]>([]);
  const [browser, setBrowser] = useState<Row | null>(null);
  const [side, setSide] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const followMessages = useRef(true);
  const sendKey = useRef<string | null>(null);
  const selected = useRef<string | null>(null);
  selected.current = current;
  const refresh = useCallback(async () => {
    const identity = await api("/auth/me");
    if (getWorkspace() && getWorkspace() !== identity.tenant_id) {
      window.location.reload();
      return;
    }
    setWorkspace(identity.tenant_id);
    setUser(identity);
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
      .then(async (session) => {
        if (!active) return;
        if (session.login_url) {
          setLoginUrl(session.login_url);
          return;
        }
        await refresh();
        if (active) {
          setReady(true);
          const saved = new URLSearchParams(window.location.search).get(
            "agent",
          );
          if (saved && /^[a-f0-9-]{36}$/.test(saved)) select(saved);
        }
      })
      .catch((e) => setError(e.message));
    return () => {
      active = false;
    };
  }, [refresh]);
  useEffect(() => {
    const expired = (event: Event) => {
      setLoginUrl((event as CustomEvent).detail);
      setReady(false);
    };
    window.addEventListener("lead:session-expired", expired);
    return () => window.removeEventListener("lead:session-expired", expired);
  }, []);
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
    const box = messagesRef.current;
    if (box && followMessages.current) box.scrollTop = box.scrollHeight;
  }, [detail?.messages?.length]);
  useEffect(() => {
    if (!ready) return;
    api("/platforms")
      .then(setPlatforms)
      .catch((e) => setError(e.message));
  }, [ready]);
  useEffect(() => {
    if (!channels) return;
    const previous = document.activeElement as HTMLElement | null;
    const drawer = document.querySelector<HTMLElement>(".drawer");
    drawer?.querySelector<HTMLElement>("button")?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") setChannels(false);
      if (e.key !== "Tab") return;
      const items = drawer?.querySelectorAll<HTMLElement>(
        "button:not(:disabled)",
      );
      if (!items?.length) return;
      if (e.shiftKey && document.activeElement === items[0]) {
        e.preventDefault();
        items[items.length - 1].focus();
      } else if (
        !e.shiftKey &&
        document.activeElement === items[items.length - 1]
      ) {
        e.preventDefault();
        items[0].focus();
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [channels]);
  useEffect(() => {
    if (!chatOpen || !window.matchMedia("(max-width:1279px)").matches) return;
    const panel = document.querySelector<HTMLElement>(".chat-panel");
    const previous = document.activeElement as HTMLElement | null;
    const background = [".agent-canvas", ".sidebar", ".topbar", ".chat-launch"]
      .map((s) => document.querySelector<HTMLElement>(s))
      .filter((e): e is HTMLElement => !!e);
    background.forEach((e) => {
      e.inert = true;
    });
    panel?.setAttribute("role", "dialog");
    panel?.setAttribute("aria-modal", "true");
    panel?.querySelector<HTMLElement>(".close-chat")?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setChatOpen(false);
      }
      if (e.key !== "Tab") return;
      const items = panel?.querySelectorAll<HTMLElement>(
        "button:not(:disabled),textarea:not(:disabled),a[href],summary",
      );
      if (!items?.length) return;
      if (e.shiftKey && document.activeElement === items[0]) {
        e.preventDefault();
        items[items.length - 1].focus();
      } else if (
        !e.shiftKey &&
        document.activeElement === items[items.length - 1]
      ) {
        e.preventDefault();
        items[0].focus();
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      background.forEach((e) => {
        e.inert = false;
      });
      panel?.removeAttribute("role");
      panel?.removeAttribute("aria-modal");
      previous?.focus();
    };
  }, [chatOpen, current, !!detail]);
  function actionError(message: string) {
    const dialog = document.querySelector("dialog[open]");
    if (dialog)
      dialog.dispatchEvent(
        new CustomEvent("lead:dialog-error", { detail: message }),
      );
    else setError(message);
  }
  async function run(action: () => Promise<void>) {
    if (readOnly) {
      setError("当前成员仅有查看权限");
      return;
    }
    setError("");
    setBusy(true);
    try {
      await action();
      await refresh();
    } catch (e) {
      actionError(e instanceof Error ? e.message : "操作未完成");
    } finally {
      setBusy(false);
    }
  }
  async function select(id: string) {
    selected.current = id;
    setCurrent(id);
    window.history.replaceState(null, "", "?agent=" + id);
    setDetail(null);
    setView("agent");
    setChatOpen(false);
    followMessages.current = true;
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
  function beginCreate(initial = "") {
    setCreateInitial(initial);
    setCreateOpen(true);
  }
  async function send() {
    if ((!text.trim() && !attachments.length) || busy) return;
    const value = text.trim() || "我上传了素材，供后续运营使用。";
    if (!current) {
      beginCreate(value);
      return;
    }
    await run(async () => {
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
  async function connect(p: Row) {
    if (!current || readOnly) return;
    window.dispatchEvent(new Event("lead:close-dialogs"));
    setBrowser({ platform: p.id, name: p.name });
    setChannels(false);
  }
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
    if (readOnly) return <p className="readonly-label">仅可查看</p>;
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
                disabled={busy || readOnly}
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
  if (loginUrl)
    return (
      <main className="login-page">
        <span className="wordmark">
          lead<span>·</span>
        </span>
        <a className="primary login-button" href={loginUrl}>
          登录
        </a>
      </main>
    );
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
          onClick={() => beginCreate()}
          disabled={busy || !ready || readOnly}
        >
          <Plus size={16} />
          新建 Agent
        </button>
        <nav>
          <button
            className={view === "home" ? "active" : ""}
            onClick={() => {
              setView("home");
              window.history.replaceState(null, "", "/");
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
              <AgentAvatar agent={a} size={22} />
              <span className="ellipsis">{a.name}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <NotificationIndicator />
          <a
            href="/admin"
            className="admin-entry"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px 12px",
              color: "var(--text-secondary)",
              textDecoration: "none",
            }}
          >
            <Settings size={16} />
            管理后台
          </a>
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
      {side && (
        <button
          className="nav-dismiss"
          aria-label="关闭导航"
          onClick={() => setSide(false)}
        />
      )}
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
                <p>介绍你的业务，Agent 将持续为你运营社媒。</p>
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
                    disabled={readOnly || busy}
                    onClick={() => beginCreate()}
                  >
                    <Plus size={16} />
                    新建 Agent
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
                        <AgentAvatar agent={a} size={36} />
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
            {user?.mode === "oidc" && (
              <WorkspaceSettings user={user} key={user.tenant_id} />
            )}
            <details className="advanced">
              <summary>运行信息</summary>
              <div className="advanced-content">
                <section>
                  <h2>模型服务</h2>
                  <div className="setting-row">
                    <span>DeepSeek</span>
                    <span className="pill">
                      {health?.model_configured ? "已配置" : "等待配置"}
                    </span>
                  </div>
                  {user?.mode === "local_owner" && (
                    <p className="muted">
                      在项目 .env 中填写 DEEPSEEK_API_KEY 后即可使用。
                    </p>
                  )}
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
              </div>
            </details>
          </main>
        ) : !detail ? (
          <div className="loading">
            <div />
            <div />
          </div>
        ) : (
          <div className="agent-layout">
            <main
              className="agent-canvas"
              id="main-content"
              tabIndex={0}
              aria-label="Agent 运营工作台"
            >
              <div className="workspace-content">
                <header className="agent-header">
                  <AgentIdentity
                    key={current}
                    agent={detail}
                    readOnly={readOnly}
                    onComplete={refresh}
                    onError={setError}
                  />
                  <div className="agent-actions">
                    <span className="status">
                      <i
                        className={
                          "dot " +
                          (detail.desired_state === "running" ? "green" : "")
                        }
                      />
                      {status(detail)}
                    </span>
                    {detail.desired_state === "running" ? (
                      <button
                        onClick={() => control("pause")}
                        disabled={busy || readOnly}
                      >
                        <Pause size={13} />
                        暂停全部
                      </button>
                    ) : (
                      <button
                        onClick={() =>
                          control(
                            detail.desired_state === "paused"
                              ? "resume"
                              : "start",
                          )
                        }
                        disabled={busy || readOnly}
                      >
                        <Play size={13} />
                        {detail.desired_state === "paused"
                          ? "恢复运营"
                          : "启动运营"}
                      </button>
                    )}
                  </div>
                </header>
                <SocialAccounts
                  key={current}
                  agent={detail}
                  platforms={platforms}
                  readOnly={readOnly}
                  onConnect={connect}
                  onComplete={refresh}
                />
                <AgentResults
                  key={"results" + current}
                  agent={detail}
                  readOnly={readOnly}
                />
                {detail.cards?.some(
                  (c: Row) => c.state === "pending" && c.valid,
                ) && (
                  <section className="workspace-section attention-section">
                    <div className="section-head">
                      <h2>需要你确认</h2>
                    </div>
                    <ChannelCards
                      aid={current!}
                      cards={detail.cards
                        .filter((c: Row) => c.state === "pending" && c.valid)
                        .map((c: Row) => ({
                          ...c.payload,
                          id: c.id,
                          type: c.type,
                          version: c.version,
                        }))}
                      states={detail.cards}
                      busy={busy || readOnly}
                      onComplete={refresh}
                      start={() => control("start")}
                      respond={(card, accepted) =>
                        run(async () => {
                          await api(
                            `/agents/${current}/cards/${card.id}/respond`,
                            { version: card.version, accepted },
                          );
                        })
                      }
                    />
                  </section>
                )}
                <AgentPreferences
                  key={"prefs" + current}
                  agent={detail}
                  readOnly={readOnly}
                  onComplete={refresh}
                  onError={setError}
                />
                <ActivityPanel key={"activity" + current} agent={detail} />
                <OperatingPanel key={"operations" + current} aid={current!} />
                <UsagePanel
                  canManageBudget={user?.role === "owner"}
                  key={"usage" + current}
                  agent={current!}
                  readOnly={readOnly}
                >
                  <section className="settings-group">
                    <div className="settings-group-heading">
                      <h3>浏览器环境</h3>
                      <p>此 Agent 使用独立的浏览器环境保存账号登录状态。</p>
                    </div>
                    <dl className="environment-details">
                      <div>
                        <dt>浏览器</dt>
                        <dd>{detail.profile.provider}</dd>
                      </div>
                      <div>
                        <dt>环境标识</dt>
                        <dd>
                          <code>{detail.profile.id.slice(0, 8)}</code>
                        </dd>
                      </div>
                      <div>
                        <dt>网络出口</dt>
                        <dd>
                          {detail.network.state === "verified"
                            ? "已验证"
                            : "未验证独立 IP"}
                        </dd>
                      </div>
                    </dl>
                    <button
                      disabled={busy || readOnly}
                      onClick={() =>
                        run(async () => {
                          const checked = await api(
                            `/agents/${current}/identity/check`,
                            {},
                          );
                          if (!checked.verified)
                            actionError("当前环境尚未通过固定独立出口验证");
                        })
                      }
                    >
                      检查环境
                    </button>
                  </section>
                  <details className="settings-disclosure">
                    <summary>
                      <span>本地浏览器测试</span>
                      <ChevronRight size={14} />
                    </summary>
                    <div className="disclosure-content">
                      <p className="muted small">
                        测试站使用真实浏览器，不计入运营和获客数据。
                      </p>
                      <div className="settings-button-row">
                        <button
                          disabled={busy || readOnly}
                          onClick={() =>
                            run(async () => {
                              await api(`/agents/${current}/lab/connect`, {});
                            })
                          }
                        >
                          授权测试站
                        </button>
                        <button
                          disabled={busy || readOnly}
                          onClick={() =>
                            run(async () => {
                              await api(`/agents/${current}/lab/publish`, {
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
                          disabled={busy || readOnly}
                          onClick={() =>
                            connect({ id: "lab", name: "本地测试站" })
                          }
                        >
                          打开测试浏览器
                        </button>
                      </div>
                    </div>
                  </details>
                  <div className="archive-setting settings-action-row">
                    <div>
                      <p>归档 Agent</p>
                      <span className="muted small">
                        停止运营，并从当前列表中收起。
                      </span>
                    </div>
                    <button
                      className="danger"
                      disabled={busy || readOnly}
                      onClick={() =>
                        run(async () => {
                          await api(`/agents/${current}/control`, {
                            action: "archive",
                          });
                          window.history.replaceState(null, "", "/");
                          selected.current = null;
                          setCurrent(null);
                          setDetail(null);
                          setView("home");
                        })
                      }
                    >
                      归档 Agent
                    </button>
                  </div>
                </UsagePanel>
              </div>
            </main>
            <button
              className="chat-launch"
              aria-label="打开 Agent 对话"
              onClick={() => setChatOpen(true)}
            >
              <MessageSquare size={18} />与 {detail.name} 对话
            </button>
            <aside
              className={"chat-panel " + (chatOpen ? "is-open" : "")}
              aria-label="Agent 辅助对话"
            >
              <header className="chat-heading">
                <AgentAvatar agent={detail} size={28} />
                <div>
                  <h2>与 {detail.name} 对话</h2>
                  <p className="muted small">补充想法，调整方向</p>
                </div>
                <button
                  className="icon close-chat"
                  aria-label="关闭对话"
                  onClick={() => setChatOpen(false)}
                >
                  <X size={16} />
                </button>
              </header>
              <div
                className="messages"
                role="log"
                aria-label="Agent 对话记录"
                tabIndex={0}
                ref={messagesRef}
                onScroll={(e) => {
                  const box = e.currentTarget;
                  followMessages.current =
                    box.scrollHeight - box.scrollTop - box.clientHeight < 100;
                }}
              >
                {detail.messages.map((m: Row) => (
                  <div className={"message " + m.role} key={m.id}>
                    {m.role === "assistant" && (
                      <AgentAvatar agent={detail} size={24} />
                    )}
                    <div className="message-content">
                      <div className="bubble">{m.content.text}</div>
                      <time className="message-time" dateTime={m.created_at}>
                        {new Date(m.created_at).toLocaleString("zh-CN", {
                          month: "numeric",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
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
                        busy={busy || readOnly}
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
                              MODEL_NOT_CONFIGURED: "所选模型尚未配置服务端凭据",
                              MODEL_AUTH_FAILED: "所选模型的凭据验证失败",
                              MODEL_CREDITS_REQUIRED: "所选模型账户余额不足",
                              MODEL_RATE_LIMITED: "所选模型请求过于频繁，请稍后重试",
                              MODEL_NOT_FOUND: "所选模型不存在或当前凭据无权使用，请在后台核对模型名称",
                              BUDGET_EXCEEDED: "今日模型用量已达上限",
                            } as Row
                          )[m.error_code] || "模型请求未完成"}
                          {m.request_model && <span className="muted small"> · {m.request_model.replace("openai:", "OpenAI · ")}</span>}
                          <button
                            disabled={busy || readOnly}
                            onClick={() =>
                              run(async () => {
                                await api("/agents/" + current + "/retry", {});
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

              <p className="chat-caption muted small">
                无需保持对话，Agent 会持续工作
              </p>
            </aside>
          </div>
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
                <button
                  key={p.id}
                  onClick={() => connect(p)}
                  disabled={busy || readOnly}
                >
                  <span className="platform-symbol">
                    <SocialIcon platform={p.id} />
                  </span>
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
      {createOpen && (
        <NewAgentDialog
          initial={createInitial}
          platforms={platforms}
          onClose={() => setCreateOpen(false)}
          onCreated={async (id) => {
            setCreateOpen(false);
            setText("");
            await select(id);
            await refresh();
          }}
        />
      )}
      {browser && current && (
        <AccountBrowser
          key={current + browser.platform}
          aid={current}
          platform={browser.platform}
          name={browser.name}
          onClose={() => {
            setBrowser(null);
            refresh().catch((e) => setError(e.message));
          }}
        />
      )}
    </div>
  );
}
