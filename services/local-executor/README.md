# Lead local Codex executor

Runs the installed official Codex CLI as a child app-server. It uses the current OS user's existing Codex login and native conversation storage. Lead pairing credentials are stored separately; Codex authentication files are never read, copied, or uploaded.

## Requirements and installation

- Node.js 24 or later.
- An installed official `codex` CLI with ChatGPT sign-in and access to `gpt-6.1-sol` at medium effort.
- The Lead application supporting executor protocol v1.

From this directory:

```sh
npm ci
npm run build
node dist/cli.js status
node dist/cli.js pair --profile PROJECT_UUID --url https://your-lead-host --code CODE_FROM_LEAD --install
```

Install the released archive at a stable private path before pairing. The command copied from Lead includes the project profile and `--install`, which pairs and starts a project-specific macOS user LaunchAgent. Projects keep separate pairing, workspaces, request records and background services. `node dist/cli.js uninstall --profile PROJECT_UUID` stops only that service without deleting its state. Closing the Lead browser does not stop it. Sleep, shutdown and OS logout suspend execution until the device returns. For foreground operation omit `--install` and use `node dist/cli.js run --profile PROJECT_UUID`.

`--profile PROJECT_UUID` selects `~/.lead/executors/PROJECT_UUID`; commands without a profile retain the original `~/.lead/executor` default. `--state /absolute/private/path` selects an explicit alternate private directory. Use `LEAD_CODEX_BINARY=/absolute/path/to/codex` when launchd cannot find the installed CLI; install captures that path and the current PATH. `LEAD_CODEX_MEMORIES=1` enables native Codex memories for these processes, with Sol for extraction and consolidation; it does not edit global configuration. Review the native memory store's scope before enabling it for unrelated workspaces. This flag does not promise a separate background-memory effort setting.

`npm pack` produces a package with built runtime and the official MCP SDK runtime dependencies. Release CI embeds its exact SHA as `REVISION`; Lead serves that same archive from the authenticated `/api/executor/download` endpoint. Capability status reports that revision alongside the actual installed Codex version. Pairing and LaunchAgent installation are explicit commands, never package lifecycle scripts.

## Runtime and recovery

Each business request has an immutable payload identity and a native session key. The connection uses `initialize`/`initialized`, native thread creation or resume, and `turn/start` with explicit `gpt-6.1-sol` and `medium`. The same defaults are applied to native subagents. Search uses `live` unless the request preserves Lead's disabled public-web setting. Native shell, workspace, compaction, skills and installed plugins remain available through Codex's configured permissions.

The connector persists the request, thread, turn, pending user requests and an event outbox before acknowledging commands. It starts no replacement turn after an ambiguous start. On recovery it reads the original thread's paginated history and matches the stable Lead request marker. Unprovable execution is reported as `execution_unknown`. A previously unknown request can be reconciled against the original thread later; this never starts another turn. Lead's original Action ledger remains authoritative for external sends.

`request/started` carries the primary thread/turn only after a native RPC acknowledgment or exact original-turn history proof. Raw native events remain available separately, including child threads; they cannot replace the business request's primary IDs or usage snapshot. Once a turn ID is known, recovery matches only that ID. Without it, the marker must begin the original user message. Early completion notifications are retained by turn ID until that proof arrives. Offline interruptions remain durable and target only the proven original turn.

Native approval and user-input requests are forwarded as `server/request` events and wait for a matching `respond` command. The connector does not automatically accept them. Mid-turn instructions use `turn/steer` with the original turn ID. Interruptions do not establish that an external action did not occur.

Every run gets a request-bound MCP stdio server; it exposes only that request's declared tools and sends `request_id` to Lead for authorization. Text, structured results and screenshots pass through the official MCP SDK. The connector never receives a CDP URL or browser profile.

## Protocol v1

- `POST /executor/v1/pair`: `{code,name,platform,capabilities}` → `{device_id,token,epoch}`.
- All following requests use the returned Bearer token.
- `POST /executor/v1/poll`: `{epoch,instance_id,capabilities}` → `{epoch,commands}`. A new process uses a fresh instance ID. The returned epoch is persisted before any tool call or event upload; stale instances exit on 409.
- `POST /executor/v1/events`: `{epoch,events:[{id,request_id,method,params}]}` → `{accepted:[event_id],epoch}`. Only accepted IDs leave the outbox.
- `POST /executor/v1/tools`: `{epoch,request_id,name,arguments}` → an MCP tool result.

Run command payload: `{session_key,input,instructions,output_schema?,model?,effort?,tools?,tool_context?,web_search?}`. The supported model/effort are Sol/medium. `tools` are standard MCP descriptors `{name,description,inputSchema}`. Task and account authority are resolved by Lead, not by these arguments. Server paths cannot select local working directories. Lead retains the original Pydantic result schema separately: closed results use the SDK's strict schema, while contracts with dynamic dictionaries use a strict `result_json` string envelope and the unchanged business validator after decoding. Frozen older requests retain their original transport.

Commands are `run`, `steer` (`payload.input`), `interrupt`, or `respond` (`payload.id`, `payload.result`). `command/ack` marks durable receipt. The final `result` event carries `{state,output,thread_id,turn_id}`, with state `completed`, `failed`, `interrupted`, or `execution_unknown`. Native thread/item/turn and usage events are forwarded independently. Token usage is a snapshot, not a bill.

The connector uses an outbox and independent polling so a long turn does not block device heartbeats. Networking accepts HTTPS or explicit loopback HTTP and rejects redirects. State files have owner-only permissions. No server model API key is used.

## Verification

```sh
npm test
npm run build
node dist/cli.js status --state /private/tmp/lead-connector-contract
```

`status` only initializes app-server and reads account/model metadata; it creates no thread or model turn and omits account identifiers and credentials. Tests use an in-process fake RPC plus a real MCP stdio subprocess and HTTP fixture to verify replay, lost acknowledgments, original-thread reconciliation, pending approvals, screenshots, request scope, epoch takeover and HTTP redirect rejection. These checks do not prove a real business chain or real model inference.
