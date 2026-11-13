import type { Binding, Dict } from "./types.ts";
export function validateUrl(url: string) {
  const parsed = new URL(url);
  if (
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.protocol !== "https:" &&
      !(
        parsed.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname)
      ))
  )
    throw new Error("lead_url_requires_https_or_loopback");
  return parsed.origin;
}
export async function post(
  url: string,
  path: string,
  payload: Dict,
  token?: string,
) {
  const response = await fetch(validateUrl(url) + path, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: "Bearer " + token } : {}),
    },
    body: JSON.stringify(payload),
    redirect: "error",
    signal: AbortSignal.timeout(40_000),
  });
  if (!response.ok) throw new Error("lead_http_" + response.status);
  return (await response.json()) as Dict;
}
export class LeadClient {
  binding: Binding;
  constructor(binding: Binding) {
    this.binding = binding;
  }
  call(path: string, payload: Dict) {
    return post(
      this.binding.url,
      "/executor/v1/" + path,
      { ...payload, epoch: this.binding.epoch },
      this.binding.token,
    );
  }
}
