"use client";
import { useState } from "react";
import { ChevronRight, ExternalLink } from "lucide-react";
type Row = Record<string, any>;
export default function BrowserHistory({
  aid,
  sources,
}: {
  aid: string;
  sources: Row[];
}) {
  const [runs, setRuns] = useState<Row[]>([]),
    [loaded, setLoaded] = useState(false),
    [error, setError] = useState("");
  const [selected, setSelected] = useState<Row | null>(null);
  const unique = Array.from(new Map(sources.map((s) => [s.url, s])).values());
  async function load() {
    if (loaded) return;
    try {
      const values = await Promise.all(
        Array.from(new Set(sources.map((s) => s.run_id))).map(async (id) => {
          const r = await fetch(`/api/agents/${aid}/browser-runs/${id}`);
          if (!r.ok) throw Error("浏览记录暂不可用");
          return r.json();
        }),
      );
      setRuns(values);
      setLoaded(true);
      const steps = values
        .flatMap((v) => v.steps)
        .filter((s) => s.screenshot_url);
      setSelected(steps[steps.length - 1] || null);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="browser-sources">
      <div className="source-links">
        {unique.map((s) => (
          <a key={s.url} href={s.url} target="_blank" rel="noreferrer">
            {s.title || new URL(s.url).hostname}
            <ExternalLink size={11} />
          </a>
        ))}
      </div>
      <details
        onToggle={(e) => {
          if (e.currentTarget.open) load();
        }}
      >
        <summary>
          浏览记录
          <ChevronRight size={12} />
        </summary>
        {error ? (
          <p className="muted small">{error}</p>
        ) : loaded ? (
          <div className="browser-history">
            <div className="browser-step-list">
              {runs
                .flatMap((r) => r.steps)
                .map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setSelected(s)}
                    className={selected?.id === s.id ? "chosen" : ""}
                  >
                    <span>
                      {(
                        {
                          open: "打开网页",
                          click: "点击",
                          read: "阅读",
                          scroll: "滚动",
                          back: "返回",
                          fill: "输入搜索",
                          search_submit: "搜索",
                        } as Row
                      )[s.operation] || s.operation}
                    </span>
                    <strong>
                      {s.page?.title || s.error_code || "操作未完成"}
                    </strong>
                  </button>
                ))}
            </div>
            {selected?.screenshot_url && (
              <img
                src={selected.screenshot_url}
                alt={selected.page?.title || "浏览器实际页面"}
              />
            )}
          </div>
        ) : (
          <p className="muted small">正在读取…</p>
        )}
      </details>
    </div>
  );
}
