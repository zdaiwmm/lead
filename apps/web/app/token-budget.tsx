"use client";

import { useEffect, useState } from "react";
import { api } from "./api-client";

export default function TokenBudget({
  agent,
  onSaved,
}: {
  agent: string;
  onSaved?: () => void;
}) {
  const [value, setValue] = useState("");
  const [saved, setSaved] = useState<number | null>(null);
  const [custom, setCustom] = useState(false);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    api(`/agents/${agent}/budget`)
      .then((config) => {
        if (!active) return;
        setValue(String(config.daily_token_limit));
        setSaved(config.daily_token_limit);
        setCustom(config.custom_daily_token_limit !== null);
        setVersion(config.config_version);
      })
      .catch((error) => {
        if (active) {
          setMessage(error.message);
          setFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [agent]);
  async function save(limit: number | null) {
    setBusy(true);
    setMessage("");
    setFailed(false);
    try {
      const config = await api(`/agents/${agent}/budget`, {
        daily_token_limit: limit,
        expected_version: version,
      });
      setValue(String(config.daily_token_limit));
      setSaved(config.daily_token_limit);
      setCustom(config.custom_daily_token_limit !== null);
      setVersion(config.config_version);
      setMessage("已保存，立即生效");
      onSaved?.();
    } catch (error) {
      setMessage((error as Error).message);
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="token-budget"
      onSubmit={(event) => {
        event.preventDefault();
        if (value !== "" && Number.isSafeInteger(Number(value)))
          void save(Number(value));
      }}
    >
      <div className="budget-field">
        <label htmlFor={`token-limit-${agent}`}>每日 Token 上限</label>
        <input
          id={`token-limit-${agent}`}
          type="number"
          min="0"
          max="9007199254740991"
          step="1"
          required
          value={value}
          disabled={busy || saved === null}
          onChange={(event) => setValue(event.target.value)}
        />
      </div>
      <p className="field-help">
        每天北京时间 08:00 重置；0 表示停止新增模型调用。
      </p>
      <div className="budget-actions">
        <button
          className="primary"
          type="submit"
          disabled={
            busy || saved === null || value === "" || Number(value) === saved
          }
        >
          {busy ? "保存中…" : "保存额度"}
        </button>
        {custom && (
          <button
            className="text-button"
            type="button"
            disabled={busy}
            onClick={() => void save(null)}
          >
            使用默认额度
          </button>
        )}
      </div>
      {message && (
        <p
          role={failed ? "alert" : "status"}
          className={failed ? "field-error" : "field-feedback"}
        >
          {message}
        </p>
      )}
    </form>
  );
}
