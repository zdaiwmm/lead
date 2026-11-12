import { Row } from "./config-editor";

const operations: Record<string, [string, string][]> = {
  tavily: [
    ["web.search", "公开搜索"],
    ["web.extract", "正文提取"],
  ],
  tikhub: [
    ["social.search", "Reddit 搜索"],
    ["social.item", "Reddit 帖子"],
    ["social.comments", "Reddit 评论"],
  ],
};
const reasons: Row = {
  missing_key: "服务端密钥未配置",
  not_enabled: "尚未启用",
  eligibility_unverified: "资格或额度待核验",
  credential_scope_conflict: "同一密钥被重复分配给多个工作室，请核对服务端归属",
  payment_not_authorized: "端点不支持已核验试用，需付费授权",
  project_disabled: "项目已停用",
};

export default function ResearchProviders({
  value,
  change,
  status,
}: {
  value: Row;
  change: (value: Row) => void;
  status: Row;
}) {
  return (
    <fieldset className="admin-choice-group">
      <legend>研究工具与费用</legend>
      <p className="admin-help">
        在工作室默认设置中登记已核验的连接额度；项目设置只能收紧上限。密钥由服务端管理。未配置或额度不足只影响相应工具，其他研究继续。未知账单保留预留，不自动重试或充值。
      </p>
      {Object.entries(operations).map(([provider, items]) => {
        const current = value[provider] || {
          enabled: false,
          starts_at: null,
          ends_at: null,
          evidence_ref: "",
          paid_authorization_ref: "",
          limit_units: 0,
          project_limit_units: 0,
          question_limit_units: 0,
          operations: {},
        };
        const scale = provider === "tavily" ? 1000 : 1000000;
        const unit = provider === "tavily" ? "credits" : "美元";
        const set = (key: string, next: any) =>
          change({ ...value, [provider]: { ...current, [key]: next } });
        return (
          <section key={provider}>
            <label className="admin-checkbox">
              <input
                type="checkbox"
                checked={current.enabled}
                onChange={(e) => set("enabled", e.target.checked)}
              />
              {provider === "tavily" ? "Tavily" : "TikHub"}
            </label>
            <p className="admin-help">
              {items
                .map(
                  ([key, title]) =>
                    `${title}：${status?.[provider]?.[key]?.available ? "资格已登记，实际请求仍核对额度" : reasons[status?.[provider]?.[key]?.reason] || "待核验"}`,
                )
                .join("；")}
            </p>
            {current.enabled && (
              <div className="admin-step-fields">
                {[
                  ["starts_at", "额度周期开始"],
                  ["ends_at", "额度周期结束"],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}（含时区）
                    <input
                      placeholder="2026-11-01T00:00:00+08:00"
                      value={current[key] || ""}
                      onChange={(e) => set(key, e.target.value || null)}
                    />
                  </label>
                ))}
                <label>
                  额度与资格核验依据
                  <input
                    value={current.evidence_ref}
                    onChange={(e) => set("evidence_ref", e.target.value)}
                    placeholder="官方后台核验记录或凭据编号"
                  />
                </label>
                <label>
                  付费授权依据（仅付费端点需要）
                  <input
                    value={current.paid_authorization_ref}
                    onChange={(e) =>
                      set("paid_authorization_ref", e.target.value)
                    }
                  />
                </label>
                {[
                  ["limit_units", "连接共享额度"],
                  ["project_limit_units", "单项目额度"],
                  ["question_limit_units", "单问题额度"],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}（{unit}）
                    <input
                      type="number"
                      min={0}
                      step={1 / scale}
                      value={current[key] / scale}
                      onChange={(e) =>
                        set(key, Math.round(Number(e.target.value) * scale))
                      }
                    />
                  </label>
                ))}
                {items.map(([key, title]) => {
                  const op = current.operations[key];
                  const update = (field: string, next: any) =>
                    set("operations", {
                      ...current.operations,
                      [key]: { ...op, [field]: next },
                    });
                  return (
                    <fieldset key={key}>
                      <legend>{title}</legend>
                      <label className="admin-checkbox">
                        <input
                          type="checkbox"
                          checked={Boolean(op)}
                          onChange={(e) => {
                            const next = { ...current.operations };
                            if (e.target.checked)
                              next[key] = {
                                trial_eligible: false,
                                price_version: "",
                                max_units: 0,
                              };
                            else delete next[key];
                            set("operations", next);
                          }}
                        />
                        允许调用此端点
                      </label>
                      {op && (
                        <>
                          <label className="admin-checkbox">
                            <input
                              type="checkbox"
                              checked={op.trial_eligible}
                              onChange={(e) =>
                                update("trial_eligible", e.target.checked)
                              }
                            />
                            已核实本连接可在此端点使用试用额度
                          </label>
                          <label>
                            价格版本／核验记录
                            <input
                              value={op.price_version}
                              onChange={(e) =>
                                update("price_version", e.target.value)
                              }
                            />
                          </label>
                          <label>
                            单次最大预留（{unit}）
                            <input
                              type="number"
                              min={0}
                              step={1 / scale}
                              value={op.max_units / scale}
                              onChange={(e) =>
                                update(
                                  "max_units",
                                  Math.round(Number(e.target.value) * scale),
                                )
                              }
                            />
                          </label>
                        </>
                      )}
                    </fieldset>
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </fieldset>
  );
}
