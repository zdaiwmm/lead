"use client";

import { Row, clone } from "./config-editor";

export function Lines({
  label,
  value,
  onChange,
  help,
}: {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
  help?: string;
}) {
  return (
    <label>
      {label}
      <textarea
        rows={3}
        value={value.join("\n")}
        onChange={(e) => onChange(e.target.value.split("\n"))}
        onBlur={() => onChange(value.map((v) => v.trim()).filter(Boolean))}
      />
      {help && <small>{help}</small>}
    </label>
  );
}

export default function StrategyForm({
  body,
  change,
}: {
  body: Row;
  change: (body: Row) => void;
}) {
  function field(key: string, value: any) {
    change({ ...body, [key]: value });
  }
  function planField(index: number, key: string, value: any) {
    const next = clone(body);
    next.plans[index][key] = value;
    change(next);
  }
  return (
    <>
      <div className="admin-field-pair">
        <label>
          策略名称
          <input
            required
            maxLength={100}
            value={body.name}
            onChange={(e) => field("name", e.target.value)}
          />
        </label>
        <label>
          效果观察周期（天）
          <input
            type="number"
            min={1}
            max={90}
            required
            value={body.maturity_days}
            onChange={(e) => field("maturity_days", Number(e.target.value))}
          />
        </label>
      </div>
      <label>
        适用目标
        <input
          maxLength={500}
          value={body.target}
          onChange={(e) => field("target", e.target.value)}
        />
      </label>
      <label>
        运营节奏
        <textarea
          required
          rows={2}
          maxLength={2000}
          value={body.cadence}
          onChange={(e) => field("cadence", e.target.value)}
        />
        <small>用于指导规划，实际发布仍受账号授权和频率上限约束。</small>
      </label>
      <label>
        转化路径
        <textarea
          rows={2}
          maxLength={4000}
          value={body.conversion_path}
          onChange={(e) => field("conversion_path", e.target.value)}
        />
      </label>
      <Lines
        label="准备步骤"
        value={body.setup}
        onChange={(v) => field("setup", v)}
        help="每行一项。"
      />
      <Lines
        label="运营边界"
        value={body.constraints}
        onChange={(v) => field("constraints", v)}
      />
      <div className="admin-subheading">
        <h3>内容方案</h3>
        <button
          type="button"
          disabled={body.plans.length >= 12}
          onClick={() =>
            field("plans", [
              ...body.plans,
              {
                id: `${body.platform}.plan_${Date.now()}`,
                name: "新方案",
                formats: ["text"],
                audience: "",
                hypothesis: "",
                assets: "",
                workflow: [],
                first_batch: [],
                template: "",
                cta: "",
                destination_path: "",
                experiment: "",
                iterate: "",
              },
            ])
          }
        >
          添加方案
        </button>
      </div>
      <div className="admin-plans">
        {body.plans.map((plan: Row, index: number) => (
          <details key={index} open={index === 0 ? true : undefined}>
            <summary>
              <span>
                {String(index + 1).padStart(2, "0")}{" "}
                <strong>{plan.name || "未命名方案"}</strong>
              </span>
              <span className="muted small">
                {plan.formats
                  .map(
                    (f: string) =>
                      ({ text: "文字", image: "图片", video: "视频" })[f],
                  )
                  .join(" / ")}
              </span>
            </summary>
            <div className="admin-plan-fields">
              <div className="admin-field-pair">
                <label>
                  方案名称
                  <input
                    required
                    maxLength={100}
                    value={plan.name}
                    onChange={(e) => planField(index, "name", e.target.value)}
                  />
                </label>
                <label>
                  方案标识
                  <input
                    required
                    pattern="[a-z0-9_.-]+"
                    maxLength={120}
                    value={plan.id}
                    onChange={(e) => planField(index, "id", e.target.value)}
                  />
                  <small>修改标识后，效果记录将按新方案累计。</small>
                </label>
              </div>
              <fieldset className="admin-format-options">
                <legend>内容格式</legend>
                {[
                  ["text", "文字"],
                  ["image", "图片"],
                  ["video", "视频"],
                ].map(([value, label]) => (
                  <label key={value}>
                    <input
                      type="checkbox"
                      checked={plan.formats.includes(value)}
                      onChange={(e) =>
                        planField(
                          index,
                          "formats",
                          e.target.checked
                            ? [...plan.formats, value]
                            : plan.formats.filter((f: string) => f !== value),
                        )
                      }
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
              {[
                ["audience", "适用人群"],
                ["hypothesis", "待验证假设"],
                ["assets", "素材要求"],
                ["template", "内容模板"],
                ["cta", "行动引导"],
                ["experiment", "尝试方式"],
                ["iterate", "复盘与调整方法"],
              ].map(([key, label]) => (
                <label key={key}>
                  {label}
                  <textarea
                    rows={key === "template" ? 4 : 2}
                    maxLength={
                      key === "template"
                        ? 10000
                        : key === "cta"
                          ? 1000
                          : key === "audience"
                            ? 2000
                            : 4000
                    }
                    value={plan[key]}
                    onChange={(e) => planField(index, key, e.target.value)}
                  />
                </label>
              ))}
              <label>
                产品承接页面路径
                <input
                  maxLength={1000}
                  placeholder="例如 /translate，留空则使用默认产品入口"
                  value={plan.destination_path || ""}
                    onChange={(e) =>
                      planField(index, "destination_path", e.target.value)
                    }
                />
                <small>填写当前产品网站内的路径，不能跳转到其他域名。</small>
              </label>
              <Lines
                label="执行步骤"
                value={plan.workflow}
                onChange={(v) => planField(index, "workflow", v)}
              />
              <Lines
                label="首批选题"
                value={plan.first_batch}
                onChange={(v) => planField(index, "first_batch", v)}
              />
              <button
                type="button"
                className="text-button"
                disabled={body.plans.length === 1}
                onClick={() =>
                  field(
                    "plans",
                    body.plans.filter((_: Row, i: number) => i !== index),
                  )
                }
              >
                移除此方案
              </button>
            </div>
          </details>
        ))}
      </div>
      <div className="admin-subheading">
        <h3>运营经验与依据</h3>
      </div>
      <label>
        已沉淀经验
        <textarea
          rows={4}
          maxLength={10000}
          value={body.experience_notes || ""}
          onChange={(e) => field("experience_notes", e.target.value)}
        />
        <small>
          记录适用业务、观察结果和限制。数据不足的结论请标为待验证。
        </small>
      </label>
      <Lines
        label="经验依据"
        value={body.evidence_refs || []}
        onChange={(v) => field("evidence_refs", v)}
        help="填写相关发布链接、工作记录或资料来源，每行一项。"
      />
      <Lines
        label="参考资料"
        value={body.sources}
        onChange={(v) => field("sources", v)}
      />
    </>
  );
}
