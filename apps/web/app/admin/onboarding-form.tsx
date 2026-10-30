"use client";

import { useState } from "react";
import SetupFields, {
  initialProfile,
  SetupProfile,
  SetupTemplate,
} from "../setup-fields";
import { Row, clone } from "./config-editor";
import { socialNames } from "../social-icon";

export default function OnboardingForm({
  body,
  change,
}: {
  body: Row;
  change: (body: Row) => void;
}) {
  const [previewStep, setPreviewStep] = useState(0),
    [preview, setPreview] = useState<SetupProfile | null>(null);
  function field(key: string, name: string, value: any) {
    const next = clone(body);
    next.fields[key][name] = value;
    change(next);
    setPreview(null);
  }
  const template: SetupTemplate = {
    version: 0,
    body: body as SetupTemplate["body"],
  };
  const profile = preview || initialProfile(template);
  return (
    <>
      <div className="admin-subheading">
        <h3>步骤与默认渠道</h3>
      </div>
      <div className="admin-step-fields">
        {body.steps.map((step: string, i: number) => (
          <label key={i}>
            第 {i + 1} 步名称
            <input
              required
              maxLength={30}
              value={step}
              onChange={(e) =>
                change({
                  ...body,
                  steps: body.steps.map((v: string, n: number) =>
                    n === i ? e.target.value : v,
                  ),
                })
              }
            />
          </label>
        ))}
      </div>
      <label>
        默认运营渠道
        <select
          value={body.default_platform}
          onChange={(e) => {
            change({ ...body, default_platform: e.target.value });
            setPreview(null);
          }}
        >
          {Object.entries(socialNames).map(([id, name]) => (
            <option value={id} key={id}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <div className="admin-subheading">
        <h3>业务与运营字段</h3>
        <span className="muted small">产品名称、介绍和运营目标始终必填</span>
      </div>
      <div className="admin-plans">
        {Object.entries(body.fields).map(([key, raw]) => {
          const entry = raw as Row;
          const fixed = ["product_name", "description", "goal"].includes(key);
          return (
            <details key={key}>
              <summary>
                <span>{entry.label || key}</span>
                <span className="muted small">
                  {entry.required ? "必填" : "选填"}
                </span>
              </summary>
              <div className="admin-plan-fields">
                <div className="admin-field-pair">
                  <label>
                    字段名称
                    <input
                      required
                      maxLength={80}
                      value={entry.label}
                      onChange={(e) => field(key, "label", e.target.value)}
                    />
                  </label>
                  <label>
                    输入示例
                    <input
                      maxLength={400}
                      value={entry.placeholder}
                      onChange={(e) =>
                        field(key, "placeholder", e.target.value)
                      }
                    />
                  </label>
                </div>
                <label>
                  补充说明
                  <input
                    maxLength={400}
                    value={entry.help}
                    onChange={(e) => field(key, "help", e.target.value)}
                  />
                </label>
                <label>
                  默认值
                  <textarea
                    rows={2}
                    maxLength={
                      key === "product_name"
                        ? 80
                        : key === "description"
                          ? 10000
                          : 2000
                    }
                    value={entry.default}
                    onChange={(e) => field(key, "default", e.target.value)}
                  />
                </label>
                <label className="admin-checkbox">
                  <input
                    type="checkbox"
                    checked={entry.required}
                    disabled={fixed}
                    onChange={(e) => field(key, "required", e.target.checked)}
                  />
                  用户必须填写此项
                </label>
              </div>
            </details>
          );
        })}
      </div>
      <div className="admin-subheading">
        <h3>运营策略选项</h3>
        <button
          type="button"
          disabled={body.strategies.length >= 8}
          onClick={() =>
            change({
              ...body,
              strategies: [
                ...body.strategies,
                { name: "新策略", description: "说明适用的运营方式" },
              ],
            })
          }
        >
          添加选项
        </button>
      </div>
      {body.strategies.map((strategy: Row, i: number) => (
        <div className="admin-option-row" key={i}>
          <label>
            选项名称
            <input
              required
              maxLength={60}
              value={strategy.name}
              onChange={(e) => {
                const next = clone(body);
                next.strategies[i].name = e.target.value;
                change(next);
                setPreview(null);
              }}
            />
          </label>
          <label>
            选项说明
            <input
              required
              maxLength={300}
              value={strategy.description}
              onChange={(e) => {
                const next = clone(body);
                next.strategies[i].description = e.target.value;
                change(next);
                setPreview(null);
              }}
            />
          </label>
          <button
            type="button"
            disabled={body.strategies.length === 1}
            onClick={() => {
              change({
                ...body,
                strategies: body.strategies.filter(
                  (_: Row, n: number) => n !== i,
                ),
              });
              setPreview(null);
            }}
          >
            移除
          </button>
        </div>
      ))}
      <details className="admin-preview" open>
        <summary>
          创建流程预览{" "}
          <span className="muted small">当前编辑内容，不会创建 Agent</span>
        </summary>
        <div className="admin-preview-nav">
          {body.steps.map((step: string, i: number) => (
            <button
              type="button"
              aria-pressed={previewStep === i}
              key={i}
              onClick={() => setPreviewStep(i)}
            >
              {i + 1} · {step}
            </button>
          ))}
        </div>
        <div className="setup-form admin-preview-form">
          {previewStep < 2 ? (
            <SetupFields
              validate={false}
              step={previewStep}
              template={template}
              profile={profile}
              onChange={(key, value) =>
                setPreview({ ...profile, [key]: value })
              }
            />
          ) : (
            <div className="admin-preview-channel">
              <strong>{profile.product_name || "产品名称"}</strong>
              <p>{profile.goal || "运营目标"}</p>
              <p>
                连接 {socialNames[profile.platform]}，验证账号后进入工作台。
              </p>
            </div>
          )}
        </div>
      </details>
    </>
  );
}
