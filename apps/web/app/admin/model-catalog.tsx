"use client";

import { useState } from "react";
import { api } from "../api-client";
import ConfigEditor, { Row } from "./config-editor";

function CatalogFields({
  body,
  change,
  available,
  readOnly,
}: {
  body: Row;
  change: (value: Row) => void;
  available: string[];
  readOnly: boolean;
}) {
  const [name, setName] = useState("");
  const choices = [
    ...new Set([...body.openai_models, ...available]),
  ].sort() as string[];
  function toggle(model: string, enabled: boolean) {
    change({
      ...body,
      openai_models: enabled
        ? [...body.openai_models, model]
        : body.openai_models.filter((value: string) => value !== model),
    });
  }
  return (
    <>
      <p className="admin-help">
        全部 OpenAI
        模型共用当前服务端凭据。这里维护额外候选，部署端默认模型仍会保留；移除使用中的模型前需先调整对应的运行配置。
      </p>
      <fieldset className="admin-choice-group admin-model-candidates">
        <legend>额外可选的 OpenAI 模型</legend>
        {choices.map((model) => (
          <label className="admin-checkbox" key={model}>
            <input
              type="checkbox"
              checked={body.openai_models.includes(model)}
              onChange={(e) => toggle(model, e.target.checked)}
            />
            {model}
          </label>
        ))}
      </fieldset>
      {!choices.length && (
        <p className="admin-empty">
          点击上方“读取账户模型”，选择需要提供给 Agent 的模型。
        </p>
      )}
      {!readOnly && (
        <div className="admin-field-pair">
          <label>
            手动补充模型名称
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              placeholder="例如 gpt-6-luna"
            />
          </label>
          <button
            type="button"
            disabled={
              !/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(name) ||
              body.openai_models.includes(name) ||
              body.openai_models.length >= 30
            }
            onClick={() => {
              toggle(name, true);
              setName("");
            }}
          >
            添加候选模型
          </button>
        </div>
      )}
    </>
  );
}

export default function ModelCatalog({ readOnly }: { readOnly: boolean }) {
  const [available, setAvailable] = useState<string[]>([]),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  return (
    <>
      <section className="admin-service-status">
        <div>
          <strong>一份 OpenAI 凭据，多个候选模型</strong>
          <p>
            先维护工作室可选模型，再在“模型与工具”中分别选择对话、规划和研究模型。
          </p>
        </div>
        {!readOnly && (
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const result = await api("/admin/openai-models");
                setAvailable(result.models);
                setMessage(result.message);
              } catch (e) {
                setMessage((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "正在读取…" : "读取账户模型"}
          </button>
        )}
        {message && <p role="status">{message}</p>}
      </section>
      <ConfigEditor
        configKey="model-catalog/default"
        title="工作室 OpenAI 模型列表"
        description="发布后即可在模型选择器中使用，共用服务端密钥，保留草稿、版本和回滚。"
        readOnly={readOnly}
      >
        {(body, change) => (
          <CatalogFields
            body={body}
            change={change}
            available={available}
            readOnly={readOnly}
          />
        )}
      </ConfigEditor>
    </>
  );
}
