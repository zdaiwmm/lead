import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { AppServer } from "./rpc.ts";
import { MODEL, EFFORT, nativeArgs } from "./runner.ts";
import type { RpcLike } from "./types.ts";
export async function capabilities(
  rpc: RpcLike = new AppServer(
    process.env.LEAD_CODEX_BINARY || "codex",
    nativeArgs(),
  ),
  version?: string,
) {
  if (!version)
    version = execFileSync(
      process.env.LEAD_CODEX_BINARY || "codex",
      ["--version"],
      { encoding: "utf8" },
    ).trim();
  await rpc.open();
  try {
    const account = await rpc.request("account/read", { refreshToken: false });
    let cursor: string | undefined, model: any;
    do {
      const page = await rpc.request("model/list", {
        includeHidden: false,
        ...(cursor ? { cursor } : {}),
      });
      model = (page.data || []).find(
        (m: any) => (m.model === MODEL || m.id === MODEL) && !m.hidden,
      );
      cursor = page.nextCursor || undefined;
    } while (!model && cursor);
    const efforts = (model?.supportedReasoningEfforts || []).map(
      (e: any) => e.reasoningEffort || e,
    );
    const auth =
      account.account?.type ||
      account.authType ||
      account.auth_type ||
      "signed_out";
    return {
      ready: auth === "chatgpt" && !!model && efforts.includes(EFFORT),
      auth_type: auth,
      cli_version: version,
      connector_revision: existsSync(new URL("../REVISION", import.meta.url))
        ? readFileSync(new URL("../REVISION", import.meta.url), "utf8").trim()
        : "development",
      models: model ? [MODEL] : [],
      reasoning_efforts: efforts,
      input_modalities: model?.inputModalities || [],
      multi_agent_version: model?.multiAgentVersion || null,
    };
  } finally {
    await rpc.close();
  }
}
