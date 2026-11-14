import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { Store } from "./store.ts";
import { LeadClient } from "./transport.ts";
import { RequestBridgeClient } from "./request-bridge.ts";
import { mcpKeys } from "./shared-runtime.ts";
const bridgePath = process.env.LEAD_REQUEST_BRIDGE;
if (bridgePath) for (const key of Object.keys(process.env)) if (!mcpKeys.has(key)) delete process.env[key];
const home = process.env.LEAD_EXECUTOR_HOME,
  id = process.env.LEAD_EXECUTOR_REQUEST;
if ((!home && !bridgePath) || !id) throw new Error("missing_request_scope");
const bridge = bridgePath ? new RequestBridgeClient(bridgePath, process.env.LEAD_REQUEST_TICKET || "", id) : undefined;
if (bridge) await bridge.open();
const initial = home && !bridge ? new Store(home) : undefined, record = initial?.request(id);
if (!bridge && !record) throw new Error("unknown_request_scope");
const tools = bridge?.tools || record!.payload.tools || [];
const server = new Server(
  { name: "lead_business_tools", version: "0.1.0" },
  { capabilities: { tools: {} } },
);
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (!tools.some((tool) => tool.name === request.params.name))
    throw new Error("tool_not_declared_for_request");
  if (bridge) {
    try {
      const result = await bridge.call("request.tools", { name: request.params.name, arguments: request.params.arguments || {} });
      if (!Array.isArray(result.content)) throw new Error("invalid_tool_result");
      return result as any;
    } catch {
      return { isError: true, content: [{ type: "text", text: "request_tool_unavailable" }] };
    }
  }
  const store = new Store(home!),
    binding = store.data.binding;
  if (!binding) throw new Error("device_not_paired");
  try {
    const result = await new LeadClient(binding).call("tools", {
      request_id: id,
      name: request.params.name,
      arguments: request.params.arguments || {},
    });
    if (!Array.isArray(result.content)) throw new Error("invalid_tool_result");
    return result as any;
  } catch (error) {
    return { isError: true, content: [{ type: "text", text: String(error) }] };
  }
});
await server.connect(new StdioServerTransport());
if (bridge) {
  const close = () => { bridge.close(); void server.close(); };
  process.once("SIGTERM", close); process.once("SIGINT", close);
  process.stdin.once("end", close);
}
