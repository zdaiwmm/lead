import { createHash } from "node:crypto";
import { join } from "node:path";
import type { ServerBinding } from "./types.ts";
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export function validateServerBinding(value: unknown): ServerBinding {
  const b = value as ServerBinding;
  const fields = ["version", "transport", "scope_ref", "scope_kind", "tenant_id", "epoch", "grant_generation", "policy_revision",
    ...(b?.scope_kind === "project" ? ["agent_id", "device_id"] : ["studio_id"])];
  if (!b || typeof b !== "object" || Array.isArray(b) || Object.keys(b).sort().join() !== fields.sort().join() ||
      b.version !== 2 || b.transport !== "unix-broker" || !["project", "studio"].includes(b.scope_kind) ||
      !Number.isSafeInteger(b.epoch) || b.epoch < 1 || !Number.isSafeInteger(b.grant_generation) || b.grant_generation < 1 ||
      typeof b.policy_revision !== "string" || !b.policy_revision || b.policy_revision.length > 300 ||
      [b.scope_ref, b.tenant_id, ...(b.scope_kind === "project" ? [b.agent_id, b.device_id] : [b.studio_id])]
        .some(id => typeof id !== "string" || !uuid.test(id))) throw new Error("invalid_server_binding");
  return JSON.parse(JSON.stringify(b));
}
export function bindingIdentity(binding: ServerBinding) {
  const { epoch, policy_revision, ...identity } = validateServerBinding(binding);
  return JSON.stringify(identity, Object.keys(identity).sort());
}
export function scopeStoreFolder(root: string, binding: ServerBinding) {
  const b = validateServerBinding(binding);
  return join(root, "scopes", createHash("sha256").update(JSON.stringify([
    b.tenant_id, b.scope_kind, b.agent_id || b.studio_id, b.scope_ref,
  ])).digest("hex"));
}
