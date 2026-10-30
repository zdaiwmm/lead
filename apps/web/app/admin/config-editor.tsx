"use client";

import { ReactNode, useEffect, useState } from "react";
import { api, getWorkspace } from "../api-client";

export type Row = Record<string, any>;
export const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

const actionNames: Row = {
  draft: "保存草稿",
  publish: "发布",
  rollback: "回滚",
};

function differences(
  before: any,
  after: any,
  path = "",
): { path: string; before: any; after: any }[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (
    Array.isArray(before) &&
    Array.isArray(after) &&
    [...before, ...after].some((value) => value && typeof value === "object")
  ) {
    return Array.from(
      { length: Math.max(before.length, after.length) },
      (_, index) => index,
    ).flatMap((index) =>
      differences(before[index], after[index], `${path}.${index + 1}`),
    );
  }
  if (
    before &&
    after &&
    typeof before === "object" &&
    typeof after === "object" &&
    !Array.isArray(before) &&
    !Array.isArray(after)
  ) {
    return [
      ...new Set([...Object.keys(before), ...Object.keys(after)]),
    ].flatMap((key) =>
      differences(before[key], after[key], path ? path + "." + key : key),
    );
  }
  return [{ path, before, after }];
}

const fieldNames: Row = {
  openai_models: "OpenAI 可选模型",
  allow_publishing: "允许发布",
  allow_community_growth: "允许社群运营",
  daily_publish_limit: "额外发布上限",
  objective: "转化目标",
  objective_definition: "业务完成条件",
  attribution_window_days: "归因窗口（天）",
  engagement_seconds: "有效访问时长（秒）",
  freshness_hours: "数据新鲜度（小时）",
  title: "经验名称",
  target_id: "账号目标",
  business_profile_id: "业务档案",
  plan_id: "关联方案",
  applicable_conditions: "适用条件",
  observation: "实际观察",
  proposed_change: "建议做法",
  evidence_action_ids: "真实发布依据",
  review_status: "审核结论",
  body: "生效配置",
  draft: "草稿",
  inherited: "继承默认",
  daily_token_limit: "每日额度",
  steps: "步骤名称",
  fields: "输入字段",
  strategies: "策略选项",
  default_platform: "默认渠道",
  label: "名称",
  placeholder: "输入示例",
  required: "必填",
  default: "默认值",
  help: "补充说明",
  cadence: "运营节奏",
  maturity_days: "观察天数",
  name: "名称",
  plans: "内容方案",
  setup: "准备步骤",
  constraints: "运营边界",
  sources: "参考来源",
  conversion_path: "转化路径",
  experience_notes: "运营经验",
  evidence_refs: "经验依据",
  target: "运营目标",
  platform: "渠道",
  product_name: "产品名称",
  product_url: "产品网址",
  description: "产品介绍",
  audience: "目标客户",
  goal: "运营目标",
  content_expectations: "内容期望",
  id: "标识",
  formats: "内容格式",
  assets: "素材要求",
  workflow: "执行步骤",
  first_batch: "首批选题",
  template: "内容模板",
  cta: "行动引导",
  destination_path: "产品承接页面路径",
  hypothesis: "待验证假设",
  experiment: "尝试方式",
  iterate: "复盘方法",
  schedule: "运营排期",
  default_model: "默认模型",
  task_models: "任务模型",
  chat: "对话",
  planning: "规划",
  research: "研究",
  timeout_seconds: "响应等待秒数",
  max_output_tokens: "输出 token 上限",
  max_model_requests: "模型请求次数",
  max_tool_calls: "工具操作次数",
  tools: "可用工具",
  public_web: "公开网页读取",
  advanced_research: "进阶网页研究",
  community_research: "社群线索研究",
  media_creation: "原创素材制作",
  voice: "表达语气",
  language: "语言",
  positioning: "品牌定位",
  facts: "产品事实",
  forbidden_phrases: "禁用表达",
  claims_to_avoid: "应避免的说法",
  asset_guidance: "素材要求",
  documents: "知识资料",
  content: "正文",
  source: "来源",
  status: "确认状态",
  valid_until: "有效期",
  tags: "标签",
  work_hours: "工作时段",
  timezone: "时区",
  weekdays: "星期",
  enabled: "启用",
  start: "开始",
  end: "结束",
  max_concurrent_agents: "并发 Agent 上限",
  retry_attempts: "最多尝试次数",
  retry_interval_seconds: "重试间隔",
  pause_after_failures: "失败暂停阈值",
  categories: "提醒事项",
  budget_warning_percent: "额度提醒比例",
  recipient_roles: "接收角色",
  recipient_subjects: "指定成员",
  quiet_hours: "免打扰",
  urgent_during_quiet: "紧急事项免打扰例外",
  recurrence_cooldown_minutes: "再次提醒间隔",
};
function display(value: any): string {
  if (value === null || value === undefined) return "未设置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.map((v) => display(v)).join("\n");
  return typeof value === "object"
    ? Object.entries(value)
        .map(([key, entry]) => `${fieldNames[key] || key}：${display(entry)}`)
        .join("\n")
    : String(value) || "空";
}

function configTitle(key: string) {
  const parts = key.split("/");
  const title: Row = {
    "model-catalog": "可选模型",
    attribution: "数据与归因",
    experiences: "渠道经验",
    models: "模型与工具",
    brand: "品牌与知识",
    runtime: "调度与异常",
    notifications: "通知配置",
  };
  if (title[parts[0]])
    return (
      title[parts[0]] +
      (parts[1] === "default"
        ? " · 工作室默认"
        : " · Agent " + parts[1].slice(0, 8))
    );
  if (parts[0] === "onboarding") return "创建引导";
  if (parts[0] === "channel-rules")
    return `${parts[1]} · ${parts.length > 3 ? "账号目标规则" : parts.length > 2 ? "Agent 规则" : "渠道默认规则"}`;
  if (parts[0] === "budgets")
    return parts[1] === "default"
      ? "工作室默认额度"
      : `Agent 额度 · ${parts[1].slice(0, 8)}`;
  return `${parts[1]} · ${parts.length > 3 ? "账号目标策略" : parts.length > 2 ? "Agent 策略" : "通用策略"}`;
}

function revisionDifferences(row: Row) {
  if (row.action === "draft")
    return differences(
      row.before_state.draft ?? row.before_state.body,
      row.after_state.draft,
      "draft",
    );
  return differences(
    { body: row.before_state.body, inherited: row.before_state.inherited },
    { body: row.after_state.body, inherited: row.after_state.inherited },
  );
}

export function History({
  rows,
  onRollback,
}: {
  rows: Row[];
  onRollback?: (version: number) => void;
}) {
  return (
    <div className="admin-history">
      {!rows.length && (
        <p className="admin-empty">
          尚无配置变更。首次保存后会记录版本、修改人及前后内容。
        </p>
      )}
      {rows.map((row) => (
        <details className="admin-history-row" key={row.id}>
          <summary>
            <span>
              {actionNames[row.action]}{" "}
              <span className="admin-version">v{row.version}</span>
              <span className="admin-version">
                {configTitle(row.config_key)}
              </span>
            </span>
            <time title="北京时间">
              {new Date(row.created_at).toLocaleString("zh-CN", {
                timeZone: "Asia/Shanghai",
              })}
            </time>
          </summary>
          <div className="admin-history-detail">
            <p className="muted small">
              {configTitle(row.config_key)} ·{" "}
              {row.actor_ref.startsWith("local:")
                ? "本地所有者"
                : row.actor_ref}
            </p>
            {row.note && <p>{row.note}</p>}
            {revisionDifferences(row).map((diff) => (
              <div className="admin-difference" key={diff.path}>
                <strong>
                  {diff.path
                    .split(".")
                    .map((part) => fieldNames[part] || part)
                    .join(" / ")}
                </strong>
                <div>
                  <pre aria-label="修改前">{display(diff.before)}</pre>
                  <span aria-hidden="true">→</span>
                  <pre aria-label="修改后">{display(diff.after)}</pre>
                </div>
              </div>
            ))}
            {onRollback && row.action !== "draft" && (
              <button type="button" onClick={() => onRollback(row.version)}>
                回滚至此版本
              </button>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}

export default function ConfigEditor({
  configKey,
  title,
  description,
  readOnly,
  drafts = true,
  children,
  onSaved,
}: {
  configKey: string;
  title: string;
  description: string;
  readOnly: boolean;
  drafts?: boolean;
  children: (body: Row, change: (value: Row) => void) => ReactNode;
  onSaved?: () => void;
}) {
  const [config, setConfig] = useState<Row | null>(null),
    [body, setBody] = useState<Row>({}),
    [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [note, setNote] = useState(""),
    [dirty, setDirty] = useState(false);
  const [retry, setRetry] = useState(0);
  const [pendingRollback, setPendingRollback] = useState<number | null>(null);
  const [discardRequested, setDiscardRequested] = useState(false);
  const localKey = `lead.admin-edit.${getWorkspace()}.${configKey}`;
  useEffect(() => {
    let active = true;
    setError("");
    Promise.all([
      api("/admin/config?key=" + encodeURIComponent(configKey)),
      api("/admin/history?key=" + encodeURIComponent(configKey)),
    ])
      .then(([c, h]) => {
        if (active) {
          let saved: Row | null = null;
          try {
            saved = JSON.parse(localStorage.getItem(localKey) || "null");
          } catch {}
          setConfig(saved ? { ...c, version: saved.version } : c);
          setBody(clone(saved?.body ?? c.draft ?? c.body));
          setRows(h);
          setDirty(!!saved);
          setNote(saved?.note || "");
          setMessage(saved ? "已恢复此配置未保存的修改。" : "");
          if (saved && saved.version !== c.version)
            setError(
              "配置已有新版本。已保留你的修改，发布前请刷新并重新核对。",
            );
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [configKey, retry, localKey]);
  useEffect(() => {
    if (config && dirty) {
      try {
        localStorage.setItem(
          localKey,
          JSON.stringify({ version: config.version, body, note }),
        );
      } catch {}
    }
  }, [config, dirty, body, note, localKey]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  async function save(
    action: string,
    rollbackVersion?: number,
    resetToDefault = false,
  ) {
    if (!config) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const next = await api("/admin/config", {
        key: configKey,
        body,
        expected_version: config.version,
        action,
        note,
        rollback_version: rollbackVersion,
        reset_to_default: resetToDefault,
      });
      try {
        localStorage.removeItem(localKey);
      } catch {}
      setConfig(next);
      setBody(clone(next.draft ?? next.body));
      setDirty(false);
      setPendingRollback(null);
      setNote("");
      setMessage(
        action === "draft" ? "草稿已保存，运行配置未改变。" : "配置已发布。",
      );
      setRows(await api("/admin/history?key=" + encodeURIComponent(configKey)));
      onSaved?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="admin-editor">
      <header className="admin-section-heading">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        {config && (
          <span className="admin-version">
            {config.published_version
              ? `生效 v${config.published_version}`
              : "继承默认"}
            {config.draft ? " · 有草稿" : ""}
          </span>
        )}
      </header>
      {error && (
        <p className="admin-feedback error-feedback" role="alert">
          {error}{" "}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              if (dirty) setDiscardRequested(true);
              else {
                try {
                  localStorage.removeItem(localKey);
                } catch {}
                setDirty(false);
                setRetry((v) => v + 1);
              }
            }}
          >
            刷新配置
          </button>
        </p>
      )}
      {discardRequested && (
        <div className="admin-inline-review" role="status">
          <p>刷新会放弃当前未保存的修改，重新读取最新配置。</p>
          <button
            type="button"
            onClick={() => {
              try {
                localStorage.removeItem(localKey);
              } catch {}
              setDirty(false);
              setDiscardRequested(false);
              setRetry((v) => v + 1);
            }}
          >
            放弃修改并刷新
          </button>
          <button type="button" onClick={() => setDiscardRequested(false)}>
            继续编辑
          </button>
        </div>
      )}
      {!config ? (
        !error && (
          <div className="admin-loading" role="status">
            正在读取配置…
          </div>
        )
      ) : (
        <>
          <form
            onInvalidCapture={(event) => {
              (event.target as HTMLElement)
                .closest("details")
                ?.setAttribute("open", "");
            }}
            onSubmit={(e) => {
              e.preventDefault();
              void save("publish");
            }}
          >
            <fieldset className="admin-form-fields" disabled={readOnly || busy}>
              {children(body, (next) => {
                setBody(next);
                setDirty(true);
                setMessage("");
              })}
              {!readOnly && (
                <label className="admin-note">
                  变更说明{" "}
                  <input
                    maxLength={1000}
                    value={note}
                    placeholder="说明本次调整的原因，便于后续追溯"
                    onChange={(e) => setNote(e.target.value)}
                  />
                </label>
              )}
            </fieldset>
            {!readOnly && (
              <div className="admin-save-bar">
                <span className="muted small">
                  {dirty
                    ? "有未保存的修改"
                    : config.draft
                      ? "正在编辑已保存草稿"
                      : "配置已同步"}
                </span>
                {[
                  "strategy",
                  "models",
                  "brand",
                  "runtime",
                  "notifications",
                  "channel_rule",
                  "attribution",
                ].includes(config.kind) &&
                  !config.inherited && (
                    <button
                      disabled={busy}
                      type="button"
                      onClick={() => void save("publish", undefined, true)}
                    >
                      恢复继承
                    </button>
                  )}
                {drafts && (
                  <button
                    disabled={busy || !dirty}
                    type="button"
                    onClick={() => void save("draft")}
                  >
                    保存草稿
                  </button>
                )}
                <button
                  className="primary"
                  disabled={busy || (!dirty && !config.draft)}
                  type="submit"
                >
                  {busy ? "保存中…" : drafts ? "发布配置" : "保存额度"}
                </button>
              </div>
            )}
            {message && (
              <p className="admin-feedback" role="status">
                {message}
              </p>
            )}
          </form>
          <details className="admin-config-history">
            <summary>
              版本与变更记录{" "}
              <span className="muted">
                {rows.length ? `最近 ${rows.length} 条` : ""}
              </span>
            </summary>
            <History
              rows={rows}
              onRollback={
                !readOnly && !busy
                  ? (version) => setPendingRollback(version)
                  : undefined
              }
            />
            {pendingRollback !== null && (
              <div className="admin-inline-review" role="status">
                <p>
                  将重新发布 v{pendingRollback}{" "}
                  的配置，替换当前生效内容和草稿。历史记录继续保留。
                </p>
                <button
                  type="button"
                  className="primary"
                  disabled={busy}
                  onClick={() => void save("rollback", pendingRollback)}
                >
                  确认回滚至 v{pendingRollback}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setPendingRollback(null)}
                >
                  取消回滚
                </button>
              </div>
            )}
          </details>
        </>
      )}
    </section>
  );
}
