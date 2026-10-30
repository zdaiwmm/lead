"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowUpRight, Image as ImageIcon } from "lucide-react";
import { api } from "./api-client";
import DialogPanel from "./dialog-panel";
import SocialIcon, { socialNames } from "./social-icon";
import styles from "./operating-panel.module.css";

type Asset = { id: string; role: string; kind: string };
type Channel = {
  platform: string;
  preparation: { result: { plans: unknown[] } } | null;
};
type Job = {
  id: string;
  state: string;
  platform: string;
  headline: string;
  kind: string;
  purpose: string;
  assets: Asset[] | null;
};
type Data = {
  channels: Channel[];
  media_jobs: Job[];
  reviews: {
    body: { platform: string; next_action: string; state: string };
    created_at: string;
  }[];
};

export default function OperatingPanel({ aid }: { aid: string }) {
  const [data, setData] = useState<Data | null>(null);
  const [open, setOpen] = useState(false);
  const [platform, setPlatform] = useState("");
  const [error, setError] = useState("");
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const next = await api(`/agents/${aid}/operations`);
        if (!signal?.aborted) {
          setData(next);
          setError("");
        }
      } catch (e) {
        if (!signal?.aborted) setError((e as Error).message);
      }
    },
    [aid],
  );

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    load(controller.signal);
    const timer = setInterval(() => load(controller.signal), 30000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [load]);

  const prepared = data?.channels.filter((c) => c.preparation).length ?? 0;
  const jobs = (data?.media_jobs ?? []).filter(
    (j) => !platform || j.platform === platform,
  );
  const visible = jobs.filter((j) => j.state === "succeeded").slice(0, 4);
  const working = jobs.some((j) => j.state === "running");
  const failed = jobs.some((j) => j.state === "failed");
  return (
    <section
      className={`workspace-section ${styles.panel}`}
      aria-label="内容与运营准备"
    >
      <div className="section-head">
        <div>
          <h2>内容与运营准备</h2>
          <p className={styles.summary}>
            {data
              ? `${prepared} 个渠道已备齐方案`
              : error
                ? "准备状态暂时无法读取"
                : "正在读取准备状态"}
            {working ? " · 正在制作素材" : ""}
          </p>
        </div>
        <button
          className="text-button"
          aria-haspopup="dialog"
          onClick={() => setOpen(true)}
        >
          查看
          <ArrowUpRight size={14} />
        </button>
      </div>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
          <button className="text-button" onClick={() => load()}>
            重试
          </button>
        </p>
      )}
      {open && (
        <DialogPanel
          title="内容与运营准备"
          description="查看渠道筹备状态、最近制作的素材与运营复盘。"
          size="wide"
          onClose={() => setOpen(false)}
          footer={<button onClick={() => setOpen(false)}>完成</button>}
        >
          {error && (
            <p role="alert" className={styles.error}>
              {error}
              <button onClick={() => load()}>重试</button>
            </p>
          )}
          <div className={styles.channels} aria-label="渠道筹备状态">
            {(data?.channels ?? []).map((channel) => (
              <span
                key={channel.platform}
                title={
                  channel.preparation
                    ? `${channel.preparation.result.plans.length} 套方案，待账号验证`
                    : "已有运营底稿，启动后自动筹备"
                }
              >
                <SocialIcon
                  platform={channel.platform}
                  size={16}
                  connected={false}
                />
                {socialNames[channel.platform]}
                <small>{channel.preparation ? "已备齐" : "待筹备"}</small>
              </span>
            ))}
          </div>
          <div className={styles.toolbar}>
            <h3>最近制作</h3>
            <select
              aria-label="筛选素材渠道"
              value={platform}
              onChange={(e) => setPlatform(e.target.value)}
            >
              <option value="">全部渠道</option>
              {(data?.channels ?? []).map((c) => (
                <option key={c.platform} value={c.platform}>
                  {socialNames[c.platform]}
                </option>
              ))}
            </select>
          </div>
          {visible.length ? (
            <ul className={styles.assets}>
              {visible.map((job) => {
                const asset = job.assets?.find((a) => a.role === "publication");
                const preview =
                  job.assets?.find((a) => a.role === "preview") ?? asset;
                return (
                  <li key={job.id}>
                    {preview ? (
                      <img
                        className={styles.preview}
                        src={`/api/agents/${aid}/assets/${preview.id}`}
                        alt={job.headline || "Agent 制作的素材"}
                        loading="lazy"
                      />
                    ) : (
                      <ImageIcon size={24} aria-hidden="true" />
                    )}
                    <div className={styles.description}>
                      <strong>{job.headline || "渠道素材"}</strong>
                      <span>
                        {socialNames[job.platform]} ·{" "}
                        {job.purpose === "profile_avatar"
                          ? "品牌头像"
                          : job.purpose === "profile_cover"
                            ? "频道封面"
                            : job.kind === "video"
                              ? "字幕讲解视频"
                              : "原创图文"}
                      </span>
                      {asset && (
                        <a
                          href={`/api/agents/${aid}/assets/${asset.id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          查看素材
                          <span className={styles.srOnly}>
                            （在新标签页打开）
                          </span>
                        </a>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className={styles.empty}>
              {working
                ? "Agent 正在制作素材，完成后会自动显示。"
                : "Agent 会按选题制作图文和讲解视频，成果会显示在这里。"}
            </p>
          )}
          {failed && (
            <p className={styles.note}>
              部分素材未完成，Agent 会根据失败原因调整后重试。
            </p>
          )}
          {data?.reviews[0] && (
            <div className={styles.review}>
              <h3>
                最近一次复盘 · {socialNames[data.reviews[0].body.platform]}
              </h3>
              <p>{data.reviews[0].body.next_action}</p>
            </div>
          )}
          <p className={styles.note}>
            账号连接后，按各渠道的验证结果开始执行；日常选题、素材和排期由 Agent
            负责。
          </p>
        </DialogPanel>
      )}
    </section>
  );
}
