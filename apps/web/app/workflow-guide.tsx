"use client";

import { useState } from "react";
import { Workflow } from "lucide-react";
import DialogPanel from "./dialog-panel";

export default function WorkflowGuide() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        <Workflow size={16} aria-hidden="true" />
        运营流程图
      </button>
      {open && (
        <DialogPanel
          title="运营流程图"
          size="wide"
          onClose={() => setOpen(false)}
        >
          <iframe
            className="workflow-guide-frame"
            title="自主运营流程与并行案例"
            src="/workflow-guide.html"
            onLoad={(event) => {
              const page = event.currentTarget.contentDocument;
              if (!page) return;
              page.documentElement.classList.add("embedded");
              page.addEventListener("keydown", (key) => {
                if (key.key === "Escape") {
                  key.preventDefault();
                  setOpen(false);
                }
              });
            }}
            tabIndex={0}
          />
        </DialogPanel>
      )}
    </>
  );
}
