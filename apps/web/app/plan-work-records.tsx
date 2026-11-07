"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { AssetPreview } from "./media-upload";
import { api } from "./api-client";
import { timestamp } from "./operating-plan-model";
import styles from "./operating-plans.module.css";
type Row = Record<string, any>;
export default function PlanWorkRecords({
  aid,
  taskId,
  zone,
  source = false,
  draft = false,
  until,
  materialOnly = false,
  observation = false,
}: {
  aid: string;
  taskId: string;
  zone: string;
  source?: boolean;
  draft?: boolean;
  until?: string;
  materialOnly?: boolean;
  observation?: boolean;
}) {
  const [material, setMaterial] = useState<{
    body?: string;
    format?: string;
    fact_quotes?: string[];
    assets: ({ id: string; media_type: string } | null)[];
  } | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [failedOffset, setFailedOffset] = useState(0);
  const sequence = useRef(0);
  const load = useCallback(
    async (start: number) => {
      const version = ++sequence.current;
      setLoading(true);
      setError("");
      try {
        const data = await api(
          `/agents/${aid}/plans/work/${taskId}/activity?observation=${observation}&offset=${start}${until ? "&until=" + encodeURIComponent(until) : ""}`,
        );
        if (sequence.current !== version) return;
        if (start === 0) setMaterial(data.material || null);
        setRows((old) =>
          start === 0
            ? data.rows
            : [
                ...old,
                ...data.rows.filter(
                  (row: Row) => !old.some((item) => item.id === row.id),
                ),
              ],
        );
        setOffset(start + data.rows.length);
        setMore(data.has_more);
      } catch (e) {
        if (sequence.current === version) {
          setError((e as Error).message);
          setFailedOffset(start);
        }
      } finally {
        if (sequence.current === version) setLoading(false);
      }
    },
    [aid, taskId, until, observation],
  );
  useEffect(() => {
    setMaterial(null);
    setRows([]);
    setOffset(0);
    setMore(false);
    void load(0);
    return () => {
      ++sequence.current;
    };
  }, [load]);
  return (
    <>
      {material && (
        <section className={styles.material} aria-label="稿件与附件">
          <header>
            <h3>
              {material.format === "report" ? "研究与策略材料" : "稿件与附件"}
            </h3>
            <span>
              {(
                {
                  report: "报告",
                  text: "文字稿",
                  image: "图文稿",
                  video: "视频稿",
                } as Record<string, string>
              )[material.format || ""] || "稿件"}{" "}
              ·{" "}
              {material.assets.length
                ? `${material.assets.length} 个附件`
                : "无附件"}
            </span>
          </header>
          {material.body ? (
            <p className={styles.manuscript}>{material.body}</p>
          ) : (
            <p className={styles.muted}>这项任务尚无已保存的正文。</p>
          )}
          {draft && material.body && /https?:\/\//.test(material.body) && <p className={styles.linkNote}>草稿中的官网地址是落地页。创建发布任务时，产品入口会替换为这篇内容专属的平台短链。</p>}
          {material.assets.map((asset, i) =>
            asset ? (
              <figure key={asset.id + i}>
                <AssetPreview
                  aid={aid}
                  assetId={asset.id}
                  kind={
                    asset.media_type.startsWith("video/") ? "video" : "image"
                  }
                />
                <figcaption>
                  <a
                    href={`/api/agents/${aid}/assets/${asset.id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开
                    {asset.media_type.startsWith("video/")
                      ? "视频"
                      : "图片"}{" "}
                    {i + 1} ↗
                  </a>
                </figcaption>
              </figure>
            ) : (
              <p key={i} className={styles.muted}>
                附件 {i + 1} 当前不可查看
              </p>
            ),
          )}
        </section>
      )}
      {!!material?.fact_quotes?.length && (
        <section className={styles.quoteSection} aria-label="稿件引用的业务事实">
          <h3>稿件引用的业务事实 <small>{material.fact_quotes.length} 条</small></h3>
          <ul>{material.fact_quotes.map((quote,i)=><li key={i}>{quote}</li>)}</ul>
        </section>
      )}
      {(!materialOnly || error || loading) && (
        <section
          className={styles.recordTrail}
          aria-label={source ? "内容准备依据" : "执行经过"}
        >
          <div className={styles.recordHeading}>
            <h3>{source ? "稿件准备记录" : "执行记录"}</h3>
            <button
              className="text-button"
              disabled={loading}
              onClick={() => void load(0)}
            >
              刷新
            </button>
          </div>
          {error && (
            <p role="alert" className={styles.error}>
              {error}{" "}
              <button
                disabled={loading}
                onClick={() => void load(failedOffset)}
              >
                重试
              </button>
            </p>
          )}
          <ol>
            {rows.map((row) => (
              <li key={row.id} data-level={row.level}>
                <time dateTime={row.created_at}>
                  {timestamp(row.created_at, zone)}
                </time>
                <div>
                  <p>{row.title}</p>
                  {row.detail && row.detail !== row.platform && (
                    <small>{row.detail}</small>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {loading && (
            <p className={styles.muted} role="status">
              正在读取记录…
            </p>
          )}
          {!rows.length && !loading && !error && (
            <p className={styles.muted}>
              {source ? "暂无准备记录" : "暂无执行记录"}
            </p>
          )}
          {more && !loading && !error && (
            <button className="text-button" onClick={() => void load(offset)}>
              加载更早记录
            </button>
          )}
        </section>
      )}
    </>
  );
}
