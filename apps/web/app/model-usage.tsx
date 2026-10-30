"use client";

type Row = Record<string, any>;
const amount = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
const providers: Row = {
  openai: "OpenAI",
  deepseek: "DeepSeek",
  unrecorded: "服务未记录",
};

export default function ModelUsage({ rows = [] }: { rows?: Row[] }) {
  return (
    <div className="model-usage">
      <p className="muted small">
        近 30 天按实际调用模型统计。今日用量按北京时间 08:00
        重置；缓存命中已包含在输入 token 中，预留是估算，不计入已用量。
      </p>
      {!rows.length ? (
        <p className="muted">此范围尚无模型用量记录。</p>
      ) : (
        <div
          className="model-usage-scroll"
          tabIndex={0}
          role="region"
          aria-label="按模型统计的 token 使用量"
        >
          <table className="model-usage-table">
            <thead>
              <tr>
                <th>实际模型</th>
                <th>近 30 天已用</th>
                <th>今日已用</th>
                <th>输入 / 输出</th>
                <th>缓存命中</th>
                <th>预估模型费用</th>
                <th>调用中 / 待核对预留</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.provider}/${row.model}`}>
                  <th scope="row">
                    <strong>
                      {row.model === "unrecorded"
                        ? "历史模型未记录"
                        : row.model.replace(/^(openai|deepseek):/, "")}
                    </strong>
                    <small>{providers[row.provider] || row.provider}</small>
                  </th>
                  <td>{amount(row.tokens)}</td>
                  <td>{amount(row.today_tokens)}</td>
                  <td>
                    {row.missing_breakdown_calls === row.invocations &&
                    row.invocations > 0
                      ? "未记录明细"
                      : `${amount(row.input_tokens)} / ${amount(row.output_tokens)}`}
                    {row.missing_breakdown_calls > 0 &&
                      row.missing_breakdown_calls < row.invocations && (
                        <small>部分记录缺少明细</small>
                      )}
                  </td>
                  <td>
                    {row.unverified_cache_calls === row.invocations &&
                    row.invocations > 0
                      ? "未完整记录"
                      : amount(row.cached_input_tokens)}
                  </td>
                  <td>
                    {Object.keys(row.costs || {}).length
                      ? Object.entries(row.costs).map(([currency, value]) => (
                          <div key={currency}>
                            {currency}{" "}
                            {Number(value).toLocaleString("zh-CN", {
                              maximumFractionDigits: 8,
                            })}
                          </div>
                        ))
                      : "暂无法估算"}
                    {row.unpriced_calls > 0 && (
                      <small>{row.unpriced_calls} 次调用未计价</small>
                    )}
                  </td>
                  <td>
                    {amount(row.reserved_tokens)} /{" "}
                    {amount(row.unknown_reserved_tokens)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows.some((row) => row.model === "unrecorded") && (
        <p className="muted small">
          未标记模型的历史记录单独保留，不归入当前所选模型。
        </p>
      )}
    </div>
  );
}
