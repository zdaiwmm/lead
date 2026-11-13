"use client";
import { FormEvent, useState } from "react";
import DialogPanel from "./dialog-panel";

type Row = Record<string, any>;
export type ConnectionLogin = { username: string; password: string };

export default function ConnectionDetails({ target, onClose, onConnect }: {
  target: Row;
  onClose: () => void;
  onConnect: (details: { site_url?: string; login?: ConnectionLogin }) => void;
}) {
  const [site, setSite] = useState(target.site_url || "");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const generic = target.id === "web";
  function clear() { setUsername(""); setPassword(""); }
  function close() { clear(); onClose(); }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    let siteUrl: string | undefined;
    if (generic) {
      try {
        const parsed = new URL(site.trim());
        if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.hash)
          throw Error();
        siteUrl = parsed.href;
      } catch {
        setError("请填写完整的网站地址，不含登录信息或片段。");
        return;
      }
    }
    if (Boolean(username.trim()) !== Boolean(password)) {
      setError("使用账号密码登录时，请同时填写账号和密码；也可全部留空。");
      return;
    }
    const details = {
      ...(siteUrl ? { site_url: siteUrl } : {}),
      ...(password ? { login: { username: username.trim(), password } } : {}),
    };
    clear();
    onConnect(details);
  }
  return <DialogPanel title={`连接 ${target.name}`} size="compact" onClose={close}
    description="平台会核对真实登录身份。需要验证码、扫码或本人确认时，会在原窗口提示。">
    <form className="setup-form" onSubmit={submit} autoComplete="off">
      {generic && <label>网站地址<input type="url" required maxLength={2000} value={site}
        onChange={event => { setSite(event.target.value); setError(""); }} placeholder="https://example.com" autoComplete="off" /></label>}
      <p className="field-help">可选：提供本次连接使用的登录信息。留空时先检查已有登录状态。</p>
      <label>账号<input value={username} maxLength={500} onChange={event => { setUsername(event.target.value); setError(""); }}
        autoComplete="off" autoCapitalize="none" spellCheck={false} /></label>
      <label>密码<input type="password" value={password} maxLength={2000} onChange={event => { setPassword(event.target.value); setError(""); }}
        autoComplete="off" /></label>
      <p className="field-help">登录信息仅用于本次连接，结束后清除。</p>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="settings-button-row"><button type="button" onClick={close}>取消</button>
        <button className="primary" type="submit">开始连接</button></div>
    </form>
  </DialogPanel>;
}
