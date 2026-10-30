"use client";
import { useId, useState } from "react";
import { Pencil, Upload, Check } from "lucide-react";
import AgentAvatar, { avatarSeed } from "./agent-avatar";
import DialogPanel from "./dialog-panel";
import { api, workspaceHeaders } from "./api-client";
type Row = Record<string, any>;
export default function AgentIdentity({
  agent,
  readOnly,
  onComplete,
}: {
  agent: Row;
  readOnly: boolean;
  onComplete: () => Promise<void>;
  onError?: (s: string) => void;
}) {
  const [editing, setEditing] = useState(false),
    [name, setName] = useState(""),
    [seed, setSeed] = useState(0),
    [asset, setAsset] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const formId = useId();
  function begin() {
    setName(agent.name);
    setSeed(avatarSeed(agent));
    setAsset(agent.onboarding_state?.avatar_asset_id ?? null);
    setError("");
    setEditing(true);
  }
  async function upload(file?: File) {
    if (!file) return;
    if (!file.type.startsWith("image/") || file.size > 2 * 1024 * 1024) {
      setError("请选择 2 MB 以内的图片");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const r = await fetch(`/api/agents/${agent.id}/assets`, {
        method: "POST",
        headers: { ...workspaceHeaders(), "X-Lead-Request": "1" },
        body,
      });
      if (!r.ok) throw Error("头像上传未完成，请重试");
      setAsset((await r.json()).id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api(`/agents/${agent.id}/identity`, {
        name: name.trim(),
        avatar_seed: seed,
        avatar_asset_id: asset,
      });
      await onComplete();
      setEditing(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const preview = {
    ...agent,
    onboarding_state: {
      ...agent.onboarding_state,
      avatar_seed: seed,
      avatar_asset_id: asset,
    },
  };
  return (
    <>
      <div className="agent-title-group">
        <button
          className="avatar-edit"
          disabled={readOnly}
          aria-label="编辑 Agent 头像和名称"
          aria-haspopup="dialog"
          onClick={begin}
        >
          <AgentAvatar agent={agent} size={40} />
          <span>
            <Pencil size={12} />
          </span>
        </button>
        <div>
          <div className="agent-name-line">
            <h1 title={agent.name}>{agent.name}</h1>
            <button
              className="icon"
              aria-label="编辑 Agent 名称"
              aria-haspopup="dialog"
              disabled={readOnly}
              onClick={begin}
            >
              <Pencil size={13} />
            </button>
          </div>
          <p className="muted small">
            {agent.business?.product_url
              ?.replace(/^https?:\/\//, "")
              .replace(/\/$/, "") || "等待了解你的业务"}
          </p>
        </div>
      </div>
      {editing && (
        <DialogPanel
          title="编辑 Agent 资料"
          description="设置头像与名称，让你的 Agent 更有辨识度。"
          size="compact"
          onClose={() => {
            if (!busy) setEditing(false);
          }}
          footer={
            <>
              <button disabled={busy} onClick={() => setEditing(false)}>
                取消
              </button>
              <button
                className="primary"
                form={formId}
                type="submit"
                disabled={busy || !name.trim()}
              >
                <Check size={14} />
                {busy ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          <form
            id={formId}
            className="modal-edit-form"
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            {error && (
              <p className="inline-feedback error-feedback" role="alert">
                {error}
              </p>
            )}
            <div className="identity-preview">
              <AgentAvatar agent={preview} size={72} />
              <div>
                <strong>{name.trim() || "你的 Agent"}</strong>
                <p>像素头像或你自己的图片</p>
              </div>
            </div>
            <label>
              Agent 名称
              <input
                required
                maxLength={80}
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={busy}
                autoFocus
              />
            </label>
            <fieldset className="avatar-field" disabled={busy}>
              <legend>选择像素头像</legend>
              <div className="avatar-options">
                {Array.from({ length: 8 }, (_, i) => (
                  <button
                    key={i}
                    type="button"
                    aria-label={`像素头像 ${i + 1}`}
                    aria-pressed={!asset && seed === i}
                    onClick={() => {
                      setSeed(i);
                      setAsset(null);
                    }}
                  >
                    <AgentAvatar seed={i} size={36} />
                  </button>
                ))}
              </div>
              <label className="avatar-upload">
                <Upload size={15} />
                <span>{asset ? "更换上传图片" : "上传自己的图片"}</span>
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  onChange={(e) => upload(e.target.files?.[0])}
                />
              </label>
              <p className="field-help">
                支持 JPG、PNG、WebP，大小不超过 2 MB。
              </p>
            </fieldset>
          </form>
        </DialogPanel>
      )}
    </>
  );
}
