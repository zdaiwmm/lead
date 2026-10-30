"use client";

import { useEffect, useState } from "react";
import { api } from "../api-client";
import ConfigEditor, { Row } from "./config-editor";

const categories: Row = {
  budget_warning: "额度接近上限",
  budget_exhausted: "额度已用尽",
  login_expired: "登录失效",
  repeated_failures: "连续失败",
  uncertain_action: "外部操作待核对",
  knowledge_expired: "知识过期",
  measurement_stale: "数据回传过期",
};
const roleNames: Row = {
  owner: "所有者",
  operator: "运营成员",
  viewer: "查看成员",
};
const displayTime = (value: string) => new Date(value).toLocaleString("zh-CN");
const localTime = (value: string) =>
  value
    ? new Date(
        new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000,
      )
        .toISOString()
        .slice(0, 16)
    : "";

function AccessFields({
  body,
  change,
  metadata,
  agents,
}: {
  body: Row;
  change: (value: Row) => void;
  metadata: Row;
  agents: Row[];
}) {
  const [identity, setIdentity] = useState("");
  const members = (metadata.members || []).filter(
    (m: Row) => m.status === "active" && m.role !== "owner",
  );
  const member = members.find(
    (m: Row) => JSON.stringify([m.issuer, m.subject]) === identity,
  );
  const policy =
    member &&
    body.members.find(
      (m: Row) => m.issuer === member.issuer && m.subject === member.subject,
    );
  function update(value: Row) {
    change({
      ...body,
      members: body.members.map((m: Row) =>
        m.issuer === value.issuer && m.subject === value.subject ? value : m,
      ),
    });
  }
  return (
    <>
      <label>
        需要细分权限的成员
        <select value={identity} onChange={(e) => setIdentity(e.target.value)}>
          <option value="">选择运营或查看成员</option>
          {members.map((m: Row) => (
            <option
              key={JSON.stringify([m.issuer, m.subject])}
              value={JSON.stringify([m.issuer, m.subject])}
            >
              {m.subject} · {roleNames[m.role]}
            </option>
          ))}
        </select>
      </label>
      {!members.length && (
        <p className="admin-empty">
          目前没有可配置的运营或查看成员。所有者始终保留完整权限。
        </p>
      )}
      {member && !policy && (
        <div className="admin-toolbar">
          <p className="admin-help">
            当前使用角色默认权限，运营成员可操作工作台，后台配置只读；查看成员只读。
          </p>
          <button
            type="button"
            onClick={() =>
              change({
                ...body,
                members: [
                  ...body.members,
                  {
                    issuer: member.issuer,
                    subject: member.subject,
                    capabilities:
                      member.role === "operator"
                        ? metadata.operator_defaults
                        : [],
                    agent_ids: null,
                  },
                ],
              })
            }
          >
            为此成员细分权限
          </button>
        </div>
      )}
      {policy && (
        <>
          <fieldset className="admin-choice-group">
            <legend>允许操作的功能</legend>
            {member.role === "viewer" ? (
              <p className="admin-help">
                查看成员始终只读。需要写入权限时，先将角色调整为运营成员。
              </p>
            ) : (
              Object.entries(metadata.capabilities).map(([id, label]) => (
                <label className="admin-checkbox" key={id}>
                  <input
                    type="checkbox"
                    checked={policy.capabilities.includes(id)}
                    onChange={(e) =>
                      update({
                        ...policy,
                        capabilities: e.target.checked
                          ? [...policy.capabilities, id]
                          : policy.capabilities.filter((v: string) => v !== id),
                      })
                    }
                  />
                  {label as string}
                </label>
              ))
            )}
          </fieldset>
          <label className="admin-checkbox">
            <input
              type="checkbox"
              checked={policy.agent_ids === null}
              onChange={(e) =>
                update({ ...policy, agent_ids: e.target.checked ? null : [] })
              }
            />
            可访问本工作室全部 Agent
          </label>
          {policy.agent_ids !== null && (
            <fieldset className="admin-choice-group">
              <legend>可访问的 Agent（未选择表示无 Agent 访问权限）</legend>
              {agents.map((agent) => (
                <label className="admin-checkbox" key={agent.id}>
                  <input
                    type="checkbox"
                    checked={policy.agent_ids.includes(agent.id)}
                    onChange={(e) =>
                      update({
                        ...policy,
                        agent_ids: e.target.checked
                          ? [...policy.agent_ids, agent.id]
                          : policy.agent_ids.filter(
                              (id: string) => id !== agent.id,
                            ),
                      })
                    }
                  />
                  {agent.name}
                </label>
              ))}
            </fieldset>
          )}
          <p className="admin-help">
            指定 Agent 范围后，只能维护这些 Agent
            的独立配置，不能修改影响整个工作室的默认配置。发布后，后续请求按新权限核对。
          </p>
          <button
            type="button"
            onClick={() =>
              change({
                ...body,
                members: body.members.filter(
                  (m: Row) =>
                    m.issuer !== member.issuer || m.subject !== member.subject,
                ),
              })
            }
          >
            恢复此成员的角色默认权限
          </button>
        </>
      )}
    </>
  );
}

export function MemberAccess({
  readOnly,
  agents,
}: {
  readOnly: boolean;
  agents: Row[];
}) {
  const [metadata, setMetadata] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0);
  const [subject, setSubject] = useState(""),
    [role, setRole] = useState("operator"),
    [status, setStatus] = useState("active"),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  useEffect(() => {
    if (readOnly) return;
    let active = true;
    api("/admin/member-access")
      .then((value) => {
        if (active) {
          setMetadata(value);
          setError("");
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [readOnly, revision]);
  if (readOnly)
    return (
      <p className="admin-readonly">
        成员身份、角色和细分权限由工作室所有者维护。
      </p>
    );
  return (
    <>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {!metadata && !error && <p role="status">正在读取工作室成员…</p>}
      {metadata && (
        <>
          <section className="admin-editor">
            <h2>成员与基础角色</h2>
            {metadata.mode === "local_owner" ? (
              <p className="admin-help">
                当前为本机所有者模式。启用工作室登录后，可以添加成员、调整角色，再分配功能和
                Agent 范围。
              </p>
            ) : (
              <>
                <ul className="admin-readiness-list">
                  {metadata.members.map((m: Row) => (
                    <li key={JSON.stringify([m.issuer, m.subject])}>
                      <div>
                        <strong>{m.subject}</strong>
                        <p>
                          {roleNames[m.role]} ·{" "}
                          {m.status === "active" ? "有效" : "已撤销"}
                        </p>
                      </div>
                      <button
                        onClick={() => {
                          setSubject(m.subject);
                          setRole(m.role);
                          setStatus(m.status);
                        }}
                      >
                        调整角色
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="admin-field-pair">
                  <label>
                    登录身份标识
                    <input
                      value={subject}
                      maxLength={500}
                      onChange={(e) => setSubject(e.target.value)}
                      placeholder="登录服务提供的成员身份标识"
                    />
                  </label>
                  <label>
                    基础角色
                    <select
                      value={role}
                      onChange={(e) => setRole(e.target.value)}
                    >
                      {Object.entries(roleNames).map(([id, name]) => (
                        <option key={id} value={id}>
                          {name as string}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <label>
                  成员状态
                  <select
                    value={status}
                    onChange={(e) => setStatus(e.target.value)}
                  >
                    <option value="active">有效</option>
                    <option value="revoked">撤销访问</option>
                  </select>
                </label>
                <button
                  disabled={busy || !subject.trim()}
                  onClick={async () => {
                    setBusy(true);
                    setError("");
                    try {
                      await api("/workspace/members", {
                        subject,
                        role,
                        status,
                      });
                      setMessage(
                        "成员角色已更新，工作室始终保留至少一位有效所有者。",
                      );
                      setRevision((v) => v + 1);
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  保存成员角色
                </button>
                {message && <p role="status">{message}</p>}
              </>
            )}
          </section>
          <ConfigEditor
            configKey="member-access/default"
            title="成员细分权限"
            description="草稿不影响现有成员。发布、回滚及恢复默认范围均保留变更记录。"
            readOnly={false}
          >
            {(body, change) => (
              <AccessFields
                body={body}
                change={change}
                metadata={metadata}
                agents={agents}
              />
            )}
          </ConfigEditor>
        </>
      )}
    </>
  );
}

function PricingFields({
  body,
  change,
  reference,
}: {
  body: Row;
  change: (value: Row) => void;
  reference: Row | null;
}) {
  function update(index: number, key: string, value: any) {
    change({
      ...body,
      models: body.models.map((row: Row, i: number) =>
        i === index ? { ...row, [key]: value } : row,
      ),
    });
  }
  return (
    <>
      <div className="admin-field-pair">
        <label>
          计价币种
          <select
            value={body.currency}
            onChange={(e) => change({ ...body, currency: e.target.value })}
          >
            {["USD", "CNY", "EUR"].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          此版本开始生效的时间
          <input
            required
            type="datetime-local"
            value={localTime(body.effective_at)}
            onChange={(e) =>
              change({
                ...body,
                effective_at: e.target.value
                  ? new Date(e.target.value).toISOString()
                  : "",
              })
            }
          />
        </label>
      </div>
      <p className="admin-help">
        单价单位为每百万
        token。调用开始时保留已生效的单价与版本；历史费用不重算。多币种分别显示，未计价与明细不足保留未知。
      </p>
      {reference && (
        <div className="admin-toolbar">
          <button
            type="button"
            onClick={() =>
              change({
                ...body,
                models: [
                  ...body.models,
                  ...reference.models.filter(
                    (row: Row) =>
                      !body.models.some(
                        (current: Row) => current.model === row.model,
                      ),
                  ),
                ],
              })
            }
          >
            补齐 OpenAI 参考价
          </button>
          <span className="admin-help">
            核对于 {displayTime(reference.verified_at)}
            ，只补充未登记模型，保留现有单价。
          </span>
        </div>
      )}
      <div className="admin-plans">
        {body.models.map((rate: Row, index: number) => (
          <details key={index} open={body.models.length === 1}>
            <summary>
              {rate.model || "新增模型单价"}
              <span className="muted"> · {body.currency} / 百万 token</span>
            </summary>
            <div className="admin-advanced-fields">
              <label>
                模型引用
                <input
                  required
                  maxLength={120}
                  value={rate.model}
                  onChange={(e) => update(index, "model", e.target.value)}
                  placeholder="openai:gpt-6-luna 或 deepseek-flash"
                />
              </label>
              <div className="admin-rate-grid">
                {[
                  ["input_per_million", "未缓存输入单价"],
                  ["cached_input_per_million", "缓存命中输入单价（可留空）"],
                  ["output_per_million", "输出单价"],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input
                      type="number"
                      min={0}
                      max={100000}
                      step="any"
                      required={key !== "cached_input_per_million"}
                      value={rate[key] ?? ""}
                      onChange={(e) =>
                        update(
                          index,
                          key,
                          e.target.value === "" ? null : e.target.value,
                        )
                      }
                    />
                  </label>
                ))}
              </div>
              <label>
                单价来源
                <input
                  required
                  type="url"
                  value={rate.source_url}
                  maxLength={2000}
                  onChange={(e) => update(index, "source_url", e.target.value)}
                  placeholder="https://…"
                />
              </label>
              <label>
                最近核对时间
                <input
                  required
                  type="datetime-local"
                  value={localTime(rate.verified_at)}
                  onChange={(e) =>
                    update(
                      index,
                      "verified_at",
                      e.target.value
                        ? new Date(e.target.value).toISOString()
                        : "",
                    )
                  }
                />
              </label>
              <label>
                单价说明
                <textarea
                  rows={2}
                  value={rate.notes}
                  maxLength={2000}
                  onChange={(e) => update(index, "notes", e.target.value)}
                />
              </label>
              <details>
                <summary>长上下文计价</summary>
                <label className="admin-checkbox">
                  <input
                    type="checkbox"
                    checked={rate.max_request_input_tokens !== null}
                    onChange={(e) =>
                      update(
                        index,
                        "max_request_input_tokens",
                        e.target.checked ? 272000 : null,
                      )
                    }
                  />
                  超过输入阈值时整次请求使用倍率
                </label>
                {rate.max_request_input_tokens !== null && (
                  <div className="admin-rate-grid">
                    {[
                      ["max_request_input_tokens", "单次输入 token 阈值"],
                      ["long_input_multiplier", "输入与缓存倍率"],
                      ["long_output_multiplier", "输出倍率"],
                    ].map(([key, label]) => (
                      <label key={key}>
                        {label}
                        <input
                          type="number"
                          min={1}
                          step={key === "max_request_input_tokens" ? 1 : "any"}
                          value={rate[key]}
                          onChange={(e) =>
                            update(
                              index,
                              key,
                              key === "max_request_input_tokens"
                                ? Number(e.target.value)
                                : e.target.value,
                            )
                          }
                        />
                      </label>
                    ))}
                  </div>
                )}
                <p className="admin-help">
                  多个请求的总输入超过阈值但缺少逐次明细时，保留费用未知。缓存写入或非标准服务档位未覆盖时，也不会套用普通单价。
                </p>
              </details>
              <button
                type="button"
                onClick={() =>
                  change({
                    ...body,
                    models: body.models.filter(
                      (_: Row, i: number) => i !== index,
                    ),
                  })
                }
              >
                移除此模型单价
              </button>
            </div>
          </details>
        ))}
      </div>
      <button
        type="button"
        disabled={body.models.length >= 100}
        onClick={() =>
          change({
            ...body,
            models: [
              ...body.models,
              {
                model: "",
                input_per_million: "0",
                cached_input_per_million: null,
                output_per_million: "0",
                source_url: "",
                verified_at: new Date().toISOString(),
                notes: "",
                max_request_input_tokens: null,
                long_input_multiplier: "1",
                long_output_multiplier: "1",
              },
            ],
          })
        }
      >
        新增模型单价
      </button>
    </>
  );
}

export function Pricing({ readOnly }: { readOnly: boolean }) {
  const [reference, setReference] = useState<Row | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api("/admin/pricing-reference")
      .then((value) => {
        if (active) setReference(value);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, []);
  return (
    <>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      <ConfigEditor
        configKey="pricing/default"
        title="模型成本单价"
        description="保存有来源的参考或合同单价，费用为已计价部分的估算，最终以模型服务账单为准。"
        readOnly={readOnly}
      >
        {(body, change) => (
          <PricingFields body={body} change={change} reference={reference} />
        )}
      </ConfigEditor>
    </>
  );
}

function SecretFields({
  destination,
  status,
  refresh,
}: {
  destination: Row;
  status: Row | undefined;
  refresh: () => void;
}) {
  const blank = () => ({
    url: "",
    signing_secret: "",
    smtp_host: "",
    smtp_port: 465,
    smtp_username: "",
    smtp_password: "",
    from_address: "",
    recipients: "",
  });
  const [fields, setFields] = useState<Row>(blank),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState("");
  const field = (key: string, value: any) =>
    setFields((previous) => ({ ...previous, [key]: value }));
  return (
    <details className="admin-secret-fields">
      <summary>
        接收地址与凭据 · {status ? `已配置 v${status.version}` : "尚未配置"}
      </summary>
      <p className="admin-help">
        凭据加密保存，只显示配置状态。此处输入不会进入配置草稿或版本记录，保存不发送测试通知。
      </p>
      {destination.kind === "webhook" ? (
        <>
          <label>
            Webhook 地址
            <input
              type="password"
              autoComplete="new-password"
              value={fields.url}
              maxLength={3000}
              onChange={(e) => field("url", e.target.value)}
              placeholder="公开 HTTPS 地址"
            />
          </label>
          <label>
            签名密钥（可选）
            <input
              type="password"
              autoComplete="new-password"
              maxLength={500}
              value={fields.signing_secret}
              onChange={(e) => field("signing_secret", e.target.value)}
            />
          </label>
        </>
      ) : (
        <>
          <div className="admin-field-pair">
            <label>
              SMTP 主机
              <input
                value={fields.smtp_host}
                onChange={(e) => field("smtp_host", e.target.value)}
                placeholder="smtp.example.com"
              />
            </label>
            <label>
              加密连接方式
              <select
                value={fields.smtp_port}
                onChange={(e) => field("smtp_port", Number(e.target.value))}
              >
                <option value={465}>TLS · 465</option>
                <option value={587}>STARTTLS · 587</option>
              </select>
            </label>
          </div>
          <div className="admin-field-pair">
            <label>
              SMTP 登录名
              <input
                value={fields.smtp_username}
                autoComplete="off"
                onChange={(e) => field("smtp_username", e.target.value)}
              />
            </label>
            <label>
              SMTP 密码
              <input
                type="password"
                autoComplete="new-password"
                value={fields.smtp_password}
                onChange={(e) => field("smtp_password", e.target.value)}
              />
            </label>
          </div>
          <label>
            发件邮箱
            <input
              value={fields.from_address}
              onChange={(e) => field("from_address", e.target.value)}
            />
          </label>
          <label>
            接收邮箱（每行一个，最多 20 个）
            <textarea
              rows={3}
              value={fields.recipients}
              onChange={(e) => field("recipients", e.target.value)}
            />
          </label>
        </>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          setMessage("");
          try {
            const { recipients, ...data } = fields;
            await api(
              "/admin/external-notifications/secrets/" + destination.id,
              {
                ...data,
                kind: destination.kind,
                expected_version: status?.version || 0,
                to_addresses: recipients
                  .split(/[\n,]+/)
                  .map((value: string) => value.trim())
                  .filter(Boolean),
              },
            );
            setFields(blank());
            setMessage(
              "凭据已加密保存。下一次发送使用最新凭据，请在配置中核对并启用渠道。",
            );
            refresh();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {status ? "替换并保存完整凭据" : "加密保存凭据"}
      </button>
      {error && (
        <p role="alert" className="error-feedback">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
    </details>
  );
}

function ExternalFields({
  body,
  change,
  readOnly,
  secrets,
  refresh,
  agents,
}: {
  body: Row;
  change: (value: Row) => void;
  readOnly: boolean;
  secrets: Row[];
  refresh: () => void;
  agents: Row[];
}) {
  function update(index: number, value: Row) {
    change({
      ...body,
      destinations: body.destinations.map((d: Row, i: number) =>
        i === index ? value : d,
      ),
    });
  }
  return (
    <>
      <label className="admin-checkbox">
        <input
          type="checkbox"
          checked={body.enabled}
          onChange={(e) => change({ ...body, enabled: e.target.checked })}
        />
        启用外部通知
      </label>
      <p className="admin-help">
        只为启用后的新异常生成外部通知，不补发历史提醒。沿用站内通知的异常去重和免打扰时间；接收范围按外部渠道独立配置。
      </p>
      <div className="admin-plans">
        {body.destinations.map((destination: Row, index: number) => (
          <details key={destination.id} open>
            <summary>
              {destination.name || "新的接收渠道"} ·{" "}
              {destination.kind === "email" ? "邮件" : "Webhook"} ·{" "}
              {destination.enabled ? "已选启用" : "未启用"}
            </summary>
            <div className="admin-advanced-fields">
              <div className="admin-field-pair">
                <label>
                  渠道名称
                  <input
                    required
                    maxLength={100}
                    value={destination.name}
                    onChange={(e) =>
                      update(index, { ...destination, name: e.target.value })
                    }
                  />
                </label>
                <label>
                  接收方式
                  <select
                    value={destination.kind}
                    onChange={(e) =>
                      update(index, {
                        ...destination,
                        kind: e.target.value,
                        enabled: false,
                      })
                    }
                  >
                    <option value="webhook">通用 Webhook</option>
                    <option value="email">SMTP 邮件</option>
                  </select>
                </label>
              </div>
              {!readOnly && (
                <SecretFields
                  destination={destination}
                  status={secrets.find((s) => s.id === destination.id)}
                  refresh={refresh}
                />
              )}
              <label className="admin-checkbox">
                <input
                  type="checkbox"
                  checked={destination.enabled}
                  onChange={(e) =>
                    update(index, { ...destination, enabled: e.target.checked })
                  }
                />
                启用此接收渠道
              </label>
              <fieldset className="admin-choice-group">
                <legend>需要发送的异常</legend>
                {Object.entries(categories).map(([id, label]) => (
                  <label className="admin-checkbox" key={id}>
                    <input
                      type="checkbox"
                      checked={destination.categories.includes(id)}
                      onChange={(e) =>
                        update(index, {
                          ...destination,
                          categories: e.target.checked
                            ? [...destination.categories, id]
                            : destination.categories.filter(
                                (value: string) => value !== id,
                              ),
                        })
                      }
                    />
                    {label as string}
                  </label>
                ))}
              </fieldset>
              <fieldset className="admin-choice-group">
                <legend>提醒级别</legend>
                {[
                  ["warning", "需要关注"],
                  ["error", "需要处理"],
                ].map(([id, label]) => (
                  <label className="admin-checkbox" key={id}>
                    <input
                      type="checkbox"
                      checked={destination.severities.includes(id)}
                      onChange={(e) =>
                        update(index, {
                          ...destination,
                          severities: e.target.checked
                            ? [...destination.severities, id]
                            : destination.severities.filter(
                                (value: string) => value !== id,
                              ),
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
                  checked={destination.agent_ids === null}
                  onChange={(e) =>
                    update(index, {
                      ...destination,
                      agent_ids: e.target.checked ? null : [],
                    })
                  }
                />
                接收全部 Agent 的提醒
              </label>
              {destination.agent_ids !== null && (
                <fieldset className="admin-choice-group">
                  <legend>需要接收的 Agent</legend>
                  {agents.map((agent) => (
                    <label className="admin-checkbox" key={agent.id}>
                      <input
                        type="checkbox"
                        checked={destination.agent_ids.includes(agent.id)}
                        onChange={(e) =>
                          update(index, {
                            ...destination,
                            agent_ids: e.target.checked
                              ? [...destination.agent_ids, agent.id]
                              : destination.agent_ids.filter(
                                  (id: string) => id !== agent.id,
                                ),
                          })
                        }
                      />
                      {agent.name}
                    </label>
                  ))}
                </fieldset>
              )}
              <div className="admin-field-pair">
                <label>
                  每轮最多发送尝试次数
                  <input
                    type="number"
                    required
                    min={1}
                    max={5}
                    step={1}
                    value={destination.max_attempts}
                    onChange={(e) =>
                      update(index, {
                        ...destination,
                        max_attempts:
                          e.target.value === "" ? "" : Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  首次重试等待秒数
                  <input
                    type="number"
                    required
                    min={30}
                    max={3600}
                    step={1}
                    value={destination.retry_seconds}
                    onChange={(e) =>
                      update(index, {
                        ...destination,
                        retry_seconds:
                          e.target.value === "" ? "" : Number(e.target.value),
                      })
                    }
                  />
                </label>
              </div>
              <button
                type="button"
                onClick={() =>
                  change({
                    ...body,
                    destinations: body.destinations.filter(
                      (d: Row) => d.id !== destination.id,
                    ),
                  })
                }
              >
                移除此接收渠道
              </button>
            </div>
          </details>
        ))}
      </div>
      {!readOnly && (
        <button
          type="button"
          disabled={body.destinations.length >= 20}
          onClick={() =>
            change({
              ...body,
              destinations: [
                ...body.destinations,
                {
                  id: crypto.randomUUID(),
                  name: "新的接收渠道",
                  kind: "webhook",
                  enabled: false,
                  categories: Object.keys(categories),
                  severities: ["warning", "error"],
                  agent_ids: null,
                  max_attempts: 3,
                  retry_seconds: 60,
                },
              ],
            })
          }
        >
          新增外部接收渠道
        </button>
      )}
    </>
  );
}

export function ExternalNotifications({
  readOnly,
  agents,
}: {
  readOnly: boolean;
  agents: Row[];
}) {
  const [secrets, setSecrets] = useState<Row[]>([]),
    [deliveries, setDeliveries] = useState<Row[]>([]),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [loaded, setLoaded] = useState(false),
    [more, setMore] = useState(false),
    [busy, setBusy] = useState(false),
    [review, setReview] = useState<Row | null>(null),
    [acknowledged, setAcknowledged] = useState(false);
  async function load(before?: Row) {
    setBusy(true);
    try {
      const rows = await api(
        "/admin/external-notifications/deliveries" +
          (before
            ? "?before=" +
              encodeURIComponent(before.created_at) +
              "&before_id=" +
              before.id
            : ""),
      );
      setDeliveries((previous) => (before ? [...previous, ...rows] : rows));
      setMore(rows.length === 40);
      setLoaded(true);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void load();
    if (!readOnly)
      api("/admin/external-notifications/secrets")
        .then(setSecrets)
        .catch((e) => setError(e.message));
  }, [readOnly, revision]);
  const states: Row = {
    pending: "待发送",
    sending: "发送中",
    retry: "等待重试",
    delivered: "已送达",
    failed: "发送失败",
    unknown: "结果待核对",
    skipped: "已取消发送",
  };
  const errors: Row = {
    delivery_result_unknown: "未取得确定回执，自动重试已停止",
    delivery_interrupted: "发送过程被中断，请先核对接收端",
    http_rejected: "接收服务未接受请求",
    partial_recipient_failure: "部分接收邮箱未接受邮件",
    destination_validation_failed: "接收地址或认证未通过",
    destination_not_configured: "缺少匹配的渠道凭据",
    disabled_or_resolved: "渠道已关闭或异常已恢复",
  };
  return (
    <>
      <ConfigEditor
        configKey="external-notifications/default"
        title="外部通知渠道"
        description="所有者维护接收范围与渠道凭据。发布后用于后续通知，草稿不发送。"
        readOnly={readOnly}
      >
        {(body, change) => (
          <ExternalFields
            body={body}
            change={change}
            readOnly={readOnly}
            secrets={secrets}
            refresh={() => setRevision((v) => v + 1)}
            agents={agents}
          />
        )}
      </ConfigEditor>
      <section className="admin-editor">
        <header className="admin-section-heading">
          <div>
            <h2>外部发送记录</h2>
            <p>
              发送结果未知时先核对接收端，避免重复提醒。重试沿用同一通知编号，接收服务可按编号去重。
            </p>
          </div>
          <button disabled={busy} onClick={() => void load()}>
            刷新发送记录
          </button>
        </header>
        {error && (
          <p role="alert" className="error-feedback">
            {error}
          </p>
        )}
        {!loaded && !error && <p role="status">正在读取发送记录…</p>}
        <ul className="admin-readiness-list">
          {deliveries.map((row) => (
            <li key={row.id}>
              <div>
                <strong>{row.title}</strong>
                <p>
                  {row.destination_name} · {states[row.state]} · 已尝试{" "}
                  {row.attempts} 次
                </p>
                <small>
                  {displayTime(row.created_at)} · {row.configuration_version}
                  {row.last_error_code
                    ? " · " + (errors[row.last_error_code] || "发送异常")
                    : ""}
                </small>
              </div>
              {!readOnly && ["failed", "unknown"].includes(row.state) && (
                <button
                  onClick={() => {
                    setReview(row);
                    setAcknowledged(false);
                  }}
                >
                  核对后重试
                </button>
              )}
            </li>
          ))}
        </ul>
        {loaded && !deliveries.length && (
          <p className="admin-empty">
            尚无外部发送记录。发布并启用接收渠道后，新出现的实际异常才会进入发送队列。
          </p>
        )}
        {more && (
          <button
            disabled={busy}
            onClick={() => void load(deliveries[deliveries.length - 1])}
          >
            加载更早发送记录
          </button>
        )}
        {review && (
          <div className="admin-inline-review">
            <strong>核对后重新发送：{review.title}</strong>
            <p>
              接收渠道：{review.destination_name}
              。若通知已送达，再次发送可能产生重复提醒。
            </p>
            {review.state === "unknown" && (
              <label className="admin-checkbox">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                />
                我已核对接收端，确认需要再次发送并接受重复提醒的可能
              </label>
            )}
            <button
              disabled={busy || (review.state === "unknown" && !acknowledged)}
              onClick={async () => {
                setBusy(true);
                try {
                  await api(
                    "/admin/external-notifications/deliveries/" +
                      review.id +
                      "/retry",
                    {
                      expected_attempts: review.attempts,
                      acknowledge_duplicate_risk: acknowledged,
                    },
                  );
                  setReview(null);
                  await load();
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              确认重新发送
            </button>
            <button disabled={busy} onClick={() => setReview(null)}>
              取消重试
            </button>
          </div>
        )}
      </section>
    </>
  );
}
