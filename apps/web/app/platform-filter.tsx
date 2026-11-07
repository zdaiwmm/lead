"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Bot, Check, ChevronDown, Layers } from "lucide-react";
import SocialIcon, { socialNames } from "./social-icon";
import styles from "./operating-plans.module.css";

const channels = [
  ["", "全部平台"],
  ...Object.entries(socialNames),
  ["system", "Agent 系统"],
];
function Icon({ channel }: { channel: string }) {
  if (!channel) return <Layers size={15} aria-hidden="true" />;
  if (channel === "system") return <Bot size={15} aria-hidden="true" />;
  return <SocialIcon platform={channel} size={15} />;
}

export default function PlatformFilter({
  value,
  onChange,
  options = channels,
  title = "按渠道筛选",
  icons = true,
}: {
  options?: string[][];
  title?: string;
  icons?: boolean;
  value: string;
  onChange: (channel: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const label = options.find(([key]) => key === value)?.[1] || title;
  useEffect(() => {
    if (!open) return;
    root.current
      ?.querySelector<HTMLButtonElement>('[aria-selected="true"]')
      ?.focus({ preventScroll: true });
    function dismiss(e: PointerEvent) {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  function close() {
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  }
  return (
    <div
      className={styles.filter}
      ref={root}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
    >
      <button
        ref={trigger}
        aria-label={`${title}：${label}`}
        aria-haspopup="listbox"
        aria-controls={open ? id : undefined}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => {
          if (["ArrowDown", "ArrowUp"].includes(e.key)) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {icons && <Icon channel={value} />}
        {label}
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open && (
        <div
          id={id}
          role="listbox"
          aria-label={title}
          className={styles.filterMenu}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              close();
              return;
            }
            const options = Array.from(
              e.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
            );
            const at = options.indexOf(
              document.activeElement as HTMLButtonElement,
            );
            const next = {
              ArrowDown: (at + 1) % options.length,
              ArrowUp: (at - 1 + options.length) % options.length,
              Home: 0,
              End: options.length - 1,
            }[e.key];
            if (next != null) {
              e.preventDefault();
              options[next].focus();
            }
          }}
        >
          {options.map(([key, name]) => (
            <button
              key={key}
              role="option"
              aria-selected={key === value}
              tabIndex={-1}
              onClick={() => {
                onChange(key);
                close();
              }}
            >
              {icons && <Icon channel={key} />}
              {name}
              {key === value && <Check size={14} aria-hidden="true" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
