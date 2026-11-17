import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const webRequire = createRequire(new URL("../apps/web/package.json", import.meta.url));
const React = webRequire("react");
const { renderToStaticMarkup } = webRequire("react-dom/server");
const ts = webRequire("typescript");
const file = new URL("../apps/web/app/growth-status.tsx", import.meta.url);
const compiled = ts.transpileModule(readFileSync(file, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  fileName: file.pathname,
}).outputText;
let snapshot;
const empty = () => null;
const imports = {
  react: React,
  "react/jsx-runtime": webRequire("react/jsx-runtime"),
  "lucide-react": { ArrowUpRight: empty, RefreshCw: empty },
  "./api-client": { api() { throw Error("Rendering must not send an API request"); } },
  "./analytics-summary": { useScopedData: () => ({ data: snapshot, refresh: empty }) },
  "./agent-results": { default: empty },
  "./dialog-panel": { default: empty },
  "./media-upload": { AssetPreview: empty },
  "./social-icon": { default: empty, socialNames: {} },
  "./growth-status.module.css": { default: {} },
};
const fixtureModule = { exports: {} };
new Function("require", "module", "exports", compiled)((name) => {
  assert.ok(Object.hasOwn(imports, name), `Unexpected UI dependency: ${name}`);
  return { __esModule: true, ...imports[name] };
}, fixtureModule, fixtureModule.exports);

function render(continuation, extra = {}, agent = { desired_state: "running" }) {
  snapshot = {
    objective: { description: "Find a relevant audience" },
    current_learning: [], current_work: [], waiting: [], recent_work: [], owner_actions: [],
    continuation, ...extra,
  };
  return renderToStaticMarkup(React.createElement(fixtureModule.exports.default, {
    aid: "fixture-agent", agent, readOnly: false,
  }));
}

const proposal = {
  status: "waiting_for_change", rationale: "The prior path needs new evidence.",
  condition: "A relevant original source becomes available.", next_action: "Inspect that source.",
  responsibility: "product",
};

test("a missing continuation producer is shown as an unformed next step", () => {
  const text = render({ ...proposal, check: { valid: false, status: "missing" } });
  assert.match(text, /下一步尚未形成/);
  assert.doesNotMatch(text, /由 Lead 处理并核对依据|等待新的依据|需要你补充相关条件/);
});

test("historical continuation prose does not claim a verified wait", () => {
  const text = render({ ...proposal, responsibility: "owner" });
  assert.match(text, /等待依据尚未核对/);
  assert.doesNotMatch(text, /由 Lead 处理并核对依据|需要你补充相关条件/);
});

test("a verified experiment wait shows its original review time", () => {
  const text = render({ ...proposal, check: { valid: true, status: "waiting", source: {
    ref: { kind: "experiment", id: "private-id" },
    check_at: "2026-11-18T03:30:00+00:00", responsibility: "product",
  } } });
  assert.match(text, /原观察窗口/);
  assert.match(text, /复盘时间/);
  assert.doesNotMatch(text, /private-id|下一步尚未形成|等待依据尚未核对/);
});

test("a verified task wait shows the server task source", () => {
  const text = render({ ...proposal, check: { valid: true, status: "waiting", source: {
    ref: { kind: "task", id: "private-id" },
    check_at: "2026-11-18T03:30:00+00:00", responsibility: "platform",
  } } });
  assert.match(text, /原任务/);
  assert.match(text, /下次检查/);
  assert.doesNotMatch(text, /private-id|由 Lead 处理并核对依据/);
});

test("a stopped resource does not advertise an automatic check", () => {
  const text = render({ ...proposal, status: "blocked", check: { valid: true, status: "blocked", source: {
    ref: { kind: "resource", id: "private-id" },
    check_at: "2026-11-18T03:30:00+00:00", responsibility: "resource",
  } } });
  assert.match(text, /当前下一步受阻/);
  assert.match(text, /未安排自动复查/);
  assert.doesNotMatch(text, /下次检查|private-id|由 Lead 处理并核对依据/);
});

test("an observing experiment can show the verified wait without current tasks", () => {
  const text = render({ ...proposal, check: { valid: true, status: "waiting", source: {
    ref: { kind: "experiment", id: "private-id" }, check_at: "2026-11-18T03:30:00+00:00",
  } } }, { current_learning: [{ state: "observing", question: "Does this audience respond?", learning_signal: "Actual responses" }] });
  assert.match(text, /Does this audience respond/);
  assert.match(text, /复盘时间/);
  assert.doesNotMatch(text, /下一步尚未形成/);
});

test("paused status preserves original-result reconciliation", () => {
  const text = render(null, {}, { desired_state: "paused" });
  assert.match(text, /项目已暂停。原操作的结果核对仍会继续/);
});

test("pause hides a prior business wait while preserving unknown-result work", () => {
  const text = render({ ...proposal, check: { valid: true, status: "waiting", source: {
    ref: { kind: "experiment", id: "private-id" }, check_at: "2026-11-18T03:30:00+00:00",
  } } }, { waiting: [{ id: "original-action", title: "核对原消息", state: "reconciling",
    wait: { reason: "unknown_receipt", check_at: "2026-11-18T03:30:00+00:00" } }] }, { desired_state: "paused" });
  assert.match(text, /核对原消息|正在核对原操作，结果尚未确认/);
  assert.doesNotMatch(text, /等待原观察窗口复盘|private-id/);
});
