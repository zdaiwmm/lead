"use client";
import { useEffect, useId, useState } from "react";
import { Link2, Pencil } from "lucide-react";
import DialogPanel from "./dialog-panel";
import { api } from "./api-client";

export type PromotionConfig = {
  destination_url: string;
  configured: boolean;
  revision: number;
};

export default function PromotionLink({
  aid,
  config,
  readOnly,
  onSaved,
}: {
  aid: string;
  config?: PromotionConfig;
  readOnly: boolean;
  onSaved: (value: PromotionConfig) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="results-promotion">
        <Link2 size={15} aria-hidden="true" />
        <span>推广链接</span>
        <span className="promotion-destination" title={config?.destination_url}>
          {config?.destination_url || (config ? "待配置" : "正在读取…")}
        </span>
        <button
          className="text-button"
          aria-haspopup="dialog"
          onClick={() => setOpen(true)}
        >
          <Pencil size={13} />
          {readOnly ? "查看" : "配置"}
        </button>
      </div>
      {open && (
        <PromotionEditor
          aid={aid}
          readOnly={readOnly}
          onClose={() => setOpen(false)}
          onSaved={(value) => {
            onSaved(value);
            setOpen(false);
          }}
        />
      )}
    </>
  );
}

function PromotionEditor({
  aid,
  readOnly,
  onClose,
  onSaved,
}: {
  aid: string;
  readOnly: boolean;
  onClose: () => void;
  onSaved: (value: PromotionConfig) => void;
}) {
  const id = useId();
  const [config, setConfig] = useState<PromotionConfig | null>(null);
  const [destination, setDestination] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setError("");
    api(`/agents/${aid}/analytics/promotion`)
      .then((value) => {
        if (active) {
          setConfig(value);
          setDestination(value.destination_url);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [aid, attempt]);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!config || busy || readOnly) return;
    setBusy(true);
    setError("");
    try {
      const value = await api(`/agents/${aid}/analytics/promotion`, {
        destination_url: destination.trim(),
        expected_revision: config.revision,
      });
      onSaved(value);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <DialogPanel
      title="推广链接"
      size="compact"
      description="Agent 推广时会自动使用此链接，并附上简短的社媒来源参数。"
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        <>
          <button disabled={busy} onClick={onClose}>
            {readOnly ? "完成" : "取消"}
          </button>
          {!readOnly && (
            <button
              className="primary"
              form={`${id}-form`}
              type="submit"
              disabled={!config || busy || !destination.trim()}
            >
              {busy ? "保存中…" : "保存"}
            </button>
          )}
        </>
      }
    >
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {!config ? (
        <div className="promotion-loading">
          <p className="muted">
            {error ? "推广链接暂时无法读取" : "正在读取推广链接…"}
          </p>
          {error && (
            <button onClick={() => setAttempt((n) => n + 1)}>重试</button>
          )}
        </div>
      ) : (
        <form id={`${id}-form`} className="pixel-section" onSubmit={save}>
          <label htmlFor={`${id}-url`}>推广目标网址</label>
          <input
            id={`${id}-url`}
            className="promotion-input"
            type="url"
            required
            maxLength={2000}
            placeholder="https://your-website.com"
            value={destination}
            disabled={readOnly || busy}
            onChange={(e) => setDestination(e.target.value)}
            aria-describedby={`${id}-hint`}
          />
          <p id={`${id}-hint`} className="muted small promotion-hint">
            可填写首页或具体落地页，原有参数和锚点会保留。仅增加一个短参数，例如{" "}
            <code>lr=tg_…</code>。
          </p>
          {!config.configured && config.destination_url && (
            <p className="muted small promotion-hint">
              当前默认使用产品网址，保存后用于后续推广。
            </p>
          )}
        </form>
      )}
    </DialogPanel>
  );
}
