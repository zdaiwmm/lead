"use client";
import { useRef, useState } from "react";
import { Paperclip } from "lucide-react";
import { workspaceHeaders } from "./api-client";

type Row = Record<string, any>;
export async function uploadFile(aid: string, file: File) {
  const data = new FormData();
  data.append("file", file);
  const response = await fetch(`/api/agents/${aid}/assets`, {
    method: "POST",
    headers: { "X-Lead-Request": "1", ...workspaceHeaders() },
    body: data,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "上传未完成" }));
    throw Error(error.detail || "上传未完成");
  }
  return response.json();
}
export function MediaUpload({
  aid,
  disabled,
  onUpload,
  onError,
}: {
  aid: string;
  disabled: boolean;
  onUpload: (asset: Row) => void;
  onError: (error: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/jpeg,image/png,image/webp,video/mp4,video/webm"
        hidden
        onChange={async (e) => {
          const file = e.currentTarget.files?.[0];
          if (!file) return;
          setBusy(true);
          try {
            onUpload(await uploadFile(aid, file));
          } catch (error) {
            onError((error as Error).message);
          } finally {
            setBusy(false);
            if (input.current) input.current.value = "";
          }
        }}
      />
      <button
        className="icon"
        aria-label="上传图片或视频"
        disabled={disabled || busy}
        onClick={() => input.current?.click()}
      >
        {busy ? (
          <span className="small">处理中…</span>
        ) : (
          <Paperclip size={18} />
        )}
      </button>
    </>
  );
}
export function AssetPreview({
  aid,
  assetId,
  kind,
}: {
  aid: string;
  assetId: string;
  kind?: string;
}) {
  const src = `/api/agents/${aid}/assets/${assetId}`;
  return kind === "video" ? (
    <video src={src} controls preload="metadata" className="asset-preview" />
  ) : (
    <img src={src} alt="已上传素材" className="asset-preview" loading="lazy" />
  );
}
export function MediaVerification({
  aid,
  card,
  onComplete,
  disabled = false,
}: {
  aid: string;
  card: Row;
  onComplete: () => void;
  disabled?: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [ai, setAi] = useState<string>(
    typeof card.ai_generated === "boolean"
      ? card.ai_generated
        ? "yes"
        : "no"
      : "",
  );
  const [kids, setKids] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const existingId = card.asset_ids?.[0];
  const video = file
    ? file.type.startsWith("video/")
    : card.formats?.[0] === "video";
  const declared = video && ["youtube", "tiktok"].includes(card.platform);
  return (
    <div className="channel-card">
      {existingId && !file && (
        <AssetPreview
          aid={aid}
          assetId={existingId}
          kind={video ? "video" : "image"}
        />
      )}
      <input
        type="file"
        aria-label="选择验证素材"
        accept={
          card.formats?.includes("image") && card.formats?.includes("video")
            ? "image/jpeg,image/png,image/webp,video/mp4"
            : card.formats?.includes("image")
              ? "image/jpeg,image/png,image/webp"
              : "video/mp4"
        }
        disabled={busy || done || disabled}
        onChange={(e) => {
          setFile(e.target.files?.[0] || null);
          setAi("");
        }}
      />
      {declared && (
        <label className="media-question">
          是否包含 AI 生成内容
          <select
            aria-label="是否包含 AI 生成内容"
            value={ai}
            onChange={(e) => setAi(e.target.value)}
          >
            <option value="">请选择</option>
            <option value="no">否</option>
            <option value="yes">是</option>
          </select>
        </label>
      )}
      {video && card.platform === "youtube" && (
        <label className="media-question">
          是否面向儿童
          <select
            aria-label="是否面向儿童"
            value={kids}
            onChange={(e) => setKids(e.target.value)}
          >
            <option value="">请选择</option>
            <option value="no">否</option>
            <option value="yes">是</option>
          </select>
        </label>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        className="primary"
        disabled={
          busy ||
          disabled ||
          done ||
          (!file && !existingId) ||
          (!!declared && !ai) ||
          (video && card.platform === "youtube" && !kids)
        }
        onClick={async () => {
          if (!file && !existingId) return;
          setBusy(true);
          setError("");
          try {
            const assetId = file
              ? (await uploadFile(aid, file)).id
              : existingId;
            const result = await fetch(
              `/api/agents/${aid}/targets/${card.target_id}/verification`,
              {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  "X-Lead-Request": "1",
                  ...workspaceHeaders(),
                },
                body: JSON.stringify({
                  asset_ids: [assetId],
                  ai_generated: ai ? ai === "yes" : null,
                  made_for_kids: kids ? kids === "yes" : null,
                }),
              },
            );
            if (!result.ok) {
              const failure = await result.json();
              throw Error(failure.detail || "验证消息未创建");
            }
            setDone(true);
            onComplete();
          } catch (error) {
            setError((error as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {done ? "验证消息已准备好" : busy ? "处理素材中…" : "准备验证消息"}
      </button>
    </div>
  );
}
