"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api-client";
import ConfigEditor, { Row } from "./config-editor";

const states: Row = {
  running: "运营中",
  paused: "已暂停",
  draft: "未启动",
  archived: "已归档",
};
const reviews: Row = {
  pending: "待审核",
  approved: "已审核",
  rejected: "不采用",
  retired: "已停用",
};
const objectives: Row = {
  signup: "注册",
  lead: "客户咨询",
  trial: "开始试用",
  purchase: "付费购买",
};
const definitions: Row = {
  signup: "由服务端确认用户完成注册，不把点击或网页浏览当作注册。",
  lead: "由业务服务端确认收到有效客户咨询。",
  trial: "由业务服务端确认用户实际开始试用。",
  purchase: "由业务服务端确认完成付费购买，点击支付按钮不算购买。",
};

function AgentScope({
  agents,
  aid,
  change,
  required = false,
}: {
  agents: Row[];
  aid: string;
  change: (value: string) => void;
  required?: boolean;
}) {
  return (
    <label>
      适用 Agent
      <select value={aid} onChange={(e) => change(e.target.value)}>
        <option value="">{required ? "选择 Agent" : "工作室默认"}</option>
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name}
          </option>
        ))}
      </select>
    </label>
  );
}
function Platform({
  options,
  value,
  change,
}: {
  options: Row;
  value: string;
  change: (value: string) => void;
}) {
  return (
    <label>
      运营渠道
      <select value={value} onChange={(e) => change(e.target.value)}>
        {(options.platforms || []).map((platform: Row) => (
          <option key={platform.id} value={platform.id}>
            {platform.name}
          </option>
        ))}
      </select>
    </label>
  );
}
function Numeric({
  label,
  value,
  change,
  min,
  max,
}: {
  label: string;
  value: number;
  change: (value: any) => void;
  min: number;
  max: number;
}) {
  return (
    <label>
      {label}
      <input
        type="number"
        required
        min={min}
        max={max}
        step={1}
        value={value}
        onChange={(e) =>
          change(e.target.value === "" ? "" : Number(e.target.value))
        }
      />
    </label>
  );
}

export function AgentDirectory({
  readOnly,
  options,
}: {
  readOnly: boolean;
  options: Row;
}) {
  const [query, setQuery] = useState(""),
    [state, setState] = useState("active"),
    [platform, setPlatform] = useState("");
  const [rows, setRows] = useState<Row[]>([]),
    [selected, setSelected] = useState<string[]>([]),
    [error, setError] = useState(""),
    [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false);
  const [review, setReview] = useState(""),
    [note, setNote] = useState("");
  const generation = useRef(0);
  const load = useCallback(
    async (before?: Row) => {
      const call = ++generation.current;
      setBusy(true);
      try {
        const params = new URLSearchParams({ query, state });
        if (platform) params.set("platform", platform);
        if (before) {
          params.set("before", before.created_at);
          params.set("before_id", before.id);
        }
        const data = await api("/admin/agent-directory?" + params);
        if (call !== generation.current) return;
        setRows((previous) => (before ? [...previous, ...data] : data));
        setLoaded(true);
        setError("");
      } catch (e) {
        if (call === generation.current) setError((e as Error).message);
      } finally {
        if (call === generation.current) setBusy(false);
      }
    },
    [query, state, platform],
  );
  useEffect(() => {
    setSelected([]);
    setReview("");
    setLoaded(false);
    void load();
  }, [load]);
  const chosen = rows.filter((row) => selected.includes(row.id));
  async function apply() {
    setBusy(true);
    setError("");
    try {
      await api("/admin/agents/control", {
        agents: selected,
        action: review,
        expected_control_versions: Object.fromEntries(
          chosen.map((row) => [row.id, row.control_version]),
        ),
        note,
      });
      setFeedback(
        `已${review === "pause" ? "暂停" : "归档"}所选 ${selected.length} 个 Agent，操作已记录。`,
      );
      setSelected([]);
      setReview("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="admin-scope-controls">
        <label>
          搜索 Agent
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="按名称查找"
          />
        </label>
        <label>
          运行状态
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {[
              ["active", "未归档"],
              ["all", "全部状态"],
              ["running", "运营中"],
              ["paused", "已暂停"],
              ["draft", "未启动"],
              ["archived", "已归档"],
            ].map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          渠道筛选
          <select
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
          >
            <option value="">全部渠道</option>
            {(options.platforms || []).map((p: Row) => (
              <option value={p.id} key={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && (
        <p className="error-feedback" role="alert">
          {error}
        </p>
      )}
      {feedback && (
        <p role="status" className="admin-feedback">
          {feedback}
        </p>
      )}
      {!loaded && !error && <p role="status">正在读取 Agent…</p>}
      {loaded && (
        <section className="admin-editor">
          <header className="admin-section-heading">
            <div>
              <h2>Agent 集中管理</h2>
              <p>
                批量操作前核对控制版本，遇到状态变化整批保留原状态。归档保留历史，停止运营并断开现有账号授权。
              </p>
            </div>
            <button disabled={busy} onClick={() => void load()}>
              刷新列表
            </button>
          </header>
          {!readOnly && (
            <div className="admin-bulk">
              <span>已选 {selected.length} 个</span>
              <button
                disabled={
                  busy ||
                  !selected.length ||
                  chosen.some((a) => a.desired_state === "archived")
                }
                onClick={() => setReview("pause")}
              >
                批量暂停
              </button>
              <button
                disabled={
                  busy ||
                  !selected.length ||
                  chosen.some((a) => a.desired_state === "archived")
                }
                onClick={() => setReview("archive")}
              >
                批量归档
              </button>
            </div>
          )}
          {review && (
            <div className="admin-inline-review">
              <strong>
                核对将要{review === "pause" ? "暂停" : "归档"}的 Agent
              </strong>
              <p>{chosen.map((a) => a.name).join("、")}</p>
              <label>
                操作说明
                <input
                  maxLength={1000}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </label>
              <button disabled={busy} onClick={() => void apply()}>
                确认{review === "pause" ? "暂停" : "归档"}所选 Agent
              </button>
              <button disabled={busy} onClick={() => setReview("")}>
                取消操作
              </button>
            </div>
          )}
          <div className="admin-table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  {!readOnly && <th>选择</th>}
                  <th>Agent</th>
                  <th>运行状态</th>
                  <th>已连接过的渠道</th>
                  <th>入口</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    {!readOnly && (
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择 ${row.name} ${row.id.slice(0, 8)}`}
                          disabled={busy || row.desired_state === "archived"}
                          checked={selected.includes(row.id)}
                          onChange={(e) =>
                            setSelected((previous) =>
                              e.target.checked
                                ? [...previous, row.id]
                                : previous.filter((id) => id !== row.id),
                            )
                          }
                        />
                      </td>
                    )}
                    <td>
                      {row.name}
                      <small className="muted"> {row.id.slice(0, 8)}</small>
                    </td>
                    <td>{states[row.desired_state] || row.desired_state}</td>
                    <td>
                      {row.platforms
                        .map(
                          (p: string) =>
                            (options.platforms || []).find(
                              (v: Row) => v.id === p,
                            )?.name || p,
                        )
                        .join("、") || "尚无渠道"}
                    </td>
                    <td>
                      {row.desired_state !== "archived" && (
                        <a href={`/?agent=${row.id}`}>打开工作台</a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!rows.length && (
            <p className="admin-empty">没有符合筛选条件的 Agent。</p>
          )}
          {rows.length > 0 && rows.length % 50 === 0 && (
            <button
              disabled={busy}
              onClick={() => void load(rows[rows.length - 1])}
            >
              加载更早 Agent
            </button>
          )}
        </section>
      )}
    </>
  );
}

export function ChannelRules({
  agents,
  options,
  readOnly,
}: {
  agents: Row[];
  options: Row;
  readOnly: boolean;
}) {
  const [aid, setAid] = useState(""),
    [platform, setPlatform] = useState("telegram"),
    [target, setTarget] = useState(""),
    [status, setStatus] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0);
  const key = `channel-rules/${platform}${aid ? "/" + aid : ""}${target ? "/" + target : ""}`;
  useEffect(() => {
    let active = true;
    api(
      `/admin/channel-rules/effective?platform=${platform}${aid ? "&aid=" + aid : ""}${target ? "&target=" + target : ""}`,
    )
      .then((data) => {
        if (active) {
          setStatus(data);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [key, aid, platform, target, revision]);
  return (
    <>
      <div className="admin-scope-controls">
        <Platform
          options={options}
          value={platform}
          change={(v) => {
            setPlatform(v);
            setTarget("");
          }}
        />
        <AgentScope
          agents={agents}
          aid={aid}
          change={(v) => {
            setAid(v);
            setTarget("");
          }}
        />
        {aid && (
          <label>
            账号目标
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">此 Agent 的全部目标</option>
              {(options.targets || [])
                .filter(
                  (t: Row) => t.agent_id === aid && t.platform === platform,
                )
                .map((t: Row) => (
                  <option key={t.id} value={t.id}>
                    {t.display_name || t.account_name}
                  </option>
                ))}
            </select>
          </label>
        )}
      </div>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {status && (
        <section className="admin-service-status">
          <div>
            <strong>当前生效的限制</strong>
            <p>
              {status.policy.enabled
                ? "渠道自动操作已允许"
                : "渠道自动操作已停用"}
              ；
              {status.policy.allow_publishing
                ? "发布仍需账号授权与格式验证"
                : "发布已停用"}
              ；额外频率上限：
              {status.policy.daily_publish_limit === null
                ? "遵循账号授权"
                : `${status.policy.daily_publish_limit} 条 / 目标 / 24 小时`}
              。
            </p>
            <small>
              各层规则取更严格的范围，不能通过账号覆盖放宽上层限制。原有平台、账号和授权检查继续生效。
            </small>
          </div>
        </section>
      )}
      <ConfigEditor
        key={key}
        configKey={key}
        title="渠道与账号规则"
        description="发布后限制后续自动操作，并在最终提交前重新核对。已提交但结果未知的操作继续核对原回执。"
        readOnly={readOnly}
        onSaved={() => setRevision((v) => v + 1)}
      >
        {(body, change) => (
          <>
            {(
              [
                ["enabled", "允许此渠道自动操作"],
                ["allow_publishing", "允许在已授权目标发布"],
                ["allow_community_growth", "允许已有委托下的社群运营"],
              ] as const
            ).map(([field, label]) => (
              <label className="admin-checkbox" key={field}>
                <input
                  type="checkbox"
                  checked={body[field]}
                  onChange={(e) =>
                    change({ ...body, [field]: e.target.checked })
                  }
                />
                {label}
              </label>
            ))}
            <fieldset className="admin-choice-group">
              <legend>可发布的内容格式</legend>
              {[
                ["text", "文字"],
                ["image", "图片"],
                ["video", "视频"],
              ].map(([id, label]) => (
                <label className="admin-checkbox" key={id}>
                  <input
                    type="checkbox"
                    checked={body.formats.includes(id)}
                    onChange={(e) =>
                      change({
                        ...body,
                        formats: e.target.checked
                          ? [...body.formats, id]
                          : body.formats.filter((v: string) => v !== id),
                      })
                    }
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <label className="admin-checkbox">
              <input
                type="checkbox"
                checked={body.daily_publish_limit === null}
                onChange={(e) =>
                  change({
                    ...body,
                    daily_publish_limit: e.target.checked ? null : 3,
                  })
                }
              />
              不增加额外频率限制，遵循账号授权
            </label>
            {body.daily_publish_limit !== null && (
              <Numeric
                label="每个目标过去 24 小时最多发布条数"
                min={0}
                max={100}
                value={body.daily_publish_limit}
                change={(value) =>
                  change({ ...body, daily_publish_limit: value })
                }
              />
            )}
          </>
        )}
      </ConfigEditor>
    </>
  );
}

export function Attribution({
  agents,
  readOnly,
}: {
  agents: Row[];
  readOnly: boolean;
}) {
  const [aid, setAid] = useState(""),
    [status, setStatus] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    setStatus(null);
    if (!aid) return;
    let active = true;
    api("/admin/attribution-status?aid=" + aid)
      .then((value) => {
        if (active) {
          setStatus(value);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [aid, revision]);
  return (
    <>
      <div className="admin-toolbar">
        <AgentScope agents={agents} aid={aid} change={setAid} />
      </div>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {status && (
        <section className="admin-service-status">
          <div>
            <strong>当前数据接入</strong>
            <p>
              {status.site
                ? `${status.site.site_host} · ${status.site.state === "verified" ? "已收到网站数据" : "尚未完成接入验证"}`
                : "尚未接入网站回传"}
            </p>
            <p>
              网站最近回传：
              {status.site?.last_received_at
                ? new Date(status.site.last_received_at).toLocaleString("zh-CN")
                : "暂无"}
              ；服务端最近回传：
              {status.server_last_received_at
                ? new Date(status.server_last_received_at).toLocaleString(
                    "zh-CN",
                  )
                : "暂无"}
              。
            </p>
            <small>
              当前规划目标：{objectives[status.policy.objective]}
              。没有回传时保留未知，生成安装代码不代表接入完成。
            </small>
          </div>
          <a href={`/?agent=${aid}`}>前往工作台管理网站接入</a>
        </section>
      )}
      <ConfigEditor
        key={aid}
        configKey={"attribution/" + (aid || "default")}
        title="数据与归因规则"
        description="新访问保留开始时的规则快照，历史访问及转化不改写。发布会更新后续规划并撤回旧方向排队内容。"
        readOnly={readOnly}
        onSaved={() => setRevision((v) => v + 1)}
      >
        {(body, change) => (
          <>
            <label>
              本轮转化目标
              <select
                value={body.objective}
                onChange={(e) =>
                  change({
                    ...body,
                    objective: e.target.value,
                    objective_definition: definitions[e.target.value],
                  })
                }
              >
                {Object.entries(objectives).map(([id, name]) => (
                  <option key={id} value={id}>
                    {name as string}
                  </option>
                ))}
              </select>
            </label>
            <label>
              业务完成条件
              <textarea
                required
                maxLength={2000}
                rows={3}
                value={body.objective_definition}
                onChange={(e) =>
                  change({ ...body, objective_definition: e.target.value })
                }
              />
            </label>
            <div className="admin-field-pair">
              <Numeric
                label="访问开始后的转化归因窗口（天）"
                min={1}
                max={30}
                value={body.attribution_window_days}
                change={(v) => change({ ...body, attribution_window_days: v })}
              />
              <Numeric
                label="有效访问所需前台活动时长（秒）"
                min={10}
                max={120}
                value={body.engagement_seconds}
                change={(v) => change({ ...body, engagement_seconds: v })}
              />
            </div>
            <Numeric
              label="数据新鲜度窗口（小时）"
              min={1}
              max={168}
              value={body.freshness_hours}
              change={(v) => change({ ...body, freshness_hours: v })}
            />
            <p className="admin-help">
              仅接收真实服务端确认事件。此配置不会让尚未接入的网站产生访问、注册或收入数据。
            </p>
          </>
        )}
      </ConfigEditor>
    </>
  );
}

function ExperienceForm({
  body,
  change,
  aid,
  options,
}: {
  body: Row;
  change: (body: Row) => void;
  aid: string;
  options: Row;
}) {
  const [sources, setSources] = useState<Row[]>([]),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api(
      `/admin/strategy-experiences/evidence?aid=${aid}&platform=${body.platform}${body.target_id ? "&target=" + body.target_id : ""}`,
    )
      .then((value) => {
        if (active) {
          setSources(value);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [aid, body.platform, body.target_id]);
  return (
    <>
      <label>
        经验名称
        <input
          required
          maxLength={160}
          value={body.title}
          onChange={(e) => change({ ...body, title: e.target.value })}
        />
      </label>
      <div className="admin-field-pair">
        <Platform
          options={options}
          value={body.platform}
          change={(v) =>
            change({
              ...body,
              platform: v,
              target_id: null,
              evidence_action_ids: [],
            })
          }
        />
        <label>
          适用账号目标
          <select
            value={body.target_id || ""}
            onChange={(e) =>
              change({
                ...body,
                target_id: e.target.value || null,
                evidence_action_ids: [],
              })
            }
          >
            <option value="">此 Agent 在该渠道的全部目标</option>
            {(options.targets || [])
              .filter(
                (t: Row) => t.agent_id === aid && t.platform === body.platform,
              )
              .map((t: Row) => (
                <option key={t.id} value={t.id}>
                  {t.display_name}
                </option>
              ))}
          </select>
        </label>
      </div>
      <label>
        关联方案标识（可选）
        <input
          maxLength={120}
          value={body.plan_id}
          onChange={(e) => change({ ...body, plan_id: e.target.value })}
        />
      </label>
      {(
        [
          ["applicable_conditions", "适用条件"],
          ["observation", "实际观察与结果"],
          ["proposed_change", "建议采用的做法"],
        ] as const
      ).map(([field, label]) => (
        <label key={field}>
          {label}
          <textarea
            rows={3}
            maxLength={
              field === "observation"
                ? 6000
                : field === "proposed_change"
                  ? 4000
                  : 3000
            }
            value={body[field]}
            onChange={(e) => change({ ...body, [field]: e.target.value })}
          />
        </label>
      ))}
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      <fieldset className="admin-experience-evidence">
        <legend>
          关联真实发布依据（已选择 {body.evidence_action_ids.length} 条）
        </legend>
        {sources.map((source) => (
          <label key={source.id} className="admin-checkbox">
            <input
              type="checkbox"
              checked={body.evidence_action_ids.includes(source.id)}
              onChange={(e) =>
                change({
                  ...body,
                  evidence_action_ids: e.target.checked
                    ? [...body.evidence_action_ids, source.id]
                    : body.evidence_action_ids.filter(
                        (id: string) => id !== source.id,
                      ),
                })
              }
            />
            <span>
              {source.target_name} ·{" "}
              {new Date(source.confirmed_at).toLocaleString("zh-CN")}
              <small>{source.excerpt}</small>
            </span>
          </label>
        ))}
        {!sources.length && (
          <p className="admin-help">
            尚无可引用的已确认真实发布。可以记录待验证做法，不能把连接测试或计划说成运营结果。
          </p>
        )}
      </fieldset>
      <label>
        审核结论
        <select
          value={body.review_status}
          onChange={(e) => change({ ...body, review_status: e.target.value })}
        >
          {Object.entries(reviews).map(([id, name]) => (
            <option key={id} value={id}>
              {name as string}
            </option>
          ))}
        </select>
      </label>
      <p className="admin-help">
        只有已发布且审核通过的经验进入当前业务的后续规划。审核通过不代表已证明获客提升；业务档案变更后需重新核对适用性。
      </p>
    </>
  );
}

export function Experiences({
  agents,
  options,
  readOnly,
  navigate,
}: {
  agents: Row[];
  options: Row;
  readOnly: boolean;
  navigate: (section: string) => void;
}) {
  const [aid, setAid] = useState(""),
    [platform, setPlatform] = useState("telegram"),
    [target, setTarget] = useState(""),
    [items, setItems] = useState<Row[]>([]),
    [editing, setEditing] = useState(""),
    [error, setError] = useState(""),
    [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0);
  const [promotion, setPromotion] = useState<Row | null>(null),
    [practice, setPractice] = useState(""),
    [conditions, setConditions] = useState("");
  useEffect(() => {
    let active = true;
    api(
      `/admin/strategy-experiences?platform=${platform}${aid ? "&aid=" + aid : ""}`,
    )
      .then((value) => {
        if (active) {
          setItems(value);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [aid, platform, revision]);
  const selectedAid = editing.split("/")[1] || aid;
  async function create(capture = false) {
    setBusy(true);
    setError("");
    try {
      const value = capture
        ? await api("/admin/strategy-experiences/capture", {
            agent_id: aid,
            target_id: target,
          })
        : await api("/admin/config", {
            key: `experiences/${aid}/${crypto.randomUUID()}`,
            expected_version: 0,
            action: "draft",
            body: {
              platform,
              target_id: target || null,
              title: "新的渠道经验",
            },
          });
      if (value) {
        setEditing(value.key);
        setRevision((v) => v + 1);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function beginPromotion(item: Row) {
    setBusy(true);
    try {
      const value = await api(
        "/admin/config?key=" +
          encodeURIComponent("strategies/" + item.body.platform),
      );
      setPromotion({ source: item, target: value });
      setPractice("");
      setConditions("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="admin-scope-controls">
        <AgentScope
          agents={agents}
          aid={aid}
          required
          change={(v) => {
            setAid(v);
            setTarget("");
            setEditing("");
            setPromotion(null);
          }}
        />
        <Platform
          options={options}
          value={platform}
          change={(v) => {
            setPlatform(v);
            setTarget("");
            setEditing("");
            setPromotion(null);
          }}
        />
        {aid && (
          <label>
            运营目标
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">选择目标以生成观察草稿</option>
              {(options.targets || [])
                .filter(
                  (t: Row) => t.agent_id === aid && t.platform === platform,
                )
                .map((t: Row) => (
                  <option value={t.id} key={t.id}>
                    {t.display_name}
                  </option>
                ))}
            </select>
          </label>
        )}
      </div>
      {!readOnly && (
        <div className="admin-toolbar">
          <button disabled={!aid || busy} onClick={() => void create()}>
            新增经验草稿
          </button>
          <button
            disabled={!aid || !target || busy}
            onClick={() => void create(true)}
          >
            从当前目标观察生成草稿
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {feedback && (
        <p role="status" className="admin-feedback">
          {feedback}
        </p>
      )}
      <section className="admin-editor">
        <header className="admin-section-heading">
          <div>
            <h2>渠道经验与审核</h2>
            <p>
              Agent
              仅在真实观察具备暂定方案倾向时自动提出经验草稿；没有数据时保留待验证状态。
            </p>
          </div>
        </header>
        <ul className="admin-readiness-list">
          {items.map((item) => {
            const body = item.draft || item.body;
            return (
              <li key={item.config_key}>
                <div>
                  <strong>{body.title}</strong>
                  <p>
                    {item.agent_name} · {item.draft ? "草稿 · " : ""}
                    {reviews[body.review_status]} ·{" "}
                    {body.evidence_action_ids.length
                      ? `${body.evidence_action_ids.length} 条发布依据`
                      : "待验证做法"}
                  </p>
                </div>
                <button onClick={() => setEditing(item.config_key)}>
                  查看与审核
                </button>
                {!readOnly &&
                  !item.draft &&
                  item.body?.review_status === "approved" && (
                    <button
                      disabled={busy}
                      onClick={() => void beginPromotion(item)}
                    >
                      提炼通用做法
                    </button>
                  )}
              </li>
            );
          })}
        </ul>
        {!items.length && (
          <p className="admin-empty">
            当前没有此渠道的经验记录。可先选择 Agent 创建待验证的经验草稿。
          </p>
        )}
      </section>
      {promotion && !readOnly && (
        <section className="admin-editor">
          <h2>核对可复用的内容</h2>
          <p className="admin-help">
            这里只复制你明确填写的做法与适用条件。不要带入特定品牌、客户和账号信息；提交后生成通用模板草稿。
          </p>
          <label>
            可跨业务复用的做法
            <textarea
              maxLength={3000}
              value={practice}
              onChange={(e) => setPractice(e.target.value)}
            />
          </label>
          <label>
            通用适用条件
            <textarea
              maxLength={2000}
              value={conditions}
              onChange={(e) => setConditions(e.target.value)}
            />
          </label>
          <div className="admin-toolbar">
            <button
              disabled={busy || !practice.trim() || !conditions.trim()}
              onClick={async () => {
                setBusy(true);
                try {
                  await api("/admin/strategy-experiences/promote", {
                    key: promotion.source.config_key,
                    expected_version: promotion.source.version,
                    target_key: promotion.target.key,
                    target_version: promotion.target.version,
                    reusable_practice: practice,
                    applicable_conditions: conditions,
                  });
                  setFeedback(
                    "已创建同渠道的通用模板草稿，请在渠道策略中核对并发布。",
                  );
                  setPromotion(null);
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              生成通用模板草稿
            </button>
            <button disabled={busy} onClick={() => setPromotion(null)}>
              取消提炼
            </button>
            <button onClick={() => navigate("strategies")}>前往渠道策略</button>
          </div>
        </section>
      )}
      {editing && (
        <ConfigEditor
          key={editing}
          configKey={editing}
          title="经验详情与审核"
          description="保存草稿不影响运行。审核结论与经验内容一起发布，历史版本完整保留。"
          readOnly={readOnly}
          onSaved={() => setRevision((v) => v + 1)}
        >
          {(body, change) => (
            <ExperienceForm
              body={body}
              change={change}
              aid={selectedAid}
              options={options}
            />
          )}
        </ConfigEditor>
      )}
    </>
  );
}
