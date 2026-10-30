"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../api-client";
import ConfigEditor, { clone, Row } from "./config-editor";
import { Lines } from "./strategy-form";

const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const categories: Row = {
  budget_warning: "额度接近上限",
  budget_exhausted: "额度已用尽",
  login_expired: "账号登录失效",
  repeated_failures: "连续失败",
  uncertain_action: "外部操作结果待核对",
  knowledge_expired: "知识资料过期",
  measurement_stale: "网站或转化回传过期",
};
const roles: Row = {
  owner: "所有者",
  operator: "运营成员",
  viewer: "查看成员",
};
const titles: Row = {
  models: "模型与工具",
  brand: "品牌与知识",
  runtime: "调度与异常",
  notifications: "通知配置",
};

function NumberField({
  label,
  value,
  min,
  max,
  onChange,
  help,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  help?: string;
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
          onChange(
            e.target.value === ""
              ? ("" as unknown as number)
              : Number(e.target.value),
          )
        }
      />
      {help && <small>{help}</small>}
    </label>
  );
}

function Hours({
  value,
  change,
  quiet = false,
}: {
  value: Row;
  change: (value: Row) => void;
  quiet?: boolean;
}) {
  const label = quiet ? "免打扰" : "工作时段";
  return (
    <>
      <label className="admin-checkbox">
        <input
          type="checkbox"
          checked={value.enabled}
          onChange={(e) => change({ ...value, enabled: e.target.checked })}
        />
        启用{label}
      </label>
      {value.enabled && (
        <>
          <label>
            {label}时区
            <input
              required
              maxLength={100}
              value={value.timezone}
              list="admin-timezones"
              onChange={(e) => change({ ...value, timezone: e.target.value })}
            />
            <datalist id="admin-timezones">
              <option value="Asia/Shanghai" />
              <option value="UTC" />
              <option value="America/New_York" />
              <option value="Europe/London" />
              <option value="Asia/Tokyo" />
            </datalist>
          </label>
          <div className="admin-field-pair">
            <label>
              开始时间
              <input
                required
                type="time"
                value={value.start}
                onChange={(e) => change({ ...value, start: e.target.value })}
              />
            </label>
            <label>
              结束时间
              <input
                required
                type="time"
                value={value.end}
                onChange={(e) => change({ ...value, end: e.target.value })}
              />
            </label>
          </div>
          <fieldset className="admin-choice-group">
            <legend>生效星期</legend>
            {weekdays.map((day, index) => (
              <label className="admin-checkbox" key={day}>
                <input
                  type="checkbox"
                  checked={value.weekdays.includes(index)}
                  onChange={(e) =>
                    change({
                      ...value,
                      weekdays: e.target.checked
                        ? [...value.weekdays, index].sort()
                        : value.weekdays.filter((n: number) => n !== index),
                    })
                  }
                />
                {day}
              </label>
            ))}
          </fieldset>
          <p className="admin-help">
            结束时间早于开始时间表示跨夜。
            {quiet
              ? "普通通知在免打扰结束后出现。"
              : "限制自动运营和新发布，对话和已有外部提交的核对仍可继续。"}
          </p>
        </>
      )}
    </>
  );
}

function Models({
  body,
  change,
  options,
  onSelection,
}: {
  body: Row;
  change: (body: Row) => void;
  options: Row;
  onSelection: (models: string[]) => void;
}) {
  const selection = JSON.stringify([
    body.default_model,
    ...Object.values(body.task_models).filter(Boolean),
  ]);
  useEffect(() => {
    onSelection(JSON.parse(selection));
  }, [selection, onSelection]);
  const models = [
    ...new Set([
      ...(options.models || []),
      body.default_model,
      ...Object.values(body.task_models).filter(Boolean),
    ]),
  ] as string[];
  const field = (key: string, value: any) => change({ ...body, [key]: value });
  const modelOptions = () =>
    ["deepseek", "openai"].map((provider) => (
      <optgroup
        key={provider}
        label={provider === "openai" ? "OpenAI" : "DeepSeek"}
      >
        {models
          .filter(
            (name) =>
              (name.startsWith("openai:") ? "openai" : "deepseek") === provider,
          )
          .map((name) => (
            <option key={name} value={name}>
              {name.replace(/^(openai|deepseek):/, "")}
            </option>
          ))}
      </optgroup>
    ));
  return (
    <>
      <label>
        默认模型
        <select
          value={body.default_model}
          onChange={(e) => field("default_model", e.target.value)}
        >
          {modelOptions()}
        </select>
        <small>
          DeepSeek 与 OpenAI
          分别使用服务端凭据。下面的任务可以各自选择服务；关闭自动切换，缺少凭据会明确提示。
        </small>
      </label>
      <div className="admin-subheading">
        <h3>按任务选择模型</h3>
      </div>
      <div className="admin-step-fields">
        {[
          ["chat", "对话"],
          ["planning", "运营规划与内容"],
          ["research", "研究与分析"],
        ].map(([key, label]) => (
          <label key={key}>
            {label}
            <select
              value={body.task_models[key]}
              onChange={(e) =>
                field("task_models", {
                  ...body.task_models,
                  [key]: e.target.value,
                })
              }
            >
              <option value="">跟随默认模型</option>
              {modelOptions()}
            </select>
          </label>
        ))}
      </div>
      <div className="admin-subheading">
        <h3>调用与工具用量</h3>
      </div>
      <div className="admin-field-pair">
        <NumberField
          label="单次响应等待上限（秒）"
          value={body.timeout_seconds}
          min={15}
          max={120}
          onChange={(v) => field("timeout_seconds", v)}
        />
        <NumberField
          label="每次输出 token 上限"
          value={body.max_output_tokens}
          min={256}
          max={8000}
          onChange={(v) => field("max_output_tokens", v)}
        />
      </div>
      <div className="admin-field-pair">
        <NumberField
          label="每轮最多模型请求次数"
          value={body.max_model_requests}
          min={1}
          max={10}
          onChange={(v) => field("max_model_requests", v)}
        />
        <NumberField
          label="每轮最多工具操作次数"
          value={body.max_tool_calls}
          min={0}
          max={20}
          onChange={(v) => field("max_tool_calls", v)}
        />
      </div>
      <fieldset className="admin-choice-group">
        <legend>可用工具</legend>
        {[
          ["public_web", "公开网页读取"],
          ["advanced_research", "进阶网页研究"],
          ["community_research", "社群线索研究"],
          ["media_creation", "原创图文与视频制作"],
        ].map(([key, label]) => (
          <label className="admin-checkbox" key={key}>
            <input
              type="checkbox"
              checked={body.tools[key]}
              onChange={(e) =>
                field("tools", { ...body.tools, [key]: e.target.checked })
              }
            />
            {label}
          </label>
        ))}
      </fieldset>
      <p className="admin-help">
        工具开关用于限制实际调用，账号操作仍受原有授权约束。
        {options.advanced_research_available === false
          ? "本机尚未启用进阶研究环境，勾选后也需环境就绪才能使用。"
          : ""}
      </p>
    </>
  );
}

function Brand({ body, change }: { body: Row; change: (body: Row) => void }) {
  const [importError, setImportError] = useState("");
  const [expandedDocuments, setExpandedDocuments] = useState<
    Record<string, boolean>
  >({});
  const field = (key: string, value: any) => change({ ...body, [key]: value });
  function documentField(index: number, key: string, value: any) {
    const next = clone(body);
    next.documents[index][key] = value;
    change(next);
  }
  function addDocument(title = "新知识资料", content = "") {
    const id = crypto.randomUUID();
    setExpandedDocuments((previous) => ({ ...previous, [id]: true }));
    field("documents", [
      ...body.documents,
      {
        id,
        title,
        content,
        source: "用户提供",
        status: "pending",
        valid_until: null,
        tags: [],
      },
    ]);
  }
  return (
    <>
      <div className="admin-field-pair">
        <label>
          品牌名称
          <input
            maxLength={100}
            value={body.name}
            onChange={(e) => field("name", e.target.value)}
          />
        </label>
        <label>
          内容语言
          <input
            maxLength={100}
            value={body.language}
            onChange={(e) => field("language", e.target.value)}
          />
        </label>
      </div>
      <label>
        表达语气
        <textarea
          maxLength={2000}
          rows={2}
          value={body.voice}
          onChange={(e) => field("voice", e.target.value)}
        />
      </label>
      <label>
        品牌定位
        <textarea
          maxLength={4000}
          rows={3}
          value={body.positioning}
          onChange={(e) => field("positioning", e.target.value)}
        />
      </label>
      <label>
        目标人群
        <textarea
          maxLength={2000}
          rows={2}
          value={body.audience}
          onChange={(e) => field("audience", e.target.value)}
        />
      </label>
      <Lines
        label="已确认的产品事实"
        value={body.facts}
        onChange={(v) => field("facts", v)}
        help="每行一条。需要定期复核的价格、活动和时效信息建议放入带有效期的知识资料。"
      />
      <Lines
        label="禁用表达"
        value={body.forbidden_phrases}
        onChange={(v) => field("forbidden_phrases", v)}
        help="发布前会检查这些词句；不区分英文字母大小写。"
      />
      <Lines
        label="应避免的承诺和说法"
        value={body.claims_to_avoid}
        onChange={(v) => field("claims_to_avoid", v)}
      />
      <label>
        素材使用与视觉要求
        <textarea
          rows={3}
          maxLength={4000}
          value={body.asset_guidance}
          onChange={(e) => field("asset_guidance", e.target.value)}
        />
      </label>
      <div className="admin-subheading">
        <h3>知识资料</h3>
        <button
          type="button"
          disabled={body.documents.length >= 40}
          onClick={() => addDocument()}
        >
          添加资料
        </button>
      </div>
      <label className="admin-file-import">
        导入文本资料
        <input
          type="file"
          accept=".txt,.md,text/plain,text/markdown"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            setImportError("");
            if (file.size > 60000 || body.documents.length >= 40) {
              setImportError(
                "请选择 60 KB 以内的文本文件，知识资料最多 40 条。",
              );
              return;
            }
            const text = await file.text();
            if (text.length > 20000) {
              setImportError("单条资料不能超过 20000 字，请拆分后导入。");
              return;
            }
            addDocument(file.name.slice(0, 160), text);
            e.target.value = "";
          }}
        />
        <small>支持 TXT 和 Markdown，导入后先保存为待确认。</small>
      </label>
      {importError && (
        <p role="alert" className="error-feedback">
          {importError}
        </p>
      )}
      {!body.documents.length && (
        <p className="admin-help">
          尚无知识资料。Agent 仍可使用自己的业务档案和已确认事实。
        </p>
      )}
      <div className="admin-plans">
        {body.documents.map((doc: Row, index: number) => (
          <details
            key={doc.id}
            open={expandedDocuments[doc.id] || false}
            onToggle={(event) => {
              const open = event.currentTarget.open;
              setExpandedDocuments((previous) =>
                previous[doc.id] === open
                  ? previous
                  : { ...previous, [doc.id]: open },
              );
            }}
          >
            <summary>
              <span>{doc.title || "未命名资料"}</span>
              <span className="muted small">
                {
                  { pending: "待确认", approved: "已确认", disabled: "已停用" }[
                    doc.status as string
                  ]
                }
              </span>
            </summary>
            <div className="admin-plan-fields">
              <label>
                资料标题
                <input
                  required
                  maxLength={160}
                  value={doc.title}
                  onChange={(e) =>
                    documentField(index, "title", e.target.value)
                  }
                />
              </label>
              <label>
                资料正文
                <textarea
                  required
                  maxLength={20000}
                  rows={7}
                  value={doc.content}
                  onChange={(e) =>
                    documentField(index, "content", e.target.value)
                  }
                />
              </label>
              <label>
                资料来源
                <input
                  maxLength={2000}
                  value={doc.source}
                  onChange={(e) =>
                    documentField(index, "source", e.target.value)
                  }
                />
                <small>可填写资料名称或网址，保存来源不会自动核验网页。</small>
              </label>
              <div className="admin-field-pair">
                <label>
                  确认状态
                  <select
                    value={doc.status}
                    onChange={(e) =>
                      documentField(index, "status", e.target.value)
                    }
                  >
                    <option value="pending">待确认，暂不提供给 Agent</option>
                    <option value="approved">已确认，可用于运营</option>
                    <option value="disabled">停用</option>
                  </select>
                </label>
                <label>
                  有效期至（北京时间）
                  <input
                    type="date"
                    value={doc.valid_until || ""}
                    onChange={(e) =>
                      documentField(
                        index,
                        "valid_until",
                        e.target.value || null,
                      )
                    }
                  />
                  <small>留空表示不设置到期日。过期后自动排除。</small>
                </label>
              </div>
              <label>
                标签
                <input
                  maxLength={500}
                  value={doc.tags.join("、")}
                  onChange={(e) =>
                    documentField(
                      index,
                      "tags",
                      e.target.value
                        .split(/[、,，]/)
                        .map((v) => v.trim())
                        .filter(Boolean),
                    )
                  }
                />
              </label>
              <button
                className="text-button"
                type="button"
                onClick={() =>
                  field(
                    "documents",
                    body.documents.filter((_: Row, i: number) => i !== index),
                  )
                }
              >
                移除此资料
              </button>
            </div>
          </details>
        ))}
      </div>
      <p className="admin-help">
        仅已确认且未过期的资料会提供给 Agent。每次选取最多 6
        条相关资料，每条最多 6000 字；发布新品牌配置会撤回旧方向的排队内容。
      </p>
    </>
  );
}

function Runtime({ body, change }: { body: Row; change: (body: Row) => void }) {
  const field = (key: string, value: any) => change({ ...body, [key]: value });
  return (
    <>
      <div className="admin-subheading">
        <h3>自动运营时段</h3>
      </div>
      <Hours
        value={body.work_hours}
        change={(value) => field("work_hours", value)}
      />
      <NumberField
        label="同时工作的 Agent 上限"
        value={body.max_concurrent_agents}
        min={1}
        max={12}
        onChange={(v) => field("max_concurrent_agents", v)}
        help="同一个 Agent 同时只执行一项自动工作；独立配置仍受工作室总并发上限约束。"
      />
      <div className="admin-subheading">
        <h3>异常恢复</h3>
      </div>
      <div className="admin-field-pair">
        <NumberField
          label="发布任务最多尝试次数"
          value={body.retry_attempts}
          min={1}
          max={10}
          onChange={(v) => field("retry_attempts", v)}
          help="包含首次尝试，仅适用于尚未提交的可重试故障。"
        />
        <NumberField
          label="重试基础间隔（秒）"
          value={body.retry_interval_seconds}
          min={5}
          max={600}
          onChange={(v) => field("retry_interval_seconds", v)}
        />
      </div>
      <NumberField
        label="同类连续失败多少次后暂停"
        value={body.pause_after_failures}
        min={1}
        max={20}
        onChange={(v) => field("pause_after_failures", v)}
        help="对话、规划和发布分别累计；成功会清零，恢复运营后重新累计。"
      />
      <p className="admin-help">
        发布任务开始时保留重试配置版本。外部操作结果不确定时只核对已有提交，不重新发布；工作时段和并发变化用于后续自动工作。
      </p>
    </>
  );
}

function Notifications({
  body,
  change,
  members,
}: {
  body: Row;
  change: (body: Row) => void;
  members: Row[];
}) {
  const field = (key: string, value: any) => change({ ...body, [key]: value });
  function toggle(key: string, value: string, checked: boolean) {
    field(
      key,
      checked
        ? [...body[key], value]
        : body[key].filter((item: string) => item !== value),
    );
  }
  return (
    <>
      <p className="admin-help">
        当前使用站内通知，可在后台及工作台的通知入口查看。
      </p>
      <label className="admin-checkbox">
        <input
          type="checkbox"
          checked={body.enabled}
          onChange={(e) => field("enabled", e.target.checked)}
        />
        启用站内通知
      </label>
      <fieldset className="admin-choice-group">
        <legend>提醒事项</legend>
        {Object.entries(categories).map(([key, label]) => (
          <label className="admin-checkbox" key={key}>
            <input
              type="checkbox"
              checked={body.categories.includes(key)}
              onChange={(e) => toggle("categories", key, e.target.checked)}
            />
            {label as string}
          </label>
        ))}
      </fieldset>
      <NumberField
        label="额度达到多少百分比时提醒"
        value={body.budget_warning_percent}
        min={1}
        max={99}
        onChange={(v) => field("budget_warning_percent", v)}
        help="按已用量与调用中预留之和计算。"
      />
      <fieldset className="admin-choice-group">
        <legend>按成员角色接收</legend>
        {Object.entries(roles).map(([key, label]) => (
          <label className="admin-checkbox" key={key}>
            <input
              type="checkbox"
              checked={body.recipient_roles.includes(key)}
              onChange={(e) => toggle("recipient_roles", key, e.target.checked)}
            />
            {label as string}
          </label>
        ))}
      </fieldset>
      <fieldset className="admin-choice-group">
        <legend>或指定工作室成员</legend>
        {[
          ...new Set([
            ...members.map((m) => m.subject),
            ...body.recipient_subjects,
          ]),
        ].map((subject) => (
          <label className="admin-checkbox" key={subject}>
            <input
              type="checkbox"
              checked={body.recipient_subjects.includes(subject)}
              onChange={(e) =>
                toggle("recipient_subjects", subject, e.target.checked)
              }
            />
            {subject === "local" ? "本地所有者" : subject}
          </label>
        ))}
      </fieldset>
      <div className="admin-subheading">
        <h3>免打扰与重复提醒</h3>
      </div>
      <Hours
        quiet
        value={body.quiet_hours}
        change={(value) => field("quiet_hours", value)}
      />
      <label className="admin-checkbox">
        <input
          type="checkbox"
          checked={body.urgent_during_quiet}
          onChange={(e) => field("urgent_during_quiet", e.target.checked)}
        />
        额度耗尽、连续失败等紧急事项不受免打扰限制
      </label>
      <NumberField
        label="异常恢复后再次出现的最短提醒间隔（分钟）"
        value={body.recurrence_cooldown_minutes}
        min={1}
        max={1440}
        onChange={(v) => field("recurrence_cooldown_minutes", v)}
      />
      <p className="admin-help">
        同一次异常持续期间只提醒一次。通知接收范围在生成时记录，每位成员分别维护自己的已读状态。
      </p>
    </>
  );
}

export function NotificationInbox({ aid = "" }: { aid?: string }) {
  const [items, setItems] = useState<Row[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false),
    [more, setMore] = useState(false);
  const load = useCallback(async (before?: Row) => {
    setBusy(true);
    try {
      const rows = await api(
        "/admin/notifications" +
          (before
            ? `?before=${encodeURIComponent(before.created_at)}&before_id=${before.id}`
            : ""),
      );
      setItems((prior) => (before ? [...prior, ...rows] : rows));
      setMore(rows.length === 50);
      setLoaded(true);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, [load]);
  async function read(id: string) {
    try {
      await api(`/admin/notifications/${id}/read`, {});
      setItems((prior) =>
        prior.map((item) =>
          item.id === id
            ? { ...item, read_at: new Date().toISOString() }
            : item,
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const visible = items.filter((item) => !aid || item.agent_id === aid);
  return (
    <section className="admin-editor">
      <header className="admin-section-heading">
        <div>
          <h2>站内通知</h2>
          <p>真实异常及恢复状态，已读记录仅作用于你自己。</p>
        </div>
        <button type="button" disabled={busy} onClick={() => void load()}>
          刷新通知
        </button>
      </header>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {!loaded && !error && <p role="status">正在读取通知…</p>}
      {loaded && !visible.length && (
        <p className="admin-empty">当前没有发给你的通知。</p>
      )}
      <ul className="admin-notification-list">
        {visible.map((item) => (
          <li key={item.id}>
            <div className="admin-notification-heading">
              <strong>{item.title}</strong>
              <span className="muted small">
                {item.resolved_at ? "已恢复" : item.read_at ? "已读" : "未读"}
              </span>
            </div>
            <p>{item.message}</p>
            <div className="admin-notification-footer">
              <time>
                {new Date(item.created_at).toLocaleString("zh-CN", {
                  timeZone: "Asia/Shanghai",
                })}
              </time>
              {item.agent_id && (
                <a href={`/?agent=${item.agent_id}`}>查看 Agent</a>
              )}
              {!item.read_at && (
                <button type="button" onClick={() => void read(item.id)}>
                  标为已读
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {more && items.length > 0 && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void load(items[items.length - 1])}
        >
          加载更早通知
        </button>
      )}
    </section>
  );
}

export default function OperationalSettings({
  kind,
  agents,
  readOnly,
}: {
  kind: string;
  agents: Row[];
  readOnly: boolean;
}) {
  const [aid, setAid] = useState(""),
    [options, setOptions] = useState<Row>({}),
    [members, setMembers] = useState<Row[]>([]),
    [status, setStatus] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [checking, setChecking] = useState(""),
    [connection, setConnection] = useState<Row>({});
  const [revision, setRevision] = useState(0);
  const [checkModels, setCheckModels] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    setError("");
    setStatus(null);
    const requests: Promise<any>[] = [];
    if (kind === "models")
      requests.push(
        api("/admin/model-options").then((value) => {
          if (active) setOptions(value);
        }),
      );
    if (kind === "notifications" && !readOnly)
      requests.push(
        api("/admin/notification-recipients").then((value) => {
          if (active) setMembers(value);
        }),
      );
    if (kind === "runtime")
      requests.push(
        api("/admin/runtime-status" + (aid ? "?aid=" + aid : "")).then(
          (value) => {
            if (active) setStatus(value);
          },
        ),
      );
    Promise.all(requests).catch((e) => {
      if (active) setError(e.message);
    });
    return () => {
      active = false;
    };
  }, [kind, aid, readOnly, revision]);
  const configKey = `${kind}/${aid || "default"}`;
  return (
    <>
      <div className="admin-toolbar">
        <label className="admin-search">
          适用范围
          <select value={aid} onChange={(e) => setAid(e.target.value)}>
            <option value="">工作室默认配置</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && (
        <p className="admin-feedback error-feedback" role="alert">
          {error}
          <button onClick={() => setRevision((v) => v + 1)}>
            重试读取状态
          </button>
        </p>
      )}
      {kind === "models" && (
        <section className="admin-model-services" aria-label="模型服务状态">
          {!options.providers && <p role="status">正在读取模型服务状态…</p>}
          {(options.providers || []).map((provider: Row) => (
            <div className="admin-service-status" key={provider.id}>
              <div>
                <strong>{provider.name}</strong>
                <p>
                  {provider.credential_configured
                    ? "服务端已配置凭据，尚未检查连接。"
                    : "尚未配置服务端凭据，请由部署方补充。"}
                </p>
                <small>密钥只在服务端使用，不进入配置版本或浏览器。</small>
                {connection[provider.id] && (
                  <p role="status">{connection[provider.id]}</p>
                )}
              </div>
              {!readOnly && (
                <button
                  type="button"
                  disabled={!!checking}
                  onClick={async () => {
                    setChecking(provider.id);
                    try {
                      const result = await api(
                        `/admin/model-check?provider=${provider.id}${aid ? "&aid=" + aid : ""}`,
                        { models: checkModels },
                      );
                      setConnection((previous) => ({
                        ...previous,
                        [provider.id]: result.message,
                      }));
                    } catch (e) {
                      setConnection((previous) => ({
                        ...previous,
                        [provider.id]: (e as Error).message,
                      }));
                    } finally {
                      setChecking("");
                    }
                  }}
                >
                  {checking === provider.id
                    ? "检查中…"
                    : `检查 ${provider.name} 连接`}
                </button>
              )}
            </div>
          ))}
        </section>
      )}
      {kind === "runtime" && status && (
        <section className="admin-service-status">
          <div>
            <strong>
              {status.window.allowed
                ? "当前处于允许工作的时段"
                : "当前处于工作时段之外"}
            </strong>
            <p>
              {status.window.allowed
                ? `当前有 ${status.active_work.length} 项自动工作占用运行名额。`
                : `下次可工作时间：${new Date(status.window.next_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}（北京时间）`}
            </p>
          </div>
          {status.failures
            .filter((row: Row) => row.failures > 0)
            .map((row: Row) => (
              <p key={`${row.agent_id}/${row.category}`}>
                {row.name} ·{" "}
                {{ chat: "对话", planning: "运营规划", publish: "发布" }[
                  row.category as string
                ] || row.category}
                连续失败 {row.failures} 次
                {row.next_retry_at
                  ? `，下次可重试：${new Date(row.next_retry_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`
                  : ""}
              </p>
            ))}
        </section>
      )}
      {kind === "notifications" && <NotificationInbox aid={aid} />}
      <ConfigEditor
        key={configKey}
        configKey={configKey}
        title={`${aid ? agents.find((a) => a.id === aid)?.name + " · " : ""}${titles[kind]}`}
        description={
          kind === "brand"
            ? "发布后用于后续工作，旧方向的排队内容会撤回。历史版本保留完整资料。"
            : kind === "models"
              ? "按任务选择模型并控制实际可用工具，已用额度和调用中预留继续保留。"
              : kind === "runtime"
                ? "工作时段、并发和失败规则接入自动运营，发布任务保留开始时的重试配置。"
                : "配置站内提醒、接收范围和免打扰。只通知真实状态变化。"
        }
        readOnly={readOnly}
        onSaved={() => setRevision((v) => v + 1)}
      >
        {(body, change) =>
          kind === "models" ? (
            <Models
              body={body}
              change={change}
              options={options}
              onSelection={setCheckModels}
            />
          ) : kind === "brand" ? (
            <>
              <p className="admin-help">
                {aid
                  ? "这里维护此业务的品牌名称、产品事实和知识来源。"
                  : "工作室默认维护通用表达规范。品牌名称、产品事实和官网资料请在上方切换到对应 Agent 后查看与维护。"}
              </p>
              <Brand body={body} change={change} />
            </>
          ) : kind === "runtime" ? (
            <Runtime body={body} change={change} />
          ) : (
            <Notifications body={body} change={change} members={members} />
          )
        }
      </ConfigEditor>
    </>
  );
}
