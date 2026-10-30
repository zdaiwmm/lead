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
import { api, workspaceHeaders } from "./api-client";
type Row = Record<string, any>;
export default function AccountBrowser({
  aid,
  platform,
  name,
  onClose,
  onVerified,
  verifyLabel = "验证并完成",
}: {
  aid: string;
  platform: string;
  name: string;
  onClose: () => void;
  onVerified?: () => Promise<void>;
  verifyLabel?: string;
}) {
  const [state, setState] = useState("opening");
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [closing, setClosing] = useState(false);
  const [retry, setRetry] = useState(0);
  const [latency, setLatency] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const keyboard = useRef<HTMLTextAreaElement>(null);
  const imageUrl = useRef("");
  const mounted = useRef(false);
  const frameBusy = useRef(false);
  const frameController = useRef<AbortController | null>(null);
  const queue = useRef<Row[]>([]);
  const draining = useRef(false);
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
  const shot = useCallback(async () => {
    if (
      !mounted.current ||
      !active.current ||
      frameBusy.current ||
      stopping.current
    )
      return;
    frameBusy.current = true;
    const controller = new AbortController();
    frameController.current = controller;
    try {
      const started = performance.now();
      const response = await fetch(
        `/api/agents/${aid}/browser/screenshot?format=jpeg`,
        {
          headers: workspaceHeaders(),
          cache: "no-store",
          signal: controller.signal,
        },
      );
      if (!response.ok) throw Error("画面连接中断，请刷新重试");
      const blob = await response.blob();
      if (!mounted.current || stopping.current) return;
      const url = URL.createObjectURL(blob);
      const previous = imageUrl.current;
      imageUrl.current = url;
      setImage(url);
      setLatency(Math.round(performance.now() - started));
      if (previous) URL.revokeObjectURL(previous);
    } catch (e) {
      if (mounted.current && !controller.signal.aborted)
        setError((e as Error).message);
    } finally {
      frameBusy.current = false;
    }
  }, [aid]);
  async function drain() {
    if (draining.current || !active.current || stopping.current) return;
    draining.current = true;
    try {
      while (queue.current.length && mounted.current && !stopping.current) {
        const command = queue.current.shift()!;
        await api(`/agents/${aid}/browser/input`, command);
      }
      await shot();
    } catch (e) {
      queue.current = [];
      if (mounted.current) setError((e as Error).message);
    } finally {
      draining.current = false;
      if (queue.current.length && mounted.current && !stopping.current)
        inputTimer.current = setTimeout(drain, 0);
    }
  }
  function input(command: Row) {
    if (!active.current || stopping.current) return;
    const previous = queue.current.at(-1);
    if (command.kind === "text" && previous?.kind === "text")
      previous.text += command.text;
    else queue.current.push(command);
    if (inputTimer.current) clearTimeout(inputTimer.current);
    inputTimer.current = setTimeout(drain, command.kind === "text" ? 35 : 0);
  }
  async function close(verify = false) {
    if (draining.current || queue.current.length) {
      setError("正在完成输入，请稍后再试");
      return;
    }
    stopping.current = true;
    setError("");
    verify ? setVerifying(true) : setClosing(true);
    try {
      if (verify)
        await api(`/agents/${aid}/accounts/verify-login`, { platform });
      if (state === "opening") {
        await connection.current?.promise.catch(() => {});
      }
      await api(`/agents/${aid}/takeover/end`, {});
      if (verify && onVerified) await onVerified();
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
    setState("opening");
    setError("");
    const key = `${aid}:${platform}:${retry}`;
    if (connection.current?.key !== key)
      connection.current = {
        key,
        promise: api(`/agents/${aid}/connect`, { platform }),
      };
    connection.current.promise
      .then(async () => {
        if (canceled) return;
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
      if (!document.hidden && !draining.current && !queue.current.length)
        shot();
    }, 800);
    return () => {
      canceled = true;
      mounted.current = false;
      active.current = false;
      clearInterval(poll);
      frameController.current?.abort();
      if (inputTimer.current) clearTimeout(inputTimer.current);
      if (scrollTimer.current) clearTimeout(scrollTimer.current);
      if (imageUrl.current) URL.revokeObjectURL(imageUrl.current);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [aid, platform, retry, shot]);
  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    function wheel(event: WheelEvent) {
      event.preventDefault();
      scrollDelta.current += event.deltaY * (event.deltaMode === 1 ? 20 : 1);
      if (scrollTimer.current) clearTimeout(scrollTimer.current);
      scrollTimer.current = setTimeout(() => {
        input({
          kind: "scroll",
          y: Math.max(-800, Math.min(800, scrollDelta.current)),
        });
        scrollDelta.current = 0;
      }, 70);
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
              <h2 id="account-browser-title">连接 {name}</h2>
              <span className="muted small">在专属浏览器中登录账号</span>
            </div>
          </div>
          <div className="browser-header-actions">
            {platform !== "lab" && (
              <button
                className="primary"
                disabled={state !== "ready" || verifying || closing}
                onClick={() => close(true)}
              >
                {verifying ? (
                  <LoaderCircle className="spin" size={15} />
                ) : (
                  <Check size={15} />
                )}
                {verifyLabel}
              </button>
            )}
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
        <div className="browser-toolbar">
          <div>
            <button
              className="icon"
              aria-label="浏览器后退"
              disabled={state !== "ready"}
              onClick={() => input({ kind: "back" })}
            >
              <ArrowLeft size={16} />
            </button>
            <button
              className="icon"
              aria-label="浏览器前进"
              disabled={state !== "ready"}
              onClick={() => input({ kind: "forward" })}
            >
              <ArrowRight size={16} />
            </button>
            <button
              className="icon"
              aria-label="重新加载网页"
              disabled={state !== "ready"}
              onClick={() => input({ kind: "reload" })}
            >
              <RefreshCw size={15} />
            </button>
          </div>
          <span className="browser-address">
            <LockKeyhole size={12} />
            {name} · 独立登录环境
          </span>
          <button
            className="text-button"
            disabled={state !== "ready"}
            onClick={() => {
              setError("");
              shot();
            }}
          >
            刷新画面
          </button>
        </div>
        {error && (
          <div className="browser-error" role="alert">
            <span>{error}</span>
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
              src={image}
              alt={`${name} 登录页面。点击输入框后可直接输入，支持滚轮滚动。`}
              onPointerDown={(e) => {
                e.preventDefault();
                const rect = e.currentTarget.getBoundingClientRect();
                input({
                  kind: "click",
                  x:
                    ((e.clientX - rect.left) * e.currentTarget.naturalWidth) /
                    rect.width,
                  y:
                    ((e.clientY - rect.top) * e.currentTarget.naturalHeight) /
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
                    ? "正在打开专属浏览器"
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
            disabled={state !== "ready"}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={(e) => {
              composing.current = false;
              const text = e.currentTarget.value;
              e.currentTarget.value = "";
              if (text) input({ kind: "text", text });
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
            <i className={`dot ${state === "ready" ? "green" : ""}`} />
            {state === "ready"
              ? "实时连接"
              : state === "failed"
                ? "未连接"
                : "连接中"}
            {latency != null && state === "ready" && (
              <small>{latency} ms</small>
            )}
          </span>
          <span>点击画面即可输入 · 支持粘贴与滚轮</span>
          <span className="muted">接管期间 Agent 已暂停</span>
        </footer>
      </div>
    </div>
  );
}
