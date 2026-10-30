"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  Bot,
  Gauge,
  GitBranch,
  History as HistoryIcon,
  LayoutGrid,
  ListChecks,
  RefreshCw,
  SlidersHorizontal,
  BookOpen,
  Clock3,
  Bell,
  ClipboardCheck,
  Library,
} from "lucide-react";
import { api, setWorkspace } from "../api-client";
import ConfigEditor, { History, Row } from "./config-editor";
import StrategyForm from "./strategy-form";
import OnboardingForm from "./onboarding-form";
import OperationalSettings from "./operational-settings";
import { ContentLibrary, Readiness } from "./preparation";
import ModelUsagePage from "./model-usage-page";
import ModelCatalog from "./model-catalog";
import { MemberAccess, Pricing, ExternalNotifications } from "./advanced";
import {
  AgentDirectory,
  Attribution,
  ChannelRules,
  Experiences,
} from "./management";
import "./admin.css";

const sections = [
  { id: "overview", label: "管理总览", icon: LayoutGrid },
  { id: "readiness", label: "配置就绪", icon: ClipboardCheck },
  { id: "content", label: "资料初始化", icon: Library },
  { id: "budgets", label: "Agent 额度", icon: Gauge },
  { id: "usage", label: "模型用量", icon: Gauge },
  { id: "agents", label: "Agent 管理", icon: Bot },
  { id: "strategies", label: "渠道策略", icon: GitBranch },
  { id: "channel-rules", label: "渠道规则", icon: SlidersHorizontal },
  { id: "experiences", label: "经验审核", icon: ClipboardCheck },
  { id: "attribution", label: "数据与归因", icon: Gauge },
  { id: "onboarding", label: "创建引导", icon: ListChecks },
  { id: "models", label: "模型与工具", icon: SlidersHorizontal },
  { id: "model-catalog", label: "可选模型", icon: ListChecks },
  { id: "brand", label: "品牌与知识", icon: BookOpen },
  { id: "runtime", label: "调度与异常", icon: Clock3 },
  { id: "notifications", label: "通知配置", icon: Bell },
  { id: "external-notifications", label: "外部通知", icon: Bell },
  { id: "pricing", label: "模型定价", icon: Gauge },
  { id: "members", label: "成员权限", icon: Bot },
  { id: "history", label: "变更记录", icon: HistoryIcon },
];
const format = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
const stateNames: Row = {
  draft: "未启动",
  running: "运营中",
  paused: "已暂停",
  ready: "就绪",
  needs_attention: "需要处理",
};

function BudgetField({
  body,
  change,
  allowDefault,
}: {
  body: Row;
  change: (body: Row) => void;
  allowDefault: boolean;
}) {
  const inherited = body.daily_token_limit === null;
  return (
    <>
      {allowDefault && (
        <label className="admin-checkbox">
          <input
            type="checkbox"
            checked={inherited}
            onChange={(e) =>
              change({ daily_token_limit: e.target.checked ? null : 0 })
            }
          />
          跟随工作室默认额度
        </label>
      )}
      {!inherited && (
        <label>
          每日 token 上限
          <input
            required
            type="number"
            min={0}
            max={9007199254740991}
            step={1}
            value={body.daily_token_limit}
            onChange={(e) =>
              change({
                daily_token_limit:
                  e.target.value === "" ? "" : Number(e.target.value),
              })
            }
          />
        </label>
      )}
      <p className="admin-help">
        每天北京时间 08:00 重置。0
        会停止新增模型调用。修改立即用于额度校验，已用量和待核对预留继续保留。
      </p>
    </>
  );
}

function Budgets({
  agents,
  readOnly,
  refresh,
}: {
  agents: Row[];
  readOnly: boolean;
  refresh: () => void;
}) {
  const [query, setQuery] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [editing, setEditing] = useState("default");
  const [limit, setLimit] = useState(""),
    [inherit, setInherit] = useState(false),
    [busy, setBusy] = useState(false),
    [feedback, setFeedback] = useState("");
  const filtered = agents.filter((a) =>
    a.name.toLowerCase().includes(query.toLowerCase()),
  );
  async function bulk() {
    setBusy(true);
    setFeedback("");
    try {
      await api("/admin/budgets/bulk", {
        agents: selected,
        daily_token_limit: inherit ? null : Number(limit),
        expected_versions: Object.fromEntries(
          agents
            .filter((a) => selected.includes(a.id))
            .map((a) => [a.id, a.config.version]),
        ),
        note: "批量调整 Agent 每日额度",
      });
      setFeedback(`已更新 ${selected.length} 个 Agent。`);
      setSelected([]);
      refresh();
    } catch (e) {
      setFeedback((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="admin-toolbar">
        <label className="admin-search">
          搜索 Agent
          <input
            type="search"
            value={query}
            placeholder="输入 Agent 名称"
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button type="button" onClick={() => setEditing("default")}>
          工作室默认额度
        </button>
      </div>
      <div className="admin-table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th scope="col">
                {!readOnly && (
                  <input
                    aria-label="选择当前搜索结果中的全部 Agent"
                    type="checkbox"
                    checked={
                      filtered.length > 0 &&
                      filtered.every((a) => selected.includes(a.id))
                    }
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [
                              ...new Set([
                                ...selected,
                                ...filtered.map((a) => a.id),
                              ]),
                            ]
                          : selected.filter(
                              (id) => !filtered.some((a) => a.id === id),
                            ),
                      )
                    }
                  />
                )}
              </th>
              <th scope="col">Agent</th>
              <th scope="col">每日额度</th>
              <th scope="col">今日已用</th>
              <th scope="col">调用中预留</th>
              <th scope="col">剩余额度</th>
              <th scope="col">配置</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((agent) => (
              <tr key={agent.id}>
                <td>
                  {!readOnly && (
                    <input
                      type="checkbox"
                      aria-label={`选择 ${agent.name}`}
                      checked={selected.includes(agent.id)}
                      onChange={(e) =>
                        setSelected(
                          e.target.checked
                            ? [...selected, agent.id]
                            : selected.filter((id) => id !== agent.id),
                        )
                      }
                    />
                  )}
                </td>
                <th scope="row">
                  <a href={`/?agent=${agent.id}`}>{agent.name}</a>
                  <small>
                    {agent.setup_pending
                      ? "创建中"
                      : stateNames[agent.desired_state] || agent.desired_state}
                  </small>
                </th>
                <td>
                  {format(agent.budget.daily_token_limit)}
                  <small>
                    {agent.budget.custom_daily_token_limit === null
                      ? "跟随默认"
                      : "独立额度"}
                  </small>
                </td>
                <td>{format(agent.spent)}</td>
                <td>{format(agent.reserved)}</td>
                <td className={agent.remaining === 0 ? "admin-attention" : ""}>
                  {format(agent.remaining)}
                </td>
                <td>
                  <button type="button" onClick={() => setEditing(agent.id)}>
                    {readOnly ? "查看" : "调整"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!filtered.length && (
        <p className="admin-empty">
          {query
            ? "没有匹配的 Agent。"
            : "尚无 Agent，创建后可在这里维护额度。"}
        </p>
      )}
      {!readOnly && selected.length > 0 && (
        <form
          className="admin-bulk"
          onSubmit={(e) => {
            e.preventDefault();
            void bulk();
          }}
        >
          <strong>已选 {selected.length} 个 Agent</strong>
          <label className="admin-checkbox">
            <input
              type="checkbox"
              checked={inherit}
              onChange={(e) => setInherit(e.target.checked)}
            />
            使用默认额度
          </label>
          {!inherit && (
            <label>
              统一每日额度
              <input
                required
                type="number"
                min={0}
                step={1}
                max={9007199254740991}
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
            </label>
          )}
          <button disabled={busy} type="submit">
            {busy ? "保存中…" : "应用到所选 Agent"}
          </button>
        </form>
      )}
      {feedback && (
        <p className="admin-feedback" role="status">
          {feedback}
        </p>
      )}
      <ConfigEditor
        key={editing}
        configKey={`budgets/${editing}`}
        title={
          editing === "default"
            ? "工作室默认额度"
            : `${agents.find((a) => a.id === editing)?.name || "Agent"} · 每日额度`
        }
        description={
          editing === "default"
            ? "用于未设置独立额度的 Agent，也用于后续新建 Agent。"
            : "调整仅作用于这个 Agent，其他 Agent 的额度保持各自设置。"
        }
        readOnly={readOnly}
        drafts={false}
        onSaved={refresh}
      >
        {(body, change) => (
          <BudgetField
            body={body}
            change={change}
            allowDefault={editing !== "default"}
          />
        )}
      </ConfigEditor>
    </>
  );
}

function Strategies({
  agents,
  options,
  readOnly,
}: {
  agents: Row[];
  options: Row;
  readOnly: boolean;
}) {
  const [platform, setPlatform] = useState("telegram"),
    [aid, setAid] = useState(""),
    [target, setTarget] = useState(""),
    [evidence, setEvidence] = useState<Row | null>(null),
    [error, setError] = useState("");
  const key = `strategies/${platform}${aid ? "/" + aid : ""}${target ? "/" + target : ""}`;
  useEffect(() => {
    let active = true;
    setEvidence(null);
    setError("");
    if (aid)
      api("/admin/strategy-evidence/" + aid)
        .then((value) => {
          if (active) setEvidence(value);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [aid, key]);
  const targets = (options.targets || []).filter(
    (t: Row) => t.agent_id === aid && t.platform === platform,
  );
  const observations = Object.entries(evidence?.targets || {}).filter(
    ([id, raw]) =>
      (raw as Row).platform === platform && (!target || id === target),
  );
  return (
    <>
      <div className="admin-scope-controls">
        <label>
          运营渠道
          <select
            value={platform}
            onChange={(e) => {
              setPlatform(e.target.value);
              setTarget("");
            }}
          >
            {options.platforms.map((p: Row) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          适用范围
          <select
            value={aid}
            onChange={(e) => {
              setAid(e.target.value);
              setTarget("");
            }}
          >
            <option value="">通用渠道模板</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        {aid && (
          <label>
            具体账号目标
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">此 Agent 在该渠道的全部目标</option>
              {targets.map((t: Row) => (
                <option key={t.id} value={t.id}>
                  {t.display_name || t.account_name || t.id}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {aid && (
        <details className="admin-observations">
          <summary>查看此渠道的实际运营观察</summary>
          {error && <p role="alert">{error}</p>}
          {!observations.length && (
            <p className="admin-help">
              尚无可比较的策略记录。发布回执、观察周期和归因数据齐备后会显示结果。
            </p>
          )}
          {observations.map(([id, raw]) => {
            const context = raw as Row;
            return (
              <div key={id}>
                <p className="admin-help">
                  {context.coverage === "connected"
                    ? "归因数据已接通"
                    : "归因数据尚未齐备"}{" "}
                  · 观察周期 {context.observation_days} 天
                </p>
                <div className="admin-table-scroll">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>方案</th>
                        <th>已发布</th>
                        <th>观察期已满</th>
                        <th>到站会话</th>
                        <th>已确认转化</th>
                      </tr>
                    </thead>
                    <tbody>
                      {context.stats?.map((s: Row) => (
                        <tr key={s.plan_id}>
                          <th>{s.plan_id}</th>
                          <td>{s.posts}</td>
                          <td>{s.mature_posts}</td>
                          <td>
                            {context.coverage === "connected"
                              ? s.sessions
                              : "待接入"}
                          </td>
                          <td>
                            {context.coverage === "connected"
                              ? s.outcomes
                              : "待接入"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </details>
      )}
      <ConfigEditor
        key={key}
        configKey={key}
        title={
          target ? "账号目标策略" : aid ? "Agent 渠道策略" : "通用渠道策略"
        }
        description="发布后用于后续规划。具体账号目标优先于 Agent 配置，Agent 配置优先于通用模板。已有任务保留其策略快照。"
        readOnly={readOnly}
      >
        {(body, change) => <StrategyForm body={body} change={change} />}
      </ConfigEditor>
    </>
  );
}

function ChangeLog() {
  const [rows, setRows] = useState<Row[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [more, setMore] = useState(true);
  const load = useCallback(async (before?: Row) => {
    setBusy(true);
    setError("");
    try {
      const next = await api(
        "/admin/history" +
          (before
            ? "?before=" +
              encodeURIComponent(before.created_at) +
              "&before_id=" +
              before.id
            : ""),
      );
      setRows((prior) => (before ? [...prior, ...next] : next));
      setMore(next.length === 40);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section className="admin-editor">
      <header className="admin-section-heading">
        <div>
          <h2>工作室配置记录</h2>
          <p>草稿、发布和回滚分别留存。历史内容不可修改，回滚会生成新版本。</p>
        </div>
        <button disabled={busy} onClick={() => void load()}>
          刷新记录
        </button>
      </header>
      {error && <p role="alert">{error}</p>}
      <History rows={rows} />
      {more && rows.length > 0 && (
        <button
          disabled={busy}
          onClick={() => void load(rows[rows.length - 1])}
        >
          {busy ? "读取中…" : "加载更早记录"}
        </button>
      )}
    </section>
  );
}

export default function AdminPage() {
  const [section, setSection] = useState("overview"),
    [user, setUser] = useState<Row | null>(null),
    [agents, setAgents] = useState<Row[]>([]),
    [options, setOptions] = useState<Row>({ platforms: [], targets: [] });
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const saved = window.location.hash.slice(1);
    if (sections.some((item) => item.id === saved)) setSection(saved);
  }, []);
  const [ready, setReady] = useState(false),
    [error, setError] = useState(""),
    [loginUrl, setLoginUrl] = useState(""),
    [refreshing, setRefreshing] = useState(false);
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const [a, o] = await Promise.all([
        api("/admin/agents"),
        api("/admin/strategy-options"),
      ]);
      setAgents(a);
      setLoaded(true);
      setOptions(o);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
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
        const me = await api("/auth/me");
        if (!active) return;
        setUser(me);
        setWorkspace(me.tenant_id);
        await refresh();
        if (active) setReady(true);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    const expired = (event: Event) => {
      setReady(false);
      setLoginUrl((event as CustomEvent).detail);
    };
    window.addEventListener("lead:session-expired", expired);
    return () => {
      active = false;
      window.removeEventListener("lead:session-expired", expired);
    };
  }, [refresh]);
  if (!ready)
    return (
      <main className="admin-gate">
        <h1>lead 管理后台</h1>
        {loginUrl ? (
          <a href={loginUrl}>登录工作室</a>
        ) : error ? (
          <>
            <p role="alert">{error}</p>
            <button onClick={() => window.location.reload()}>重新连接</button>
          </>
        ) : (
          <p role="status">正在连接工作室…</p>
        )}
        <a href="/">返回工作台</a>
      </main>
    );
  const sectionCapabilities: Row = {
    budgets: "admin.budgets",
    agents: "admin.agents",
    strategies: "admin.strategies",
    "channel-rules": "admin.channels",
    experiences: "admin.experiences",
    attribution: "admin.attribution",
    onboarding: "admin.onboarding",
    models: "admin.models",
    "model-catalog": "admin.models",
    brand: "admin.brand",
    runtime: "admin.runtime",
    notifications: "admin.notifications",
    pricing: "admin.pricing",
  };
  const readOnly =
    user?.role !== "owner" &&
    !(user?.capabilities || []).includes(sectionCapabilities[section]);
  const needsAttention = agents.filter(
    (a) => a.health_state === "needs_attention" || a.remaining === 0,
  );
  return (
    <div className="admin-shell">
      <a className="admin-skip" href="#admin-main">
        跳到配置内容
      </a>
      <aside className="admin-sidebar">
        <a className="admin-brand" href="/">
          <span>lead</span>
          <small>管理后台</small>
        </a>
        <nav aria-label="后台导航">
          {sections.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              aria-current={section === id ? "page" : undefined}
              onClick={() => {
                setSection(id);
                window.history.replaceState(null, "", `/admin#${id}`);
                document.getElementById("admin-main")?.scrollTo({ top: 0 });
              }}
            >
              <Icon size={17} />
              {label}
            </button>
          ))}
        </nav>
        <div className="admin-sidebar-bottom">
          <span className="muted small">
            {user?.role === "owner"
              ? "所有者权限"
              : (user?.capabilities || []).length
                ? "按授权访问"
                : "只读访问"}
          </span>
          <a href="/">
            <ArrowLeft size={15} />
            返回运营工作台
          </a>
        </div>
      </aside>
      <main className="admin-main" id="admin-main">
        <header className="admin-page-heading">
          <div>
            <span className="admin-eyebrow">工作室管理</span>
            <h1>{sections.find((s) => s.id === section)?.label}</h1>
            <p>
              {section === "members"
                ? "维护成员角色、可操作功能与 Agent 数据范围。"
                : section === "pricing"
                  ? "维护有来源的模型单价，保留历史调用的计价版本。"
                  : section === "external-notifications"
                    ? "管理 Webhook、邮件接收渠道与真实发送结果。"
                    : section === "agents"
                      ? "集中查找 Agent，核对状态后批量暂停或归档。"
                      : section === "channel-rules"
                        ? "维护渠道、Agent 与目标层级的运行限制。"
                        : section === "attribution"
                          ? "维护转化目标、归因窗口和数据新鲜度。"
                          : section === "experiences"
                            ? "审核渠道观察，将适用的做法纳入后续规划。"
                            : section === "model-catalog"
                              ? "维护共用同一 OpenAI 凭据的可选模型，并保留配置版本。"
                              : section === "overview"
                                ? "维护 Agent 的运行规则、运营策略和创建流程。"
                                : section === "readiness"
                                  ? "核对配置、业务资料、账号授权和真实数据接入的缺口。"
                                  : section === "content"
                                    ? "把已有研究填充到配置，保留来源、版本和人工编辑。"
                                    : section === "usage"
                                      ? "按实际模型区分 token 消耗、输入输出、缓存和待核对预留。"
                                      : section === "budgets"
                                        ? "集中分配每日额度，查看消耗与待核对预留。"
                                        : section === "strategies"
                                          ? "把渠道方法和实际经验沉淀为可追溯的运营策略。"
                                          : section === "onboarding"
                                            ? "维护三个创建步骤，发布前可直接预览填写体验。"
                                            : section === "models"
                                              ? "按任务选择模型，管理 Agent 实际可用的工具。"
                                              : section === "brand"
                                                ? "维护品牌表达、业务事实和有来源的知识资料。"
                                                : section === "runtime"
                                                  ? "安排自动运营时段，控制并发、重试和失败暂停。"
                                                  : section === "notifications"
                                                    ? "选择真实异常的提醒方式、接收范围与免打扰时段。"
                                                    : "查看配置何时改变、由谁修改，以及具体差异。"}
            </p>
          </div>
          <button
            aria-label="刷新后台数据"
            disabled={refreshing}
            onClick={() => void refresh()}
          >
            <RefreshCw size={16} />
            刷新
          </button>
        </header>
        {error && (
          <p className="admin-feedback error-feedback" role="alert">
            {error}
          </p>
        )}
        {readOnly && (
          <p className="admin-readonly">
            你可以查看已授权范围内的配置和记录。修改、发布及回滚需要所有者授予对应权限。
          </p>
        )}
        {loaded && section === "overview" && (
          <>
            <section className="admin-overview">
              <h2>Agent 运行概况</h2>
              <dl>
                <div>
                  <dt>当前 Agent</dt>
                  <dd>{agents.length}</dd>
                </div>
                <div>
                  <dt>今日已用 token</dt>
                  <dd>{format(agents.reduce((sum, a) => sum + a.spent, 0))}</dd>
                </div>
                <div>
                  <dt>调用中预留</dt>
                  <dd>
                    {format(agents.reduce((sum, a) => sum + a.reserved, 0))}
                  </dd>
                </div>
                <div>
                  <dt>需要处理</dt>
                  <dd>{needsAttention.length}</dd>
                </div>
              </dl>
            </section>
            <section className="admin-editor">
              <header className="admin-section-heading">
                <div>
                  <h2>待处理事项</h2>
                  <p>额度和运行异常会集中显示在这里。</p>
                </div>
              </header>
              {needsAttention.length ? (
                <ul className="admin-attention-list">
                  {needsAttention.map((a) => (
                    <li key={a.id}>
                      <Bot size={18} />
                      <div>
                        <strong>{a.name}</strong>
                        <p>
                          {a.remaining === 0
                            ? "今日可用额度已耗尽，或被调用中预留占满。"
                            : "Agent 当前需要处理，请前往工作台查看。"}
                        </p>
                      </div>
                      <button onClick={() => setSection("budgets")}>
                        查看额度
                      </button>
                      <a href={`/?agent=${a.id}`}>工作台</a>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="admin-empty">
                  当前没有额度耗尽或需要处理的 Agent。
                </p>
              )}
            </section>
            <div className="admin-overview-links">
              {sections
                .filter((s) =>
                  ["readiness", "content", "strategies"].includes(s.id),
                )
                .map(({ id, label, icon: Icon }) => (
                  <button key={id} onClick={() => setSection(id)}>
                    <Icon size={18} />
                    <span>{label}</span>
                    <span aria-hidden="true">→</span>
                  </button>
                ))}
            </div>
          </>
        )}
        {loaded && section === "budgets" && (
          <Budgets
            agents={agents}
            readOnly={readOnly}
            refresh={() => void refresh()}
          />
        )}
        {loaded && section === "strategies" && (
          <Strategies agents={agents} options={options} readOnly={readOnly} />
        )}
        {loaded && section === "onboarding" && (
          <ConfigEditor
            configKey="onboarding/default"
            title="创建引导模板"
            description="只影响使用此版本开始的新建流程。已经保存的创建草稿继续沿用原版本。"
            readOnly={readOnly}
          >
            {(body, change) => <OnboardingForm body={body} change={change} />}
          </ConfigEditor>
        )}
        {loaded && section === "history" && <ChangeLog />}
        {loaded && section === "usage" && <ModelUsagePage agents={agents} />}
        {loaded && section === "members" && (
          <MemberAccess readOnly={readOnly} agents={agents} />
        )}
        {loaded && section === "pricing" && <Pricing readOnly={readOnly} />}
        {loaded && section === "external-notifications" && (
          <ExternalNotifications readOnly={readOnly} agents={agents} />
        )}
        {loaded && section === "model-catalog" && (
          <ModelCatalog readOnly={readOnly} />
        )}
        {loaded && section === "agents" && (
          <AgentDirectory readOnly={readOnly} options={options} />
        )}
        {loaded && section === "channel-rules" && (
          <ChannelRules readOnly={readOnly} options={options} agents={agents} />
        )}
        {loaded && section === "attribution" && (
          <Attribution readOnly={readOnly} agents={agents} />
        )}
        {loaded && section === "experiences" && (
          <Experiences
            readOnly={readOnly}
            agents={agents}
            options={options}
            navigate={(next) => {
              setSection(next);
              window.history.replaceState(null, "", `/admin#${next}`);
            }}
          />
        )}
        {loaded && section === "content" && (
          <ContentLibrary readOnly={readOnly} />
        )}
        {loaded && section === "readiness" && (
          <Readiness
            navigate={(next) => {
              setSection(next);
              window.history.replaceState(null, "", `/admin#${next}`);
              document.getElementById("admin-main")?.scrollTo({ top: 0 });
            }}
          />
        )}
        {loaded &&
          ["models", "brand", "runtime", "notifications"].includes(section) && (
            <OperationalSettings
              key={section}
              kind={section}
              agents={agents}
              readOnly={readOnly}
            />
          )}
      </main>
    </div>
  );
}
