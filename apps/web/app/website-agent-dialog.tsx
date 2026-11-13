"use client";

import { useEffect, useRef, useState } from "react";
import { api, getWorkspace } from "./api-client";
import DialogPanel from "./dialog-panel";
import LegacySetup from "./new-agent-dialog";
import BriefReview, { emptyBrief, type Brief } from "./brief-review";

type Props = {
  platforms: Record<string, any>[];
  onClose: () => void;
  onCreated: (id: string) => Promise<void>;
};
type FormState = { brief: Brief; clientId: string };
export default function WebsiteAgentDialog(props: Props) {
  const legacyKey = "lead.agent-setup." + (getWorkspace() || "local");
  const draftKey = legacyKey + ".website.v1";
  const [resumeLegacy, setResumeLegacy] = useState(false);
  const [legacy] = useState(() => {
    try {
      return Boolean(localStorage.getItem(legacyKey));
    } catch {
      return false;
    }
  });
  const [state, setState] = useState<FormState>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(draftKey) || "null");
      if (saved?.clientId)
        return {
          clientId: saved.clientId,
          brief: { ...emptyBrief(saved.website), ...saved.brief },
        };
      const previous = JSON.parse(
        localStorage.getItem(legacyKey + ".form") || "null",
      );
      return {
        clientId: crypto.randomUUID(),
        brief: emptyBrief(previous?.profile?.product_url),
      };
    } catch {
      return { clientId: crypto.randomUUID(), brief: emptyBrief() };
    }
  });
  const [phase, setPhase] = useState<"edit" | "generate" | "create">("edit");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const analysis = useRef<AbortController | null>(null);
  const creating = useRef(false);
  const { brief } = state;
  useEffect(() => () => analysis.current?.abort(), []);
  function edit(patch: Partial<Brief>) {
    const next = { ...state, brief: { ...brief, ...patch } };
    setState(next);
    try {
      localStorage.setItem(draftKey, JSON.stringify(next));
    } catch {}
  }
  function cancel() {
    analysis.current?.abort();
    analysis.current = null;
    setPhase("edit");
    setNotice("已取消生成，可以继续填写。");
  }
  async function generate() {
    if (creating.current || analysis.current || !brief.product_url.trim())
      return;
    const controller = new AbortController();
    analysis.current = controller;
    setPhase("generate");
    setNotice("");
    setError("");
    try {
      const requestId = crypto.randomUUID();
      let draft: Brief = await api(
        "/agents/setup/analyze",
        { product_url: brief.product_url.trim(), request_id: requestId },
        controller.signal,
      );
      while ((draft as any).executor_pending && !controller.signal.aborted) {
        await new Promise<void>((resolve, reject) => {
          const canceled = () => { clearTimeout(timer); reject(controller.signal.reason); };
          const timer = setTimeout(() => { controller.signal.removeEventListener("abort", canceled); resolve(); }, 2000);
          controller.signal.addEventListener("abort", canceled, { once: true });
          if (controller.signal.aborted) canceled();
        });
        draft = await api("/agents/setup/analyze", { product_url: brief.product_url.trim(), request_id: requestId }, controller.signal);
      }
      if (controller.signal.aborted) return;
      edit({
        product_url: draft.product_url,
        product_name: brief.product_name.trim() || draft.product_name,
        introduction: brief.introduction.trim() || draft.introduction,
        features: brief.features.some((s) => s.trim())
          ? brief.features
          : draft.features,
        customers: brief.customers.some((s) => s.trim())
          ? brief.customers
          : draft.customers,
        scenarios: brief.scenarios.some((s) => s.trim())
          ? brief.scenarios
          : draft.scenarios,
        advantages: brief.advantages.some((item) => item.text.trim())
          ? brief.advantages
          : draft.advantages,
        review_notice: draft.review_notice,
      });
      setNotice("生成完成，可修改后创建。");
    } catch (err) {
      if (!controller.signal.aborted) setError((err as Error).message);
    } finally {
      if (analysis.current === controller) {
        analysis.current = null;
        setPhase("edit");
      }
    }
  }
  async function create() {
    if (creating.current || analysis.current) return;
    creating.current = true;
    setPhase("create");
    setError("");
    try {
      const clean = (rows: string[]) =>
        rows.map((s) => s.trim()).filter(Boolean);
      const fields = {
        features: clean(brief.features),
        customers: clean(brief.customers),
        scenarios: clean(brief.scenarios),
        advantages: brief.advantages.filter((item) => item.text.trim()),
      };
      for (const rows of [
        fields.features,
        fields.customers,
        fields.scenarios,
        fields.advantages.map((item) => item.text),
      ]) {
        if (rows.length > 20 || rows.some((s) => s.length > 2000))
          throw Error("每类最多 20 条，每条最多 2000 字，请精简后创建。");
      }
      const agent = await api("/agents/setup/confirm", {
        ...brief,
        ...fields,
        client_id: state.clientId,
      });
      try {
        localStorage.removeItem(draftKey);
      } catch {}
      await props.onCreated(agent.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      creating.current = false;
      setPhase("edit");
    }
  }
  if (resumeLegacy) return <LegacySetup {...props} />;
  return (
    <DialogPanel
      title="新建 Agent"
      description="所有资料均为选填。填写官网后，可自动补全空白项。"
      onClose={() => {
        if (!creating.current) {
          analysis.current?.abort();
          props.onClose();
        }
      }}
      footer={
        <>
          <button
            disabled={phase === "create"}
            onClick={() => {
              if (phase === "generate") cancel();
              else props.onClose();
            }}
          >
            {phase === "generate" ? "取消生成，手动填写" : "关闭"}
          </button>
          <button
            className="primary"
            type="button"
            onClick={create}
            disabled={phase !== "edit"}
          >
            {phase === "create" ? "正在创建…" : "确认并创建 Agent"}
          </button>
        </>
      }
    >
      {error && (
        <p role="alert" className="inline-feedback error-feedback">
          {error}
        </p>
      )}
      <form className="setup-form" onSubmit={(e) => e.preventDefault()}>
        <fieldset className="setup-fields" disabled={phase !== "edit"}>
          <div>
            <label>
              官网网址
              <input
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void generate();
                  }
                }}
                inputMode="url"
                autoComplete="url"
                maxLength={2000}
                placeholder="例如 example.com"
                value={brief.product_url}
                onChange={(e) => edit({ product_url: e.target.value })}
              />
            </label>
            <button
              className="text-button"
              type="button"
              disabled={!brief.product_url.trim()}
              onClick={generate}
            >
              根据官网生成
            </button>
            <span className="muted small" role="status">
              {phase === "generate" ? "正在阅读官网和产品资料…" : notice}
            </span>
          </div>
          <BriefReview brief={brief} edit={edit} />
          {legacy && (
            <button type="button" onClick={() => setResumeLegacy(true)}>
              继续上次的渠道连接
            </button>
          )}
        </fieldset>
      </form>
    </DialogPanel>
  );
}
