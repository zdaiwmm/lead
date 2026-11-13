import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { Store } from "./store.ts";
import { LeadClient } from "./transport.ts";
const home = process.env.LEAD_EXECUTOR_HOME,
  id = process.env.LEAD_EXECUTOR_REQUEST;
if (!home || !id) throw new Error("missing_request_scope");
const initial = new Store(home),
  record = initial.request(id);
if (!record) throw new Error("unknown_request_scope");
const tools = record.payload.tools || [];
const server = new Server(
  { name: "lead_business_tools", version: "0.1.0" },
  { capabilities: { tools: {} } },
);
server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (!tools.some((tool) => tool.name === request.params.name))
    throw new Error("tool_not_declared_for_request");
  const store = new Store(home),
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
