"use client";
import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
export default function DialogPanel({
  title,
  description,
  onClose,
  children,
  footer,
  size = "default",
}: {
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "default" | "compact" | "wide";
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId(),
    descriptionId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [error, setError] = useState("");
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (!element.open) element.showModal();
    element
      .querySelector<HTMLElement>("[data-dialog-heading]")
      ?.focus({ preventScroll: true });
    function closeAll() {
      closeRef.current();
    }
    function report(e: Event) {
      setError(String((e as CustomEvent).detail || ""));
    }
    window.addEventListener("lead:close-dialogs", closeAll);
    element.addEventListener("lead:dialog-error", report);
    return () => {
      window.removeEventListener("lead:close-dialogs", closeAll);
      element.removeEventListener("lead:dialog-error", report);
      if (element.open) element.close();
      document.body.style.overflow = overflow;
      previous?.focus({ preventScroll: true });
    };
  }, []);
  return createPortal(
    <dialog
      ref={dialog}
      className={`dialog-panel dialog-size-${size}`}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          return;
        }
        if (e.key !== "Tab") return;
        e.stopPropagation();
        const targets = Array.from(
          e.currentTarget.querySelectorAll<HTMLElement>(
            'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable="true"]',
          ),
        ).filter(
          (node) =>
            node.tabIndex >= 0 &&
            !node.matches(":disabled, [hidden], [inert]") &&
            node.getClientRects().length > 0,
        );
        const first = targets[0],
          last = targets[targets.length - 1],
          active = document.activeElement;
        if (!first) {
          e.preventDefault();
          e.currentTarget
            .querySelector<HTMLElement>("[data-dialog-heading]")
            ?.focus({ preventScroll: true });
        } else if (
          e.shiftKey &&
          (active === first || !targets.includes(active as HTMLElement))
        ) {
          e.preventDefault();
          last.focus();
        } else if (
          !e.shiftKey &&
          (active === last || !e.currentTarget.contains(active))
        ) {
          e.preventDefault();
          first.focus();
        }
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target !== e.currentTarget) return;
        const r = e.currentTarget.getBoundingClientRect();
        if (
          e.clientX < r.left ||
          e.clientX > r.right ||
          e.clientY < r.top ||
          e.clientY > r.bottom
        )
          onClose();
      }}
    >
      <header className="dialog-heading">
        <div>
          <h2 id={titleId} data-dialog-heading tabIndex={-1}>
            {title}
          </h2>
          {description && <p id={descriptionId}>{description}</p>}
        </div>
        <button className="icon" aria-label={`关闭${title}`} onClick={onClose}>
          <X size={18} />
        </button>
      </header>
      <div className="dialog-body" tabIndex={0} aria-label={`${title}内容`}>
        {error && (
          <p className="inline-feedback error-feedback" role="alert">
            {error}
          </p>
        )}
        {children}
      </div>
      {footer && <footer className="dialog-footer">{footer}</footer>}
    </dialog>,
    document.body,
  );
}
