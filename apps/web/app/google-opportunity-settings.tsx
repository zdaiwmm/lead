"use client";

import { useEffect, useMemo, useState } from "react";
import AccountBrowser from "./account-browser";
import { api } from "./api-client";

type Policy = { enabled: boolean; allow_free_signup: boolean };
type Attempt = {
  origin: string;
  status: string;
  reason?: string | null;
  updated_at?: string;
};
type Settings = Policy & {
  account_hint: string | null;
  state: "unbound" | "ready" | "needs_owner";
  attempts: Attempt[];
};

const connectionLabels: Record<Settings["state"], string> = {
  unbound: "未绑定",
  ready: "已验证",
  needs_owner: "需要本人处理",
};

const attemptLabels: Record<string, string> = {
  checking: "正在核对",
  connected: "已登录",
  needs_owner: "需要本人处理",
  no_google_login: "未找到 Google 登录入口",
  unsupported: "已跳过",
  disabled: "已停止",
  unverified: "结果待核对",
  unavailable: "浏览器暂不可用",
};

export default function GoogleOpportunitySettings({ aid }: { aid: string }) {
  const path = `/agents/${aid}/google-opportunities`;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [bindingEmail, setBindingEmail] = useState<string | null>(null);

  useEffect(() => {
    const request = new AbortController();
    api(path, undefined, request.signal)
      .then((data: Settings) => {
        if (!request.signal.aborted) setSettings(data);
      })
      .catch((failure: Error) => {
        if (!request.signal.aborted) setError(failure.message);
      });
    return () => request.abort();
  }, [path]);

  async function update(request: () => Promise<Settings>) {
    setBusy(true);
    setError("");
    try {
      setSettings(await request());
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function save(policy: Policy) {
    void update(() => api(path, policy, undefined, "PUT"));
  }

  const manualLogin = useMemo(() => {
    if (!bindingEmail) return undefined;
    return {
      open: () => api(`${path}/bind`, { email: bindingEmail }),
      verify: async (takeover: string) => {
        const result: Settings = await api(`${path}/verify`, { takeover });
        setSettings(result);
        if (result.state !== "ready") {
          throw Error("请在 Google 页面完成登录，并核对是否使用了指定账号。");
        }
      },
      instructions: "请在 Google 官方页面完成登录和本人验证，然后核对账号。",
    };
  }, [bindingEmail, path]);

  return (
    <section
      className="settings-group google-opportunity-settings"
      aria-labelledby="google-opportunities-heading"
    >
      <div className="settings-group-heading">
        <h3 id="google-opportunities-heading">新平台的 Google 登录</h3>
        <p>
          给此 Agent 绑定一个专用 Google 账号。启用后，Agent
          可在符合条件的新平台授权共享姓名、邮箱和头像。超出基本资料时，等待你处理该平台。
        </p>
      </div>

      {error && (
        <p className="inline-feedback error-feedback" role="alert">
          {error}
        </p>
      )}
      {!settings &&
        (error ? (
          <button disabled={busy} onClick={() => void update(() => api(path))}>
            重试加载
          </button>
        ) : (
          <p className="muted small" role="status">
            正在加载 Google 登录设置…
          </p>
        ))}

      {settings && (
        <>
          <div className="setting-row">
            <span>专用账号</span>
            <span>
              {settings.account_hint && <bdi>{settings.account_hint} · </bdi>}
              {connectionLabels[settings.state]}
            </span>
          </div>
          <form
            className="google-opportunity-bind"
            onSubmit={(event) => {
              event.preventDefault();
              setError("");
              setBindingEmail(email.trim());
              setEmail("");
            }}
          >
            <label>
              <span>Google 账号邮箱</span>
              <input
                type="email"
                required
                maxLength={254}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
              />
            </label>
            <div className="settings-button-row">
              <button type="submit" disabled={busy || !email.trim()}>
                {settings.state === "unbound" ? "打开 Google 登录" : "重新绑定"}
              </button>
              {settings.state !== "unbound" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void update(() => api(`${path}/disconnect`, {}))
                  }
                >
                  停止使用此账号
                </button>
              )}
            </div>
          </form>

          <fieldset className="google-opportunity-policy">
            <legend>自动授权规则</legend>
            <label className="setting-row">
              <span>在新平台自动使用 Google 登录</span>
              <input
                type="checkbox"
                checked={settings.enabled}
                disabled={
                  busy || (settings.state !== "ready" && !settings.enabled)
                }
                onChange={(event) =>
                  save({
                    enabled: event.target.checked,
                    allow_free_signup: settings.allow_free_signup,
                  })
                }
              />
            </label>
            <label className="setting-row">
              <span>允许首次 Google 登录时免费创建平台账号</span>
              <input
                type="checkbox"
                checked={settings.allow_free_signup}
                disabled={
                  busy || !settings.enabled || settings.state !== "ready"
                }
                onChange={(event) =>
                  save({
                    enabled: settings.enabled,
                    allow_free_signup: event.target.checked,
                  })
                }
              />
            </label>
          </fieldset>

          <div className="setting-row">
            <span>最近的平台登录</span>
            <button
              type="button"
              disabled={busy}
              onClick={() => void update(() => api(path))}
            >
              刷新
            </button>
          </div>
          {settings.attempts.length ? (
            <ul className="google-opportunity-attempts">
              {settings.attempts.slice(0, 8).map((attempt, index) => (
                <li key={`${attempt.origin}:${attempt.updated_at ?? index}`}>
                  <bdi>{attempt.origin}</bdi>
                  <span className="muted small">
                    {attemptLabels[attempt.status] ?? "等待核对"}
                    {attempt.reason && ` · ${attempt.reason}`}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">暂无登录记录</p>
          )}
        </>
      )}

      {manualLogin && (
        <AccountBrowser
          aid={aid}
          platform="google"
          name="Google 专用账号"
          manualLogin={manualLogin}
          verifyLabel="核对 Google 登录"
          onClose={() => {
            setBindingEmail(null);
            void update(() => api(path));
          }}
        />
      )}
    </section>
  );
}
