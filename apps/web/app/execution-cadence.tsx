import type { ExecutionCadence } from "./api-client";

export default function CadenceStatus({ value, zone = "UTC" }: { value?: ExecutionCadence | null; zone?: string }) {
  if (!value) return <p className="small muted" data-cadence-state="unverified">等待依据待核实</p>;
  const date = value.state === "waiting_time" && value.next_at ? new Date(value.next_at) : null;
  const deadline = date && Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("zh-CN", { timeZone: zone, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date)
    : null;
  return <div className="small" data-cadence-state={value.state}>
    <p>{value.reason_label || "等待依据待核实"}</p>
    <p className="muted">{deadline ? `${deadline} 后重新核验` : value.resume_event_label || (value.state === "waiting_event" ? "条件变化后重查" : value.state === "ready" ? "执行前继续核验账号和目标" : value.state === "terminal" ? "本项结果已保留" : "恢复条件待核实")}</p>
    <p className="muted">{value.scope_label || "适用范围待核实"} · 依据：{value.evidence_source || "待核实"}</p>
    {value.state === "unverified" && value.compatible === null && <p>执行条件与版本尚待核实。</p>}
    {value.compatible === false && <p>执行版本待核对，新操作保持等待。</p>}
  </div>;
}
