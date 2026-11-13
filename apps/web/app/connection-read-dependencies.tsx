"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "./api-client";

type Candidate = {
  candidate_id: string;
  resource_url: string;
  parent_url: string;
  role: "read_only_frame";
  expires_at: string;
  state: "pending" | "approved" | "refused";
  reason?: string;
};
type Review = { connection_id: string; challenge_version: number; candidates: Candidate[] };

const reasons: Record<string, string> = {
  account_origin_conflict: "此来源涉及另一个账号，无法在本次连接中读取。",
  profile_storage_present: "此来源已有登录或网站资料，无法按公开内容读取。",
  storage_unverifiable: "暂时无法确认此来源能够安全读取。",
  authentication_dependency: "此来源属于身份验证步骤，请在原窗口处理。",
  source_changed: "网页引用的来源已变化，需要重新核对。",
  approval_expired: "本次读取确认已过期。",
  isolation_unsupported: "此内容暂不支持安全的公开读取。",
  identity_required: "此内容需要登录，公开读取无法完成。",
  owner_step_pending: "请先完成原窗口中的输入，再确认此来源。",
};

function address(value: string) {
  try {
    const url = new URL(value);
    return url.origin + url.pathname + (url.search ? "（含链接参数）" : "");
  } catch {
    return "来源地址暂不可用";
  }
}

export default function ConnectionReadDependencies({ aid, connectionId, challengeVersion, disabled, onBusyChange }: {
  aid: string;
  connectionId: string;
  challengeVersion: number;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [refresh, setRefresh] = useState(0);
  const alive = useRef(false);
  const posting = useRef(false);
  const stickyError = useRef(false);
  const sequence = useRef(0);
  const reading = useRef<AbortController | null>(null);
  const writing = useRef<AbortController | null>(null);
  const path = `/agents/${aid}/connections/${connectionId}/read-dependencies`;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      reading.current?.abort();
      writing.current?.abort();
      onBusyChange(false);
    };
  }, [onBusyChange]);

  useEffect(() => {
    let canceled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      if (!posting.current && !document.hidden) {
        const current = ++sequence.current;
        const controller = new AbortController();
        reading.current = controller;
        try {
          const value: Review = await api(path, undefined, controller.signal);
          if (!canceled && current === sequence.current) {
            setReview(value);
            if (!stickyError.current) setError("");
          }
        } catch {
          if (!canceled && !controller.signal.aborted && current === sequence.current)
            setError("暂时无法核对嵌入内容的来源，请刷新后再确认。");
        }
      }
      if (!canceled) timer = setTimeout(read, 4000);
    }
    read();
    return () => {
      canceled = true;
      clearTimeout(timer);
      reading.current?.abort();
    };
  }, [path, challengeVersion, refresh]);

  async function decide(candidate: Candidate, decision: "approve" | "refuse") {
    if (posting.current || disabled || error || review?.connection_id !== connectionId || review?.challenge_version !== challengeVersion) return;
    posting.current = true;
    ++sequence.current;
    reading.current?.abort();
    const controller = new AbortController();
    writing.current = controller;
    setBusy(candidate.candidate_id);
    onBusyChange(true);
    stickyError.current = false;
    setError("");
    try {
      const value: Review = await api(path, {
        challenge_version: challengeVersion, candidate_id: candidate.candidate_id, decision,
      }, controller.signal);
      if (alive.current) setReview(value);
    } catch (failure) {
      if (alive.current && !controller.signal.aborted) {
        stickyError.current = true;
        setError(failure instanceof Error ? failure.message : "确认结果尚待核对，请刷新后查看。");
      }
    } finally {
      posting.current = false;
      if (alive.current) {
        setBusy("");
        onBusyChange(false);
        setRefresh(value => value + 1);
      }
    }
  }

  if (!error && !review?.candidates.length) return null;
  const stale = review?.connection_id !== connectionId || review?.challenge_version !== challengeVersion;
  return <section className="connection-read-dependencies" aria-labelledby="read-dependencies-title">
    <h3 id="read-dependencies-title">确认嵌入内容来源</h3>
    <p className="muted small">网页引用了以下网站。确认后仅查看公开内容，不发送登录信息。保持关闭时，本次连接无法继续。</p>
    {review?.candidates.map(candidate => {
      const expired = Date.parse(candidate.expires_at) <= Date.now();
      const pending = candidate.state === "pending" && !expired;
      return <div className="connection-read-dependency" role="group" aria-label={address(candidate.resource_url)} key={candidate.candidate_id}>
        <div>
          <p className="connection-read-address connection-read-address-summary"><bdi>{address(candidate.resource_url)}</bdi></p>
          {address(candidate.resource_url).length > 160 && <details className="connection-read-full-address">
            <summary>查看完整页面地址</summary><p className="connection-read-address"><bdi>{address(candidate.resource_url)}</bdi></p>
          </details>}
          <p className="muted small" role="status">{candidate.reason ? reasons[candidate.reason] || "此来源暂时无法读取。"
            : expired ? "本次读取确认已过期。" : candidate.state === "approved" ? "已允许公开读取"
            : candidate.state === "refused" ? "保持关闭" : "等待确认"}</p>
        </div>
        {pending && <div className="connection-read-actions">
          <button className="text-button" disabled={disabled || !!busy || !!error || stale}
            onClick={() => decide(candidate, "refuse")}>保持关闭</button>
          <button disabled={disabled || !!busy || !!error || stale}
            onClick={() => decide(candidate, "approve")}>{busy === candidate.candidate_id ? "正在核对…" : "允许读取"}</button>
        </div>}
      </div>;
    })}
    {stale && <p className="muted small" role="status">连接状态已变化，正在重新核对。</p>}
    {error && <div className="connection-read-error" role="alert"><span>{error}</span>
      <button className="text-button" disabled={!!busy || disabled} onClick={() => {
        stickyError.current = false;
        setRefresh(value => value + 1);
      }}>刷新</button>
    </div>}
  </section>;
}
