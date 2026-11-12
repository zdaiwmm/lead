"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  X,
} from "lucide-react";
import SocialIcon from "./social-icon";
import type { ConnectionLogin } from "./connection-details";
import ConnectionReadDependencies from "./connection-read-dependencies";
import { api, workspaceHeaders } from "./api-client";
type Row = Record<string, any>;
export default function AccountBrowser({
  aid,
  platform,
  requestedAccountId,
  newAccount = false,
  siteUrl,
  login,
  onCredentialsSent,
  name,
  onClose,
  onVerified,
  verifyLabel = "验证并完成",
}: {
  aid: string;
  platform: string;
  requestedAccountId?: string;
  newAccount?: boolean;
  siteUrl?: string;
  login?: ConnectionLogin;
  onCredentialsSent?: () => void;
  name: string;
  onClose: () => void;
  onVerified?: (started?: boolean) => Promise<void>;
  verifyLabel?: string;
}) {
  const [state, setState] = useState("opening");
  const [image, setImage] = useState<{
    url: string;
    width: number;
    height: number;
  } | null>(null);
  const [error, setError] = useState("");
  const [frameError, setFrameError] = useState("");
  const [managedStart, setManagedStart] = useState(false);
  const [automatic, setAutomatic] = useState(false);
  const automaticConnection = useRef(false);
  const initialDetails = useRef({ site_url: siteUrl, login });
  const ownerInput = useRef(false);
  const challengeVersion = useRef(0);
  const [controlMode, setControlMode] = useState("automatic");
  const [ownerAction, setOwnerAction] = useState("");
  const [canReviewDependencies, setCanReviewDependencies] = useState(false);
  const [dependencyBusy, setDependencyBusy] = useState(false);
  function updateControl(session: Row) {
    if (automaticConnection.current && typeof session.challenge_version === "number" &&
      session.challenge_version < challengeVersion.current) return false;
    const mode = session.control_mode === "owner" ? "owner" : "automatic";
    if (ownerInput.current && (mode !== "owner" || challengeVersion.current !== session.challenge_version)) {
      queue.current = [];
      inputController.current?.abort();
      keyboard.current && (keyboard.current.value = "");
      composing.current = false;
    }
    ownerInput.current = !automaticConnection.current || mode === "owner";
    challengeVersion.current = session.challenge_version || 0;
    setControlMode(mode);
    setOwnerAction(session.owner_action || "");
    setCanReviewDependencies(session.can_review_read_dependencies === true);
    return true;
  }
  const [verifying, setVerifying] = useState(false);
  const [closing, setClosing] = useState(false);
  const [retry, setRetry] = useState(0);
  const [latency, setLatency] = useState<number | null>(null);
  const [challenge, setChallenge] = useState<Row | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const keyboard = useRef<HTMLTextAreaElement>(null);
  const imageUrl = useRef("");
  const frameTag = useRef("");
  const mounted = useRef(false);
  const takeover = useRef("");
  const accountId = useRef<string | undefined>(undefined);
  const lastInput = useRef(0);
  const nextFrame = useRef(0);
  const expire = useCallback(() => {
    active.current = false;
    queue.current = [];
    setFrameError("");
    setError("接管已结束，点击重试可重新打开登录窗口");
    setState("failed");
    setLatency(null);
  }, []);
  const preview = useRef(false);
  const frameController = useRef<AbortController | null>(null);
  const queue = useRef<Row[]>([]);
  const draining = useRef<Promise<boolean> | null>(null);
  const inputController = useRef<AbortController | null>(null);
  const composing = useRef(false);
  const inputTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollDelta = useRef(0);
  const stopping = useRef(false);
  const active = useRef(false);
  const connection = useRef<{ key: string; promise: Promise<any> } | null>(
    null,
  );
  const closeButton = useRef<HTMLButtonElement>(null);
  const readFrame = useCallback(
    async (
      response: Response,
      controller: AbortController,
      started: number,
      compact: boolean,
    ) => {
      const current = () =>
        mounted.current &&
        active.current &&
        !stopping.current &&
        !controller.signal.aborted;
      if (!current()) return;
      if (!response.ok && response.status !== 304) {
        const failure = await response.json().catch(() => ({}));
        if (!current()) return;
        if (failure.detail === "接管已过期") {
          expire();
          return;
        }
        if (failure.code === "workspace_changed") window.location.reload();
        if (response.status === 401)
          window.dispatchEvent(
            new CustomEvent("lead:session-expired", {
              detail: failure.login_url || "/api/auth/login",
            }),
          );
        throw Error(failure.detail || "画面连接中断，请刷新重试");
      }
      if (response.status !== 204 && response.status !== 304) {
        const blob = await response.blob();
        if (!current()) return;
        frameTag.current =
          response.headers.get(compact ? "x-lead-frame-tag" : "etag") || "";
        preview.current = compact;
        const url = URL.createObjectURL(blob);
        const previous = imageUrl.current;
        imageUrl.current = url;
        setImage({
          url,
          width: Number(response.headers.get("x-lead-frame-width")),
          height: Number(response.headers.get("x-lead-frame-height")),
        });
        if (previous) URL.revokeObjectURL(previous);
      }
      setFrameError("");
      setLatency(Math.round(performance.now() - started));
    },
    [expire],
  );

  const shot = useCallback(async () => {
    if (
      !mounted.current ||
      !active.current ||
      draining.current ||
      frameController.current ||
      stopping.current
    )
      return;
    const controller = new AbortController();
    frameController.current = controller;
    const started = performance.now();
    const compact = !!imageUrl.current && started - lastInput.current < 1800;
    const headers = workspaceHeaders();
    if (frameTag.current && (compact || !preview.current))
      headers[compact ? "X-Lead-Frame-Tag" : "If-None-Match"] =
        frameTag.current;
    const url = compact
      ? `/browser-control/${aid}?takeover=${takeover.current}`
      : `/api/agents/${aid}/browser/screenshot?format=jpeg&takeover=${takeover.current}`;
    try {
      const response = await fetch(url, {
        headers,
        cache: "no-store",
        signal: controller.signal,
      });
      await readFrame(response, controller, started, compact);
    } catch (e) {
      if (mounted.current && !controller.signal.aborted)
        setFrameError((e as Error).message);
    } finally {
      if (frameController.current === controller) {
        frameController.current = null;
        nextFrame.current =
          performance.now() +
          (performance.now() - lastInput.current < 1800 ? 30 : 800);
      }
    }
  }, [aid, readFrame]);

  function drain(): Promise<boolean> {
    if (draining.current) return draining.current;
    if (!active.current || !ownerInput.current) return Promise.resolve(false);
    if (!queue.current.length) return Promise.resolve(true);
    frameController.current?.abort();
    const controller = new AbortController();
    inputController.current = controller;
    const task = (async () => {
      let timeout: ReturnType<typeof setTimeout> | undefined;
      let timedOut = false;
      try {
        while (
          queue.current.length &&
          active.current &&
          !controller.signal.aborted
        ) {
          const command = queue.current.shift()!;
          if (!ownerInput.current) { queue.current = []; return false; }
          const body = { ...command, takeover: takeover.current,
            ...(automaticConnection.current ? { challenge_version: challengeVersion.current } : {}) };
          timeout = setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, 45_000);
          // Input acknowledgement must not wait for screenshot capture or compression.
          await api(`/agents/${aid}/browser/input`, body, controller.signal);
          clearTimeout(timeout);
        }
        return mounted.current && active.current && !controller.signal.aborted;
      } catch (e) {
        if (mounted.current && (timedOut || !controller.signal.aborted)) {
          queue.current = [];
          if ((e as Error).message === "接管已过期") expire();
          else
            setError(
              timedOut
                ? "输入响应超时，请检查当前画面后再验证；操作不会自动重发"
                : (e as Error).message,
            );
        }
        return false;
      } finally {
        clearTimeout(timeout);
        if (inputController.current === controller) {
          inputController.current = null;
          draining.current = null;
          nextFrame.current = performance.now();
        }
      }
    })();
    draining.current = task;
    return task;
  }
  function input(command: Row) {
    if (!active.current || stopping.current || !ownerInput.current) return;
    lastInput.current = performance.now();
    const previous = queue.current.at(-1);
    if (command.kind === "text" && previous?.kind === "text")
      previous.text += command.text;
    else if (
      command.kind === "scroll" &&
      previous?.kind === "scroll" &&
      Math.sign(command.y) === Math.sign(previous.y) &&
      Math.abs(previous.y + command.y) <= 800
    )
      previous.y += command.y;
    else queue.current.push(command);
    if (inputTimer.current) clearTimeout(inputTimer.current);
    inputTimer.current = setTimeout(drain, command.kind === "text" ? 35 : 0);
  }
  async function close(verify = false, cancel = false) {
    if (stopping.current) return;
    if (cancel) {
      queue.current = [];
      inputController.current?.abort();
      composing.current = false;
      if (keyboard.current) keyboard.current.value = "";
      if (scrollTimer.current) clearTimeout(scrollTimer.current);
      scrollTimer.current = null;
      scrollDelta.current = 0;
    }
    if (composing.current && keyboard.current) {
      const text = keyboard.current.value;
      keyboard.current.value = "";
      composing.current = false;
      if (text) input({ kind: "text", text });
      input({ kind: "composition_end" });
    }
    // Flush the last debounced scroll before freezing new input.
    if (scrollTimer.current) {
      clearTimeout(scrollTimer.current);
      scrollTimer.current = null;
      input({
        kind: "scroll",
        y: Math.max(-800, Math.min(800, scrollDelta.current)),
      });
      scrollDelta.current = 0;
    }
    stopping.current = true;
    if (inputTimer.current) clearTimeout(inputTimer.current);
    frameController.current?.abort();
    setError("");
    verify ? setVerifying(true) : setClosing(true);
    try {
      if (!cancel && (draining.current || queue.current.length) && !(await drain())) {
        stopping.current = false;
        return;
      }
      if (!mounted.current) return;
      if (automaticConnection.current && takeover.current && (verify || cancel)) {
        const result = await api(`/agents/${aid}/connections/${takeover.current}/control`, {
          challenge_version: challengeVersion.current, action: cancel ? "cancel" : "resume",
        });
        if (cancel) { active.current = false; onClose(); }
        else {
          updateControl(result);
          stopping.current = false;
          await shot();
        }
        return;
      }
      let recovered = false;
      if (verify && (!automaticConnection.current || challenge)) {
        const currentChallenge = challenge || (await api(`/agents/${aid}/browser/challenge/status`, {
          platform, takeover: takeover.current,
        })).challenge;
        if (currentChallenge) {
          setChallenge(currentChallenge);
          const result = await api(`/agents/${aid}/browser/challenge/verify`, {
            platform, takeover: takeover.current, challenge_id: currentChallenge.id,
            start_managed_targets: managedStart,
          });
          recovered = !result.finish_login;
        } else {
          await api(`/agents/${aid}/accounts/verify-login`, { platform, account_id: accountId.current, start_managed_targets: managedStart });
        }
      }
      if (state === "opening") {
        await connection.current?.promise.catch(() => {});
      }
      if (!verify && takeover.current) await api(`/agents/${aid}/takeover/end?takeover=${encodeURIComponent(takeover.current)}`, {});
      if (verify && onVerified && !recovered) await onVerified(managedStart);
      else onClose();
    } catch (e) {
      stopping.current = false;
      setError((e as Error).message);
    } finally {
      setVerifying(false);
      setClosing(false);
    }
  }
  useEffect(() => {
    mounted.current = true;
    stopping.current = false;
    active.current = false;
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    let canceled = false;
    frameTag.current = "";
    preview.current = false;
    imageUrl.current = "";
    setImage(null);
    setFrameError("");
    setLatency(null);
    setState("opening");
    setError("");
    setChallenge(null);
    ownerInput.current = false;
    challengeVersion.current = 0;
    setControlMode("automatic");
    setOwnerAction("");
    const key = `${aid}:${platform}:${requestedAccountId || ""}:${newAccount}:${retry}`;
    if (connection.current?.key !== key) {
      const details = initialDetails.current;
      initialDetails.current = { site_url: details.site_url, login: undefined };
      connection.current = {
        key,
        promise: api(`/agents/${aid}/connect`, { platform, account_id: requestedAccountId, new_account: newAccount, ...details }),
      };
      onCredentialsSent?.();
    }
    connection.current.promise
      .then(async (session) => {
        if (canceled) return;
        takeover.current = session.takeover_id;
        setChallenge(session.challenge || null);
        accountId.current = session.account_id;
        automaticConnection.current = Boolean(session.automatic_connection);
        setAutomatic(Boolean(session.automatic_connection));
        updateControl(session);
        setManagedStart(Boolean(session.managed_target_start));
        lastInput.current = performance.now();
        active.current = true;
        setState("ready");
        await shot();
      })
      .catch((e) => {
        if (!canceled) {
          setError(e.message);
          setState("failed");
        }
      });
    const poll = setInterval(() => {
      if (
        !document.hidden &&
        !draining.current &&
        !queue.current.length &&
        performance.now() >= nextFrame.current
      )
        shot();
    }, 60);
    let checkingChallenge = false;
    const challengePoll = setInterval(async () => {
      if (
        canceled ||
        !active.current ||
        stopping.current ||
        checkingChallenge ||
        document.hidden
      )
        return;
      checkingChallenge = true;
      try {
        if (automaticConnection.current) {
          const connection = await api(`/agents/${aid}/connections/${takeover.current}`);
          if (canceled) return;
          if (!updateControl(connection)) return;
          if (connection.status === "connected" || connection.state === "connected") {
            active.current = false;
            if (onVerified) await onVerified(false);
            else onClose();
            return;
          }
          if (["expired", "ended", "canceled"].includes(connection.status || connection.state)) {
            expire();
            setError(connection.owner_action || "本次连接已结束，可重新连接。");
          }
          return;
        }
        const result = await api(`/agents/${aid}/browser/challenge/status`, {
          platform,
          takeover: takeover.current,
        });
        if (!canceled) setChallenge(result.challenge || null);
      } catch {
        // Frame/input errors expose expired or disconnected sessions.
      } finally {
        checkingChallenge = false;
      }
    }, 2000);
    return () => {
      initialDetails.current.login = undefined;
      canceled = true;
      mounted.current = false;
      active.current = false;
      clearInterval(poll);
      clearInterval(challengePoll);
      frameController.current?.abort();
      inputController.current?.abort();
      inputController.current = null;
      draining.current = null;
      if (inputTimer.current) clearTimeout(inputTimer.current);
      if (scrollTimer.current) clearTimeout(scrollTimer.current);
      scrollTimer.current = null;
      scrollDelta.current = 0;
      queue.current = [];
      if (imageUrl.current) URL.revokeObjectURL(imageUrl.current);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [aid, platform, requestedAccountId, newAccount, retry, shot]);
  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    function wheel(event: WheelEvent) {
      if (!ownerInput.current) return;
      event.preventDefault();
      scrollDelta.current += event.deltaY * (event.deltaMode === 1 ? 20 : 1);
      if (scrollTimer.current) return;
      scrollTimer.current = setTimeout(() => {
        scrollTimer.current = null;
        input({
          kind: "scroll",
          y: Math.max(-800, Math.min(800, scrollDelta.current)),
        });
        scrollDelta.current = 0;
      }, 24);
    }
    target.addEventListener("wheel", wheel, { passive: false });
    return () => target.removeEventListener("wheel", wheel);
  }, [state]); // Input is queued in the active session.
  return (
    <div
      className="account-browser-overlay"
      ref={root}
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-browser-title"
      onKeyDown={(e) => {
        if (
          e.key === "Escape" &&
          e.target !== keyboard.current &&
          !verifying &&
          !closing
        )
          close();
        if (e.key === "Tab") {
          const elements = root.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), textarea:not(:disabled)",
          );
          if (!elements?.length) return;
          const first = elements[0],
            last = elements[elements.length - 1];
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }}
    >
      <div className="account-browser-panel">
        <header className="account-browser-header">
          <div className="browser-title">
            <SocialIcon platform={platform} size={24} />
            <div>
              <h2 id="account-browser-title">{(automatic && controlMode === "owner") || challenge ? "完成验证" : "连接"} {name}</h2>
              <span className="muted small" role="status">
                {automatic ? controlMode === "owner" ? ownerAction || "请在原窗口完成本人步骤，再继续连接" : "平台正在自动连接；关闭窗口后仍会在后台核对" : challenge ? "请在原窗口完成真人验证，其他账号继续运行" : "在受控窗口中登录账号"}
              </span>
            </div>
          </div>
          <div className="browser-header-actions">
            {platform !== "lab" && (!automatic || controlMode === "owner") && (
              <button
                className="primary"
                disabled={state !== "ready" || verifying || closing || dependencyBusy}
                onClick={() => close(true)}
              >
                {verifying ? (
                  <LoaderCircle className="spin" size={15} />
                ) : (
                  <Check size={15} />
                )}
                {verifying ? "正在完成验证…" : automatic ? "本人步骤已完成，继续连接" : challenge ? "验证完成，继续任务" : managedStart ? "绑定并启动获客" : verifyLabel}
              </button>
            )}
            {automatic && <button className="text-button" disabled={state !== "ready" || verifying || closing}
              onClick={() => close(false, true)}>取消连接</button>}
            <button
              ref={closeButton}
              className="icon"
              aria-label="关闭账号浏览器"
              disabled={verifying || closing}
              onClick={() => close()}
            >
              <X size={20} />
            </button>
          </div>
        </header>
        {automatic && controlMode === "owner" && canReviewDependencies && takeover.current &&
          <ConnectionReadDependencies key={`${takeover.current}:${challengeVersion.current}`}
            aid={aid} connectionId={takeover.current} challengeVersion={challengeVersion.current}
            disabled={state !== "ready" || verifying || closing} onBusyChange={setDependencyBusy} />}
        <div className="browser-toolbar">
          <div>
            <button
              className="icon"
              aria-label="浏览器后退"
              disabled={state !== "ready" || verifying || closing || automatic && controlMode !== "owner"}
              onClick={() => input({ kind: "back" })}
            >
              <ArrowLeft size={16} />
            </button>
            <button
              className="icon"
              aria-label="浏览器前进"
              disabled={state !== "ready" || verifying || closing || automatic && controlMode !== "owner"}
              onClick={() => input({ kind: "forward" })}
            >
              <ArrowRight size={16} />
            </button>
            <button
              className="icon"
              aria-label="重新加载网页"
              disabled={state !== "ready" || verifying || closing || automatic && controlMode !== "owner"}
              onClick={() => input({ kind: "reload" })}
            >
              <RefreshCw size={15} />
            </button>
          </div>
          <span className="browser-address">
            <LockKeyhole size={12} />
            {name} · 受控登录窗口
          </span>
          <button
            className="text-button"
            disabled={state !== "ready" || verifying || closing}
            onClick={() => {
              setError("");
              shot();
            }}
          >
            刷新画面
          </button>
        </div>
        {(error || frameError) && (
          <div className="browser-error" role="alert">
            <span>{error || frameError}</span>
            <button
              onClick={() => {
                setError("");
                state === "failed" ? setRetry((n) => n + 1) : shot();
              }}
            >
              重试
            </button>
          </div>
        )}
        <div className="account-browser-screen" ref={viewport}>
          {image && state === "ready" ? (
            <img
              draggable={false}
              src={image.url}
              alt={`${name} 登录页面。点击输入框后可直接输入，支持滚轮滚动。`}
              onPointerDown={(e) => {
                e.preventDefault();
                if (!ownerInput.current) return;
                const rect = e.currentTarget.getBoundingClientRect();
                input({
                  kind: "click",
                  x:
                    ((e.clientX - rect.left) *
                      (image.width || e.currentTarget.naturalWidth)) /
                    rect.width,
                  y:
                    ((e.clientY - rect.top) *
                      (image.height || e.currentTarget.naturalHeight)) /
                    rect.height,
                });
                keyboard.current?.focus({ preventScroll: true });
              }}
            />
          ) : (
            <div className="browser-loading">
              <LoaderCircle
                className={state === "failed" ? "" : "spin"}
                size={24}
              />
              <strong>
                {state === "failed"
                  ? "浏览器未能打开"
                  : state === "opening"
                    ? "正在打开登录窗口"
                    : "正在连接实时画面"}
              </strong>
              <span className="muted small">
                {state === "failed"
                  ? "可重试，或关闭后重新连接"
                  : "首次启动可能需要几秒钟"}
              </span>
            </div>
          )}
          <textarea
            ref={keyboard}
            className="browser-keyboard"
            aria-label="浏览器键盘输入"
            disabled={state !== "ready" || verifying || closing || automatic && controlMode !== "owner"}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            onCompositionStart={() => {
              composing.current = true;
              if (automaticConnection.current) input({ kind: "composition_start" });
            }}
            onCompositionEnd={(e) => {
              composing.current = false;
              const text = e.currentTarget.value;
              e.currentTarget.value = "";
              if (text) input({ kind: "text", text });
              if (automaticConnection.current) input({ kind: "composition_end" });
            }}
            onChange={(e) => {
              if (!composing.current) {
                const text = e.target.value;
                e.target.value = "";
                if (text) input({ kind: "text", text });
              }
            }}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.nativeEvent.isComposing) return;
              if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
                e.preventDefault();
                input({ kind: "key", text: "ControlOrMeta+A" });
              } else if (
                [
                  "Enter",
                  "Tab",
                  "Backspace",
                  "Delete",
                  "Escape",
                  "ArrowUp",
                  "ArrowDown",
                  "ArrowLeft",
                  "ArrowRight",
                  "Home",
                  "End",
                ].includes(e.key)
              ) {
                if (e.key === "Tab" && e.shiftKey) return;
                e.preventDefault();
                input({ kind: "key", text: e.key });
              }
            }}
          />
        </div>
        <footer className="account-browser-footer">
          <span>
            <i
              className={`dot ${state === "ready" && image && !frameError ? "green" : ""}`}
            />
            {state === "ready"
              ? frameError
                ? "正在恢复画面"
                : image
                  ? "实时连接"
                  : "正在取帧"
              : state === "failed"
                ? "未连接"
                : "连接中"}
            {latency != null && state === "ready" && (
              <small>{latency} ms</small>
            )}
          </span>
          <span>{automatic && controlMode !== "owner" ? "自动连接中，画面仅供查看" : "点击画面即可输入 · 支持粘贴与滚轮"}</span>
          <span className="muted">{automatic ? controlMode === "owner" ? "本人步骤期间自动操作已暂停" : "其他账号继续运行" : "接管期间 Agent 已暂停"}</span>
        </footer>
      </div>
    </div>
  );
}
