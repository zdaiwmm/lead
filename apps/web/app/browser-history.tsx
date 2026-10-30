"use client";
import { useState } from "react";
import { ChevronRight, ExternalLink, RefreshCw } from "lucide-react";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";
type Row = Record<string, any>;
const labels: Row = {
  open: "打开网页",
  click: "点击",
  read: "阅读",
  scroll: "滚动",
  back: "返回",
  fill: "输入搜索",
  search_submit: "搜索",
};
export default function BrowserHistory({
  aid,
  sources,
}: {
  aid: string;
  sources: Row[];
}) {
  const [open, setOpen] = useState(false),
    [runs, setRuns] = useState<Row[]>([]),
    [loaded, setLoaded] = useState(false),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [selected, setSelected] = useState<Row | null>(null);
  const unique = Array.from(new Map(sources.map((s) => [s.url, s])).values());
  async function load(force = false) {
    if (loaded && !force) return;
    setLoading(true);
    setError("");
    try {
      const values = await Promise.all(
        Array.from(new Set(sources.map((s) => s.run_id).filter(Boolean))).map(
          (id) => api(`/agents/${aid}/browser-runs/${id}`),
        ),
      );
      setRuns(values);
      setLoaded(true);
      const steps = values.flatMap((v) => v.steps);
      setSelected(
        steps.filter((s) => s.screenshot_url).at(-1) || steps.at(-1) || null,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  const steps = runs.flatMap((r) => r.steps);
  return (
    <div className="browser-sources">
      <div className="source-links">
        {unique.map((s) => (
          <a key={s.url} href={s.url} target="_blank" rel="noreferrer">
            {s.title || s.url}
            <ExternalLink size={11} />
          </a>
        ))}
      </div>
      {sources.some((s) => s.run_id) && (
        <button
          className="text-button browser-history-trigger"
          aria-haspopup="dialog"
          onClick={() => {
            setOpen(true);
            load();
          }}
        >
          浏览记录
          <ChevronRight size={12} />
        </button>
      )}
      {open && (
        <DialogPanel
          title="浏览记录"
          description="查看 Agent 阅读网页的步骤与留存画面。"
          size="wide"
          onClose={() => setOpen(false)}
          footer={
            <>
              <button
                className="text-button"
                disabled={loading}
                onClick={() => load(true)}
              >
                <RefreshCw size={13} />
                刷新
              </button>
              <button onClick={() => setOpen(false)}>关闭</button>
            </>
          }
        >
          {error ? (
            <div className="inline-feedback error-feedback" role="alert">
              <span>{error}</span>
              <button onClick={() => load(true)}>重试</button>
            </div>
          ) : loading ? (
            <p className="muted">正在读取浏览记录…</p>
          ) : steps.length ? (
            <div className="browser-history-layout">
              <div className="history-step-buttons">
                {steps.map((s, i) => (
                  <button
                    key={s.id}
                    aria-pressed={selected?.id === s.id}
                    onClick={() => setSelected(s)}
                  >
                    <span>
                      {String(i + 1).padStart(2, "0")} ·{" "}
                      {labels[s.operation] || "网页操作"}
                    </span>
                    <strong>
                      {s.page?.title ||
                        (s.error_code ? "操作未完成" : "网页记录")}
                    </strong>
                  </button>
                ))}
              </div>
              <div className="history-preview">
                {selected?.screenshot_url ? (
                  <img
                    src={selected.screenshot_url}
                    alt={selected.page?.title || "浏览器实际页面"}
                  />
                ) : (
                  <p className="muted empty-copy">此步骤未保存页面截图。</p>
                )}
                {selected && (
                  <p className="muted small">
                    {labels[selected.operation] || "网页操作"} ·{" "}
                    {selected.page?.title || "网页记录"}
                  </p>
                )}
              </div>
            </div>
          ) : (
            <p className="muted empty-copy">暂无可展示的浏览步骤。</p>
          )}
        </DialogPanel>
      )}
    </div>
  );
}
