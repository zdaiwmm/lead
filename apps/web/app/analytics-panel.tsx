"use client";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Check, Copy, Plus, RefreshCw } from "lucide-react";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";

type Row = Record<string, any>;
export type PixelTask =
  "embed" | "detect" | "events" | "details" | "reference" | null;
export default function AnalyticsPanel({
  aid,
  url,
  readOnly: readOnlyProp,
  task,
  onTask,
  onData,
}: {
  aid: string;
  url?: string;
  readOnly: boolean;
  task: PixelTask;
  onTask: (task: PixelTask) => void;
  onData: (data: Row) => void;
}) {
  const [data, setData] = useState<Row | null>(null);
  const readOnly = readOnlyProp || !!data?.configuration_denied;
  const [site, setSite] = useState(url || "");
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState("");
  const [view, setView] = useState<"list" | "add" | "edit" | "test">("list");
  const [search, setSearch] = useState(""),
    [referenceSearch, setReferenceSearch] = useState("");
  const [draft, setDraft] = useState<Row | null>(null),
    [newType, setNewType] = useState("");
  const [newLabel, setNewLabel] = useState(""),
    [newName, setNewName] = useState(""),
    [newAction, setNewAction] = useState("");
  const [test, setTest] = useState<Row | null>(null),
    [serverKey, setServerKey] = useState("");
  const returnTask = useRef<PixelTask>("events"),
    currentTask = useRef(task),
    currentAid = useRef(aid),
    busyRef = useRef(false);
  const drafts = useRef<Record<string, Row>>({});
  const siteEdited = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  currentTask.current = task;
  currentAid.current = aid;
  const id = useId(),
    root = "/agents/" + aid + "/analytics";
  const load = useCallback(async () => {
    let value: Row;
    if (readOnlyProp) value = await api("/agents/" + aid + "/analytics/report");
    else {
      try {
        value = await api("/agents/" + aid + "/analytics");
      } catch {
        value = {
          ...(await api("/agents/" + aid + "/analytics/report")),
          configuration_denied: true,
        };
      }
    }
    if (currentAid.current !== aid || !mounted.current) return null;
    setData(value);
    if (!siteEdited.current) setSite(value.connection?.site_host || url || "");
    onData(value);
    return value;
  }, [aid, readOnlyProp, onData, url]);
  useEffect(() => {
    setData(null);
    siteEdited.current = false;
    setSite(url || "");
    setDraft(null);
    setView("list");
    setTest(null);
    setServerKey("");
    drafts.current = {};
    let active = true;
    const update = () =>
      load()
        .then(() => {
          if (active) setError("");
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    update();
    const timer = setInterval(() => {
      if (!document.hidden) update();
    }, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [aid, url, load]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(""), 2500);
    return () => clearTimeout(timer);
  }, [copied]);
  useEffect(() => {
    if (task !== "detect") return;
    let active = true;
    const check = () =>
      load()
        .then((value) => {
          if (active && value?.pixel.installed) {
            setView("list");
            setError("");
            onTask("events");
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    check();
    const timer = setInterval(check, 2500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [task, load, onTask]);
  useEffect(() => {
    if (task !== "events" || !test?.active) return;
    let active = true;
    const check = async () => {
      try {
        const result = await api(root + "/events/test/" + test.id);
        if (!active) return;
        setTest((previous) =>
          previous?.id === result.id ? { ...previous, ...result } : previous,
        );
        if (!result.current)
          setError("事件设置已变更，请结束本次测试并开始新会话");
        if (result.received_at) await load();
      } catch (e) {
        if (active) setError((e as Error).message);
      }
    };
    check();
    const timer = setInterval(check, 2000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [task, test?.id, test?.active, root, load]);
  async function run(fn: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function copy(value: string, name: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(name);
    } catch {
      setError("复制未完成，请手动复制代码");
    }
  }
  function dismiss() {
    setError("");
    setTest(null);
    setServerKey("");
    setView("list");
    setDraft(null);
    drafts.current = {};
    if (mounted.current) onTask(null);
  }
  async function stopTest() {
    if (!test?.active) return;
    const result = await api(root + "/events/test/" + test.id + "/end", {});
    setTest((previous) => (previous ? { ...previous, ...result } : previous));
    await load();
  }
  function close() {
    if (test?.active) {
      void run(async () => {
        await stopTest();
        dismiss();
      });
    } else dismiss();
  }
  function showReference() {
    returnTask.current = task;
    setReferenceSearch("");
    setError("");
    onTask("reference");
  }
  function edit(row: Row) {
    setDraft({ ...(drafts.current[row.id] || row) });
    setView("edit");
    setError("");
  }
  function list() {
    if (draft) drafts.current[draft.id] = { ...draft };
    setView("list");
    setError("");
  }
  const conn = data?.connection,
    pixel = data?.pixel,
    catalog: Row[] = pixel?.catalog || [],
    events: Row[] = pixel?.events || [];
  let siteOrigin = "";
  try {
    siteOrigin = new URL(site.trim()).origin;
  } catch {
    // The URL field supplies native validation before saving.
  }
  const siteChanged = !!conn && siteOrigin !== conn.site_host;
  const available = catalog.filter(
    (e) => !events.some((d) => d.name.toLowerCase() === e.name.toLowerCase()),
  );
  const selectedNew = newType || available[0]?.name || "custom";
  const original = events.find((e) => e.id === draft?.id);
  const revision =
    (draft?.revision || 1) +
    (original &&
    ["label", "method", "definition"].some((k) => original[k] !== draft?.[k])
      ? 1
      : 0);
  const custom = draft && !catalog.some((e) => e.name === draft.name);
  function eventCode() {
    if (!draft || !conn) return "";
    const props =
      draft.name === "Activation"
        ? { action: draft.definition, rule_version: "activation-v" + revision }
        : draft.name === "Purchase"
          ? { order_id: "order_1001", value: 99, currency: "CNY" }
          : custom
            ? { action: draft.definition }
            : {};
    if (draft.method === "server") {
      const properties =
        draft.name === "Purchase"
          ? "{ order_id: String(order.id), value: order.paid_amount, currency: order.currency }"
          : JSON.stringify(props, null, 2);
      return (
        "// 网站服务端：业务成功后执行，密钥不放入浏览器\n" +
        "await fetch(" +
        JSON.stringify(data?.public_url + "/collect/server/events") +
        ", {\n" +
        "  method: 'POST',\n  headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + SERVER_KEY },\n" +
        "  body: JSON.stringify({\n    connection_id: " +
        JSON.stringify(conn.id) +
        ",\n" +
        "    event_name: " +
        JSON.stringify(draft.name) +
        ",\n" +
        "    event_id: businessEvent.id,\n    occurred_at: businessEvent.occurred_at,\n" +
        "    lead_attribution: businessEvent.lead_attribution,\n" +
        "    properties: " +
        properties +
        "\n  })\n});"
      );
    }
    const identify = ["CompleteRegistration", "Activation"].includes(draft.name)
      ? "await leadq('identify', String(user.id));\n"
      : "";
    return (
      identify +
      "await leadq('track', " +
      JSON.stringify(draft.name) +
      ", " +
      JSON.stringify(props, null, 2) +
      ",\n  { event_id: businessEvent.id" +
      " });"
    );
  }
  async function saveEvent() {
    if (!draft || !conn) return;
    const row = await api(root + "/events/configure", {
      connection_id: conn.id,
      name: draft.name,
      label: draft.label,
      method: draft.method,
      definition: draft.definition,
      expected_revision: draft.revision,
    });
    setDraft(row);
    drafts.current[row.id] = row;
    await load();
  }
  async function startTest() {
    if (!conn) return;
    if (test?.active) {
      setView("test");
      return;
    }
    const result = await api(root + "/events/test", { connection_id: conn.id });
    if (mounted.current && currentTask.current === "events") {
      setTest(result);
      setView("test");
    }
  }
  async function viewTest() {
    if (!test) return;
    const result = await api(root + "/events/test/" + test.id);
    setTest((previous) => (previous ? { ...previous, ...result } : previous));
    setView("test");
  }
  async function addEvent() {
    const standard = catalog.find((e) => e.name === selectedNew);
    if (!standard) {
      if (!newLabel.trim()) throw Error("请填写显示名称");
      if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(newName.trim()))
        throw Error("事件标识须以字母开头，仅包含字母、数字或下划线");
      if (!newAction.trim()) throw Error("请填写触发行为");
    }
    const row = await api(root + "/events/configure", {
      connection_id: conn.id,
      name: standard?.name || newName.trim(),
      label: standard?.label || newLabel.trim(),
      definition: standard ? "" : newAction.trim(),
      method: "browser",
      expected_revision: 0,
    });
    await load();
    edit(row);
  }
  async function finish(visitsOnly = false) {
    await api(root + "/complete", {
      connection_id: conn.id,
      visits_only: visitsOnly,
    });
    await stopTest();
    await load();
    dismiss();
  }
  if (!task) return null;
  let title = "事件设置与测试",
    content: React.ReactNode,
    footer: React.ReactNode;
  const reference = (
    <button className="text-button" onClick={showReference} disabled={busy}>
      标准事件参考
    </button>
  );
  if (task === "embed") {
    title = "嵌入像素代码";
    content = (
      <div className="pixel-task">
        <form
          id={id + "-site-form"}
          onSubmit={(e) => {
            e.preventDefault();
            if (conn && !siteChanged) return;
            run(async () => {
              const value = await api(root, { site_url: site.trim() });
              if (currentAid.current !== aid || !mounted.current) return;
              siteEdited.current = false;
              setSite(value.connection.site_host);
              setData(value);
              onData(value);
            });
          }}
        >
          <label htmlFor={id + "-site"}>网站地址</label>
          <div className="pixel-url-row">
            <input
              id={id + "-site"}
              type="url"
              required
              value={site}
              onChange={(e) => {
                siteEdited.current = true;
                setSite(e.target.value);
                setError("");
              }}
              disabled={busy || readOnly}
            />
            {(!conn || siteChanged) && (
              <button type="submit" disabled={busy || readOnly || !site.trim()}>
                {busy ? "保存中…" : conn ? "保存网址" : "获取代码"}
              </button>
            )}
          </div>
        </form>
        {conn?.snippet && (
          <>
            <p className="pixel-task-hint">
              {siteChanged
                ? "以下是已保存网站的代码，更换网站请先保存网址。"
                : "同一网站只需安装一次，后续可直接复用此代码。"}
              <br />
              粘贴到网站的 <code>&lt;/head&gt;</code> 前。
            </p>
            <textarea
              className="pixel-code"
              aria-label="像素安装代码"
              readOnly
              value={conn.snippet}
              rows={6}
              spellCheck={false}
            />
          </>
        )}
        {data?.public_url?.startsWith("http://127.0.0.1") && (
          <details className="pixel-advanced">
            <summary>接入环境</summary>
            <p>当前采集地址仅本机可用。接入公开网站前需配置公开 HTTPS 地址。</p>
          </details>
        )}
      </div>
    );
    footer = (
      <>
        <button onClick={close}>稍后嵌入</button>
        <div className="pixel-footer-actions">
          {conn?.snippet && (
            <button onClick={() => copy(conn.snippet, "base")}>
              <Copy size={14} />
              {copied === "base" ? "已复制" : "复制代码"}
            </button>
          )}
          <button
            className="primary"
            disabled={busy || readOnly || !conn || siteChanged}
            onClick={() =>
              run(async () => {
                await api(root + "/published", { connection_id: conn.id });
                await load();
                if (currentTask.current === "embed") onTask("detect");
              })
            }
          >
            已发布，去检测
          </button>
        </div>
      </>
    );
  } else if (task === "detect") {
    title = "检测像素安装";
    content = (
      <div className="pixel-task">
        <label>检测网址</label>
        <p className="pixel-site">
          {conn?.site_host || site || "请先嵌入像素代码"}
        </p>
        <div className="pixel-detect-result" role="status">
          <RefreshCw size={26} />
          <h3>等待网站数据</h3>
          {conn?.last_received_at && (
            <p className="muted small">
              最近收到：
              {new Date(conn.last_received_at).toLocaleString("zh-CN")}
            </p>
          )}
        </div>
      </div>
    );
    footer = (
      <>
        <button onClick={close}>稍后检测</button>
        <div className="pixel-footer-actions">
          <button onClick={() => onTask("embed")}>查看嵌入指引</button>
          <button
            className="primary"
            onClick={() =>
              run(async () => {
                const value = await load();
                if (value?.pixel.installed) {
                  setView("list");
                  onTask("events");
                } else setError("尚未收到网站数据，请发布后访问网站");
              })
            }
            disabled={busy || !conn}
          >
            重新检测
          </button>
        </div>
      </>
    );
  } else if (task === "reference") {
    title = "标准事件参考";
    const rows = [
      {
        name: "PageView",
        label: "浏览页面",
        when: "页面加载或路由切换",
        automatic: true,
      },
      ...catalog,
    ].filter((e) =>
      [e.name, e.label, e.when].some((v) =>
        v.toLowerCase().includes(referenceSearch.toLowerCase()),
      ),
    );
    content = (
      <div className="pixel-task">
        <label htmlFor={id + "-reference"}>查找事件</label>
        <input
          id={id + "-reference"}
          type="search"
          value={referenceSearch}
          onChange={(e) => setReferenceSearch(e.target.value)}
          placeholder="事件名称或业务行为"
        />
        <table className="pixel-event-table">
          <thead>
            <tr>
              <th>事件</th>
              <th>触发时机</th>
              <th>类型</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.name}>
                <td>
                  {e.label}
                  <code>{e.name}</code>
                </td>
                <td data-label="触发时机">{e.when}</td>
                <td data-label="类型">
                  {e.automatic
                    ? "自动采集"
                    : e.custom
                      ? "自定义事件"
                      : "标准事件"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <p className="pixel-task-hint">没有匹配的事件</p>}
      </div>
    );
    footer = (
      <button onClick={() => onTask(returnTask.current)}>
        返回{returnTask.current === "details" ? "事件明细" : "事件设置"}
      </button>
    );
  } else if (task === "details") {
    title = "事件明细";
    content = (
      <div className="pixel-task">
        <p className="muted small">近 30 天 · 正式数据</p>
        <table className="pixel-event-table">
          <thead>
            <tr>
              <th>事件</th>
              <th>事件次数</th>
              <th>触发人数</th>
              <th>最近发生</th>
              <th>接入状态</th>
            </tr>
          </thead>
          <tbody>
            {(pixel?.report.events || []).map((e: Row) => (
              <tr key={e.name}>
                <td>
                  {e.label}
                  <code>{e.name}</code>
                </td>
                <td data-label="事件次数">
                  {e.count?.toLocaleString() ?? "—"}
                </td>
                <td data-label="触发人数">
                  {e.users?.toLocaleString() ?? "—"}
                </td>
                <td data-label="最近发生">
                  {e.last_occurred_at
                    ? new Date(e.last_occurred_at).toLocaleString("zh-CN")
                    : "—"}
                </td>
                <td data-label="接入状态">
                  {e.count != null
                    ? "已收到数据"
                    : e.tested
                      ? "测试通过"
                      : "待测试"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {pixel?.report.event_count == null && (
          <p className="pixel-task-hint">暂无正式事件数据</p>
        )}
        <section className="pixel-stat-rules">
          <h3>统计口径</h3>
          <p>
            事件按业务规则去重计次，不含页面访问与测试记录。人数按各事件分别去重，不相加为总人数。
          </p>
        </section>
      </div>
    );
    footer = (
      <>
        {reference}
        <button onClick={close}>关闭</button>
      </>
    );
  } else if (view === "test") {
    title = "完整流程测试";
    const received: Row[] = test?.events || [];
    content = (
      <div className="pixel-task">
        <div className="pixel-test-result" role="status">
          <h3>
            {test?.active
              ? "测试进行中"
              : test?.state === "expired"
                ? "测试已过期"
                : "测试已结束"}
          </h3>
          <p>
            业务埋点代码保持不变。打开测试网站，按正常流程浏览、注册、加购或完成测试订单。
          </p>
          {test?.active && test.url && (
            <a href={test.url} target="_blank" rel="noopener noreferrer">
              打开测试网站 ↗
            </a>
          )}
          <p className="small muted">
            本会话内所有已配置事件均可测试，不计入正式数据。结束后，网站 SDK
            会自动清理测试状态；离线网站在重新联网后核对清理。
          </p>
          {test?.active && (
            <p className="small muted">
              有效期至 {new Date(test.expires_at).toLocaleTimeString("zh-CN")}
            </p>
          )}
        </div>
        <h3>实时事件</h3>
        <table className="pixel-event-table">
          <thead>
            <tr>
              <th>事件</th>
              <th>来源</th>
              <th>结果</th>
              <th>接收时间</th>
            </tr>
          </thead>
          <tbody>
            {received.map((e: Row) => (
              <tr key={e.id}>
                <td>
                  <code>{e.event_name || "未知事件"}</code>
                  {e.reason && (
                    <p className="small pixel-test-error">{e.reason}</p>
                  )}
                </td>
                <td data-label="来源">
                  {e.trust_level === "server_verified"
                    ? "服务端"
                    : e.trust_level === "rejected"
                      ? "校验"
                      : "浏览器"}
                </td>
                <td
                  data-label="结果"
                  className={
                    e.status === "rejected"
                      ? "pixel-test-error"
                      : "pixel-test-success"
                  }
                >
                  {e.status === "rejected"
                    ? "校验失败"
                    : e.duplicate
                      ? "已去重"
                      : "已接收"}
                </td>
                <td data-label="接收时间">
                  {new Date(e.received_at).toLocaleTimeString("zh-CN")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!received.length && (
          <p className="pixel-task-hint">
            等待网站上报。请先打开测试网站，再完成实际操作。
          </p>
        )}
        <p className="small muted">
          服务端支付复用订单保存的
          lead_attribution，测试上下文会自动解析。不会要求临时修改支付回调代码。
        </p>
      </div>
    );
    footer = (
      <>
        <button onClick={list} disabled={busy}>
          返回事件设置
        </button>
        <button
          className="primary"
          disabled={busy || readOnly}
          onClick={() => run(test?.active ? stopTest : startTest)}
        >
          {busy ? "正在处理…" : test?.active ? "结束测试" : "开始新测试"}
        </button>
      </>
    );
  } else if (view === "list") {
    const rows = events.filter((e) =>
      [e.name, e.label].some((v) =>
        v.toLowerCase().includes(search.toLowerCase()),
      ),
    );
    content = (
      <div className="pixel-task">
        <div className="pixel-manager-head">
          <h3>已添加事件</h3>
          <div>
            {reference}
            <button
              disabled={readOnly || busy}
              onClick={() => {
                setView("add");
                setNewType("");
                setNewLabel("");
                setNewName("");
                setNewAction("");
                setError("");
              }}
            >
              <Plus size={14} />
              添加事件
            </button>
          </div>
        </div>
        <p className="pixel-task-hint">
          业务埋点只接入一次。测试时直接操作网站，SDK 自动识别测试会话。
        </p>
        <button
          className="primary"
          disabled={readOnly || busy || !pixel?.installed}
          onClick={() => run(startTest)}
        >
          {test?.active ? "继续完整流程测试" : "开始完整流程测试"}
        </button>
        {test && (
          <button disabled={busy} onClick={() => run(viewTest)}>
            查看本次测试记录
          </button>
        )}
        <label htmlFor={id + "-search"}>查找已添加事件</label>
        <input
          id={id + "-search"}
          type="search"
          value={search}
          placeholder="事件名称或标识"
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="pixel-event-list">
          {rows.map((e) => (
            <div className="pixel-event-row" key={e.id}>
              <div>
                {e.label}
                <code>{e.name}</code>
                <span className={e.tested ? "pixel-test-success" : "muted"}>
                  {e.tested ? "✓ 测试通过" : "待测试"}
                </span>
              </div>
              <button onClick={() => edit(e)}>
                {readOnly ? "查看设置" : "设置与埋点"}
              </button>
            </div>
          ))}
        </div>
        {!rows.length && <p className="pixel-task-hint">没有匹配的事件</p>}
      </div>
    );
    footer = (
      <>
        <button
          onClick={() =>
            pixel?.completed || readOnly ? close() : run(() => finish(true))
          }
          disabled={busy}
        >
          {pixel?.completed || readOnly ? "关闭" : "稍后配置事件"}
        </button>
        <button
          className="primary"
          disabled={
            busy ||
            (!pixel?.completed && !events.some((e) => e.tested)) ||
            readOnly
          }
          onClick={() => (pixel?.completed ? close() : run(() => finish()))}
        >
          {pixel?.completed ? "完成" : "完成接入"}
        </button>
      </>
    );
  } else if (view === "add") {
    content = (
      <div className="pixel-task">
        <div className="pixel-manager-head">
          <h3>添加事件</h3>
          {reference}
        </div>
        <label htmlFor={id + "-type"}>事件类型</label>
        <select
          id={id + "-type"}
          value={selectedNew}
          onChange={(e) => {
            setNewType(e.target.value);
            setError("");
          }}
        >
          {available.map((e) => (
            <option key={e.name} value={e.name}>
              {e.label} · {e.name}
            </option>
          ))}
          <option value="custom">自定义事件</option>
        </select>
        {selectedNew === "custom" ? (
          <>
            <label htmlFor={id + "-label"}>显示名称</label>
            <input
              id={id + "-label"}
              value={newLabel}
              maxLength={64}
              onChange={(e) => setNewLabel(e.target.value)}
              placeholder="例如：导出报告"
            />
            <label htmlFor={id + "-name"}>事件标识</label>
            <input
              id={id + "-name"}
              value={newName}
              maxLength={64}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="例如：ReportExported"
            />
            <label htmlFor={id + "-action"}>触发行为</label>
            <input
              id={id + "-action"}
              value={newAction}
              maxLength={200}
              onChange={(e) => setNewAction(e.target.value)}
              placeholder="例如：报告成功导出"
            />
          </>
        ) : (
          <p className="pixel-task-hint">
            {catalog.find((e) => e.name === selectedNew)?.when}后上报。
          </p>
        )}
      </div>
    );
    footer = (
      <>
        <button onClick={list} disabled={busy}>
          返回事件列表
        </button>
        <button
          className="primary"
          disabled={busy || readOnly}
          onClick={() => run(addEvent)}
        >
          添加并设置
        </button>
      </>
    );
  } else {
    content = draft && (
      <div className="pixel-task">
        <h3>{draft.label}</h3>
        <code className="muted">{draft.name}</code>
        {draft.name === "Activation" || custom ? (
          <>
            <label htmlFor={id + "-definition"}>
              {custom ? "触发行为" : "核心行为"}
            </label>
            <input
              id={id + "-definition"}
              value={draft.definition}
              maxLength={200}
              disabled={readOnly || busy}
              onChange={(e) => {
                setDraft({ ...draft, definition: e.target.value });
              }}
            />
          </>
        ) : (
          <p className="pixel-task-hint">
            {catalog.find((e) => e.name === draft.name)?.when}后上报。
          </p>
        )}
        <div className="pixel-code-head">
          <span>固定业务埋点 · 只需接入一次</span>
          <div>
            {reference}
            <button
              className="text-button"
              onClick={() => copy(eventCode(), "event")}
            >
              {copied === "event" ? <Check size={14} /> : <Copy size={14} />}
              {copied === "event" ? "已复制" : "复制代码"}
            </button>
          </div>
        </div>
        <textarea
          className="pixel-code"
          aria-label="固定业务埋点代码"
          readOnly
          value={eventCode()}
          rows={7}
          spellCheck={false}
        />
        <p className="small muted">
          在业务成功回调中接入。开始、重试或结束测试都不需要修改此代码；测试上下文由
          SDK 自动携带。
        </p>
        <details className="pixel-advanced">
          <summary>开发接入</summary>
          <label htmlFor={id + "-method"}>上报方式</label>
          <select
            id={id + "-method"}
            value={draft.method}
            disabled={readOnly || busy}
            onChange={(e) => {
              setDraft({ ...draft, method: e.target.value });
            }}
          >
            <option value="browser">浏览器</option>
            <option value="server">服务端</option>
          </select>
          <p>
            注册按用户、有效使用按用户与规则版本、支付按订单去重；其他事件按事件编号去重。
          </p>
          <p>
            注册或创建订单时，等待 <code>leadq(&apos;attribution&apos;)</code>{" "}
            返回，将来源快照保存到业务记录；异步支付使用订单保存的触点。
          </p>
          {draft.method === "server" && !readOnly && (
            <>
              <button
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const value = await api(root + "/server-key", {
                      connection_id: conn.id,
                    });
                    setServerKey(value.key);
                    await load();
                  })
                }
              >
                {data?.server ? "更换回传密钥" : "生成回传密钥"}
              </button>
              {serverKey && (
                <>
                  <textarea
                    className="pixel-code"
                    aria-label="服务端回传密钥"
                    value={serverKey}
                    readOnly
                    rows={2}
                  />
                  <button onClick={() => copy(serverKey, "key")}>
                    {copied === "key" ? "已复制" : "复制密钥"}
                  </button>
                  <p>仅本次显示，请保存在网站服务端。</p>
                </>
              )}
            </>
          )}
        </details>
      </div>
    );
    footer = (
      <>
        <button onClick={list} disabled={busy}>
          返回事件列表
        </button>
        <button
          className="primary"
          disabled={busy || readOnly}
          onClick={() =>
            run(async () => {
              await saveEvent();
              list();
            })
          }
        >
          {busy ? "正在保存…" : "保存设置"}
        </button>
      </>
    );
  }
  return (
    <DialogPanel
      key={task}
      title={title}
      size={
        task === "details" || task === "reference"
          ? "wide"
          : task === "detect"
            ? "compact"
            : "default"
      }
      onClose={() =>
        task === "reference" ? onTask(returnTask.current) : close()
      }
      footer={footer}
    >
      {error && (
        <p className="inline-feedback error-feedback" role="alert">
          {error}
          {!data && (
            <button
              className="text-button"
              onClick={() =>
                run(async () => {
                  await load();
                })
              }
            >
              重试读取
            </button>
          )}
        </p>
      )}
      {!data ? <p className="muted">正在读取接入设置…</p> : content}
    </DialogPanel>
  );
}
