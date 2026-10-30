"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, LoaderCircle } from "lucide-react";
import DialogPanel from "./dialog-panel";
import AccountBrowser from "./account-browser";
import SocialIcon, { socialNames } from "./social-icon";
import { api, getWorkspace } from "./api-client";
import SetupFields, {
  initialProfile,
  SetupProfile,
  SetupTemplate,
} from "./setup-fields";
type Row = Record<string, any>;
export default function NewAgentDialog({
  initial = "",
  platforms,
  onClose,
  onCreated,
}: {
  initial?: string;
  platforms: Row[];
  onClose: () => void;
  onCreated: (id: string) => Promise<void>;
}) {
  const [step, setStep] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [browser, setBrowser] = useState(false),
    [draft, setDraft] = useState<Row | null>(null);
  const [template, setTemplate] = useState<SetupTemplate | null>(null);
  const pinnedTemplate = useRef(false);
  const [profile, setProfile] = useState<SetupProfile>({
    product_name: "",
    product_url: /^https?:\/\/\S+$/.test(initial) ? initial : "",
    description: /^https?:\/\/\S+$/.test(initial) ? "" : initial,
    audience: "",
    goal: "获得更多有效访问和潜在客户",
    content_expectations: "",
    strategy: "稳健持续：先验证内容方向，再逐步扩大运营。",
    platform: "telegram",
  });
  const key = useRef(crypto.randomUUID());
  const form = useRef<HTMLFormElement>(null);
  function field(name: keyof typeof profile, value: string) {
    setProfile((p) => ({ ...p, [name]: value }));
  }
  async function close() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (draft) await api(`/agents/${draft.id}/setup/cancel`, {});
      try {
        localStorage.removeItem(storageKey);
        localStorage.removeItem(storageKey + ".form");
      } catch {}
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function connect() {
    setBusy(true);
    setError("");
    try {
      const next = draft
        ? await api(`/agents/${draft.id}/setup`, {
            ...profile,
            expected_revision: draft.config_revision,
          })
        : await api("/agents/setup", {
            ...profile,
            client_id: key.current,
            template_version: template?.version,
          });
      setDraft(next);
      try {
        localStorage.setItem(storageKey, next.id);
      } catch {}
      setBrowser(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function complete() {
    if (!draft) return;
    await api(`/agents/${draft.id}/setup/complete`, {});
    try {
      localStorage.removeItem(storageKey);
      localStorage.removeItem(storageKey + ".form");
    } catch {}
    await onCreated(draft.id);
  }
  const storageKey = `lead.agent-setup.${getWorkspace() || "local"}`;
  const [restored, setRestored] = useState(false),
    [recoveryRetry, setRecoveryRetry] = useState(0);
  useEffect(() => {
    let active = true;
    let id: string | null = null;
    try {
      id = localStorage.getItem(storageKey);
    } catch {}
    if (!id || !/^[-a-f0-9]{36}$/.test(id)) return;
    setBusy(true);
    setDraft({ id, config_revision: 0 });
    api(`/agents/${id}`)
      .then(async (a) => {
        if (!active) return;
        if (a.onboarding_state?.setup_pending) {
          pinnedTemplate.current = true;
          const savedTemplate =
            a.onboarding_state.setup_template ||
            (await api("/onboarding-template"));
          if (!active) return;
          setTemplate(savedTemplate);
          setProfile(a.onboarding_state.setup);
          setDraft({ id, config_revision: a.config_revision });
          setStep(2);
          setRestored(true);
          setError("");
        } else {
          setDraft(null);
          try {
            localStorage.removeItem(storageKey);
            localStorage.removeItem(storageKey + ".form");
          } catch {}
        }
      })
      .catch((e) => {
        if (!active) return;
        if (e.message === "Agent 不存在") {
          setDraft(null);
          try {
            localStorage.removeItem(storageKey);
            localStorage.removeItem(storageKey + ".form");
          } catch {}
        } else setError("未完成的创建资料暂时无法读取，请重试。");
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [storageKey, recoveryRetry]);
  const formStorageKey = storageKey + ".form";
  const [formLoaded, setFormLoaded] = useState(false);
  useEffect(() => {
    let active = true;
    api("/onboarding-template")
      .then((next: SetupTemplate) => {
        if (!active) return;
        if (!pinnedTemplate.current) {
          setTemplate(next);
          let saved: Row | null = null;
          try {
            saved = JSON.parse(localStorage.getItem(formStorageKey) || "null");
          } catch {}
          if (saved?.profile && saved?.template && saved?.client_id) {
            setProfile(saved.profile);
            setTemplate(saved.template);
            key.current = saved.client_id;
            setStep(Math.max(0, Math.min(2, saved.step || 0)));
            setRestored(true);
          } else setProfile(initialProfile(next, initial));
        }
        setFormLoaded(true);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [formStorageKey, initial, recoveryRetry]);
  useEffect(() => {
    if (!template || !formLoaded) return;
    try {
      localStorage.setItem(
        formStorageKey,
        JSON.stringify({ profile, template, step, client_id: key.current }),
      );
    } catch {}
  }, [profile, template, step, formStorageKey, formLoaded]);
  const chosen = platforms.find((p) => p.id === profile.platform);
  if (browser && draft)
    return (
      <AccountBrowser
        aid={draft.id}
        platform={profile.platform}
        name={socialNames[profile.platform]}
        verifyLabel="验证并创建 Agent"
        onVerified={complete}
        onClose={() => setBrowser(false)}
      />
    );
  return (
    <DialogPanel
      title="新建 Agent"
      description="告诉 Agent 你的业务和运营方向，连接一个渠道即可开始。"
      onClose={onClose}
      footer={
        <>
          <button
            disabled={busy || !template || draft?.config_revision === 0}
            onClick={() =>
              step ? (setError(""), setStep((s) => s - 1)) : close()
            }
          >
            {step ? (
              <>
                <ArrowLeft size={14} />
                上一步
              </>
            ) : (
              "取消"
            )}
          </button>
          <button
            className="primary"
            disabled={busy || !template || draft?.config_revision === 0}
            onClick={() => {
              if (step === 2) {
                connect();
                return;
              }
              form.current?.requestSubmit();
            }}
          >
            {busy ? (
              <>
                <LoaderCircle size={14} className="spin" />
                正在准备…
              </>
            ) : step === 2 ? (
              <>
                <SocialIcon platform={profile.platform} size={15} />
                连接 {socialNames[profile.platform]}
              </>
            ) : (
              <>
                下一步
                <ArrowRight size={14} />
              </>
            )}
          </button>
        </>
      }
    >
      <ol className="setup-steps" aria-label="创建进度">
        {(template?.body.steps || ["产品信息", "运营方向", "连接渠道"]).map(
          (name, i) => (
            <li
              key={name}
              className={i === step ? "current" : i < step ? "complete" : ""}
              aria-current={i === step ? "step" : undefined}
            >
              <span>{i < step ? <Check size={12} /> : i + 1}</span>
              {name}
            </li>
          ),
        )}
      </ol>
      {!template && !error && <p role="status">正在读取创建引导…</p>}
      {template && (
        <p className="muted small">
          引导版本 {template.version ? `v${template.version}` : "默认"} ·
          关闭后可继续填写
        </p>
      )}
      {restored && (
        <p className="setup-restored muted small" role="status">
          已恢复上次未完成的创建资料。
        </p>
      )}
      {error && (
        <p className="inline-feedback error-feedback" role="alert">
          {error}
          {(!template || draft?.config_revision === 0) && (
            <button
              onClick={() => setRecoveryRetry((n) => n + 1)}
              disabled={busy || !template || draft?.config_revision === 0}
            >
              重试恢复
            </button>
          )}
        </p>
      )}
      <form
        ref={form}
        className="setup-form"
        onSubmit={(e) => {
          e.preventDefault();
          setError("");
          setStep((s) => s + 1);
        }}
      >
        <fieldset
          className="setup-fields"
          disabled={busy || !template || draft?.config_revision === 0}
        >
          {step < 2 ? (
            template && (
              <SetupFields
                step={step}
                template={template}
                profile={profile}
                onChange={field}
              />
            )
          ) : (
            <>
              <div className="setup-product-summary">
                <strong>{profile.product_name}</strong>
                <span>{profile.goal}</span>
              </div>
              <div className="setup-channel-heading">
                <h3>先连接一个运营渠道</h3>
                <p>
                  登录验证成功后，Agent 会创建并进入工作台。其他渠道可稍后添加。
                </p>
              </div>
              <div
                className="setup-platforms"
                role="radiogroup"
                aria-label="选择首个运营渠道"
              >
                {Object.entries(socialNames).map(([id, name]) => {
                  const p = platforms.find((p) => p.id === id);
                  return (
                    <button
                      key={id}
                      type="button"
                      role="radio"
                      aria-checked={profile.platform === id}
                      onClick={() => field("platform", id)}
                    >
                      <SocialIcon platform={id} />
                      <span>{name}</span>
                      {p?.automation && <small>自主运营</small>}
                    </button>
                  );
                })}
              </div>
              <p className="setup-channel-note">
                {chosen?.automation
                  ? "连接后可在工作台启动自主运营，Agent 会继续完成运营准备。"
                  : "此渠道支持账号连接，自主运营能力尚未开放。可优先选择标有“自主运营”的渠道。"}
              </p>
            </>
          )}
        </fieldset>
      </form>
    </DialogPanel>
  );
}
