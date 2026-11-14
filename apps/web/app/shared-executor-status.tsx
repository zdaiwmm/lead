"use client";

import { useEffect, useState } from "react";
import { api, getWorkspace } from "./api-client";

type Status = {
  policy: { source: "legacy" | "managed_node"; node_id: string | null; revision?: string };
  node: { name: string; online: boolean; ready: boolean; auth_type?: string; state: string } | null;
  affected_projects: number;
  explicit_overrides: number;
};

export default function SharedExecutorStatus() {
  const workspace = getWorkspace();
  const [result, setResult] = useState<{ workspace: string | null; status: Status } | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const status = await api("/executor/nodes/status", undefined, controller.signal);
        if (controller.signal.aborted || getWorkspace() !== workspace) return;
        setResult({ workspace, status });
        setError("");
      } catch (failure) {
        if (!controller.signal.aborted && getWorkspace() === workspace)
          setError(failure instanceof Error ? failure.message : "服务器状态暂时无法读取");
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void load(), 10000);
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [workspace, version]);
  const status = result?.workspace === workspace ? result.status : null;
  const enabled = status?.policy.source === "managed_node";
  const node = status?.node;
  const label = !status ? "正在读取服务器状态…" : !enabled ? "共用服务器尚未启用" :
    !node || node.state !== "active" ? "服务器连接不可用" : !node.online ? "等待服务器上线" :
    !node.ready ? "服务器已连接，等待 Codex 就绪" : `${node.name} 已就绪`;
  return <section aria-label="共用服务器 Codex">
    <h2>共用服务器 Codex</h2>
    <p role="status"><span className={`dot ${enabled && node?.online && node.ready ? "green" : ""}`} />{label}</p>
    <p className="muted small">{enabled
      ? "新建运营 Agent 自动使用此服务器，创建前也可直接根据官网生成资料。服务器登录一次，项目无需逐个配对。"
      : "完成服务器连接并启用后，新建运营 Agent 可共用 Codex。也可以先手动填写产品资料。"}</p>
    {enabled && <p className="muted small">GPT-6.1 Sol · 中等推理。服务器离线时等待恢复，关闭网页后继续运行。</p>}
    {error && <p role="alert">{error}</p>}
    <button type="button" className="text-button" onClick={() => setVersion(value => value + 1)}>刷新服务器状态</button>
  </section>;
}
