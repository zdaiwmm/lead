export type Dict = Record<string, any>;
export interface ToolSpec {
  name: string;
  description: string;
  inputSchema: Dict;
}
export interface RunPayload {
  session_key: string;
  input: string;
  instructions: string;
  output_schema?: Dict;
  model?: string;
  effort?: string;
  tools?: ToolSpec[];
  tool_context?: Dict;
  web_search?: "live" | "disabled";
}
export interface Command {
  id: string;
  request_id: string;
  kind: "run" | "steer" | "interrupt" | "respond";
  payload: Dict;
}
export interface Event {
  id: string;
  request_id: string;
  method: string;
  params: Dict;
}
export interface Binding {
  url: string;
  device_id: string;
  token: string;
  epoch: number;
  name: string;
}
export interface ServerBinding {
  version: 2;
  transport: "unix-broker";
  scope_ref: string;
  scope_kind: "project" | "studio";
  tenant_id: string;
  agent_id?: string;
  device_id?: string;
  studio_id?: string;
  epoch: number;
  grant_generation: number;
  policy_revision: string;
}
export interface ExecutorClient {
  binding?: Binding | ServerBinding;
  call(path: string, payload: Dict): Promise<Dict>;
}
export interface BrokerRpc {
  call(method: string, params: Dict): Promise<Dict>;
}
export interface RequestRecord {
  id: string;
  hash: string;
  payload: RunPayload;
  state: string;
  thread_id?: string;
  turn_id?: string;
  output?: unknown;
  pending?: Dict;
  last_message?: string;
  command_ids: string[];
  cancel_requested?: boolean;
}
export interface RpcLike {
  open(): Promise<void>;
  close(): Promise<void>;
  request(method: string, params?: Dict): Promise<any>;
  respond(id: string | number, result: any): void;
  onNotification(fn: (method: string, params: Dict) => void): void;
  onRequest(
    fn: (id: string | number, method: string, params: Dict) => void,
  ): void;
}
