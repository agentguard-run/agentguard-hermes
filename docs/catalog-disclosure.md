# Catalog PR disclosure

What AgentGuard for Hermes Agent does at runtime, for catalog review. At 0.4.1,
`hermes plugins validate --install-deps` passes every check on Hermes Agent
v0.21.6, and the install security scan reports safe.

## Plugin runtime

The rule pack quotes the commands it blocks and ships in the `agentguard-spend`
dependency. The JSON is a fresh, unmodified export of the Codex plugin source
of truth. Python matcher logic and pattern strings are unchanged. Style edits
expand regex flag aliases, use consecutive-pair iteration, flatten one condition
and explain the existing fail-closed exception handler for the SDK linter. No pattern was
reworded, encoded, split, hidden or weakened to change a scanner verdict.

The plugin registers `llm_request`, `llm_execution` and `tool_execution`
middleware; `pre_approval_request`, `post_approval_response`,
`pre_auxiliary_call` and `post_auxiliary_call`; and the `agentguard` slash command.
The optional Desktop SDK contribution registers `statusBar.right` and a GET
route at `/api/plugins/agentguard-spend/status`. There are no model tools,
providers or approval transports registered by this plugin.

- Network: the Python plugin and the SDK functions it uses make no direct HTTP
  calls. An operator-selected Hermes approval transport may contact its configured
  service, such as the push server chosen by a phone approval plugin. The host
  passes it a one-operation identifier and digest, generic approval label, public
  rule reason, allowed choices and timeout. No raw arguments are sent. Built-in
  messaging bridges use their configured platform endpoints. Desktop makes GET
  requests to the authenticated status route on the connected Hermes gateway,
  once on mount and every 30 seconds while mounted. That gateway may be remote
  if the operator connected one. No analytics endpoint is contacted.
  Hermes continues provider and tool calls after applicable gates; auxiliary
  hooks cannot veto their provider calls.
  The plugin does not construct `SpendGuard`, invoke licensing or telemetry, fetch
  prices, or use the optional SDK network APIs listed below.
- Reads: its policy, signing keypair and existing receipt chain beneath
  `HERMES_HOME/agentguard`; Hermes's source model tables, approval context and
  `HERMES_HOME/cache/openrouter_model_metadata.json`, approval configuration
  through Hermes's public config API, and the SDK's
  `AGENTGUARD_HOME/cost-overrides.json`, defaulting to `~/.agentguard`.
  It inspects request and response metadata in memory for accounting and scans
  tool arguments locally. Prompts, completions and raw arguments are not written
  to its decision log. The shared scanner can read bounded local Git metadata
  when a caller supplies a working directory. Hermes currently supplies none at
  this middleware boundary, so it treats branches conservatively.
- Writes: `policy.yaml`, `private.key`, `public.key`, `decisions.ndjson` and
  `decisions.lock` beneath `HERMES_HOME/agentguard`, defaulting to
  `~/.hermes/agentguard`. The directory is mode 0700 and the private key is created
  with mode 0600. A preexisting directory is tightened to mode 0700.
- Credentials: reads only its own Ed25519 signing key. No provider key, browser
  profile or other CLI token is read, refreshed or changed by this plugin.
- Shell commands: none. The scanners inspect quoted commands without executing
  them. Hermes's own allowed tool calls are outside this claim.
- Background processes: none started by the plugin. A synchronous receipt write
  inside an existing event loop uses a temporary worker thread and waits for it.
  Hermes invokes approval transports on its bounded worker pool. A timed-out
  callback may keep running until it exits, but cannot grant a late approval.
  Desktop uses a scoped timer removed on unmount. Hermes may itself launch the
  configured plugin-host process; AgentGuard does not start or configure it.
- Optional environment settings: `HOME`, `HERMES_HOME`, `HERMES_SESSION_KEY`,
  `HERMES_SESSION_ID`, `HERMES_PLUGIN_HOST_PROCESS`, `AGENTGUARD_DISABLE`,
  `AGENTGUARD_HOME`, `AGENTGUARD_LOCALE`, `LC_ALL`, `LC_MESSAGES`, `LANG`.
  No credential environment variable is required. `HOME` expands local paths;
  the final four settings choose policy language. Session identities are hashed.
  The host-process marker prevents use of an unrelated approval or status registry.

Hermes owns any writes made by a configured approval transport or messaging
bridge, including pending approval state and its own session/permanent approval
settings. AgentGuard uses fresh operation rule IDs and accepts only once; a wider
scope returned by an older host interface cannot approve later AgentGuard calls.
Hermes also installs the Desktop package and its marker in its desktop-plugins
folder when the operator enables unified packages. AgentGuard does not write
those files itself. Transport and gateway credentials remain owned by Hermes
and the selected transport; AgentGuard does not read them.

The validator can warn about package-install examples and examples of operations
the rules block. Those warnings remain visible for review. Moving the rule pack
does not claim that dependency code is outside the review boundary.

## Optional SDK network paths

These are present in the dependency but are not invoked by this plugin. Importing
the SDK and its guards does not start them. Endpoints described as configurable
may be replaced by the SDK caller; injected clients or callbacks may use other
operator-selected endpoints.

| SDK entry point | Request and destination | When it runs and what it sends |
| :--- | :--- | :--- |
| License validation | POST `https://agentguard.run/api/license/validate` | An explicit validation or a standalone `SpendGuard` license check with a configured license key, when the one-day cache is absent, stale or bypassed. Sends that key. The base is configurable through the call or `AGENTGUARD_LICENSE_ENDPOINT`. |
| Seat registration | POST `https://agentguard.run/api/license/seats` | Standalone license registration with a paid key, or unlicensed registration with explicit seat counting or activation-metrics consent. Sends the key if present, a machine fingerprint and process ID. Paid metering is separate from the opt-in beacon. Same configurable base. |
| Activation beacon | POST `https://agentguard.run/api/beacon` | A telemetry event only after opt-in by `AGENTGUARD_TELEMETRY=1` or saved consent. `AGENTGUARD_NO_BEACON=1` disables it. Sends an anonymous install ID, event, SDK/runtime/OS versions and CI/TTY flags. No model content. |
| Managed inference key | GET `https://agentguard.run/api/tenant/openrouter-key` | Explicit managed-key resolution for a licensed SDK runtime when its memory cache expires. Sends a bearer license key, machine fingerprint and process ID. The returned provider key stays in memory. Configurable base. |
| Foreign-origin consent | GET `https://agentguard.run/api/consent/verify/{id}` | Explicit receipt verification or the standalone foreign-origin posture gate when it needs verification. Sends the receipt ID. Configurable base. |
| Model catalog and price sync | GET `https://openrouter.ai/api/v1/models` | Explicit `fetch_catalog` or price sync with no fresh local cache; a supplied local fixture bypasses HTTP. No provider key is sent. Endpoint and cache path are configurable. |
| OpenRouter key verification | GET `https://openrouter.ai/api/v1/auth/key` | Explicit SDK CLI authentication or key verification. Sends the operator's OpenRouter key as a bearer token. |
| Advisor | POST `/chat/completions` at `https://openrouter.ai/api/v1` or `https://api.openai.com/v1`; POST `/messages` at `https://api.anthropic.com/v1` | An explicitly started advisor conversation. Sends the conversation and the selected provider key. Supports an operator-provided compatible base URL or opener. This feature carries content to that provider. |
| Remote workflow store | POST `/api/workflows/create` and `/api/workflows/{id}/receipt`; GET `/api/workflows/lookup?name=...` and `/api/workflows/{id}/chain` | Explicit workflows with `api_base_url` or `AGENTGUARD_WORKFLOW_API_BASE` configured. Sends workflow metadata and receipts, with an optional bearer license key. There is no default remote base. |
| Provider and framework bindings | Caller-selected OpenAI, Anthropic, Bedrock, OpenRouter or framework endpoint | Explicit SDK-wrapped provider calls. The caller's client or runner carries its credentials and model content. These client wrappers are no longer used by the Hermes plugin. |
| Remote storage | Configured Redis or PostgreSQL connection | Explicit creation and use of those optional store adapters. Sends spend and decision records using the supplied connection/client credentials. No default hosted service. |
| Remote signing | Configured AWS KMS endpoint/region or Vault transit URL, default Vault URL `http://127.0.0.1:8200` | Explicit construction and use of remote signing adapters. KMS fetches a public key and signs digests using the caller's AWS credential chain. Vault reads the configured key and signs through transit using its configured token. |
| Local dashboard | Listens on the SDK CLI's configured host/port, default `127.0.0.1:8787` | Only the explicit `agentguard serve` command. Serves local decision data; optionally opens a browser. It is not started by the plugin. |

The optional SDK APIs can read or write their own `AGENTGUARD_HOME` configuration,
license cache, `process-id`, telemetry `install.json`, `openrouter-key`, model
catalog, cost overrides and caller-selected receipt/checkpoint/output paths.
They can read `AGENTGUARD_LICENSE_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` or `AGENTGUARD_ADVISOR_API_KEY` when those corresponding
features are explicitly used. Remote adapters use credentials supplied to their
clients; an AWS client may consult its standard credential chain. No plugin code
reads those credentials. The SDK does not launch shell commands. Its optional
dashboard can ask the operating system to launch a browser and runs a local
server until stopped. Optional synchronous/async bridges use worker threads,
not persistent child processes. No self-updater is present.

## Host API findings and limits

Hermes 0.16.0 is the first release with middleware, so `requires_hermes` is
`>=0.16.0`.

Hermes 0.21.0 gives PyPI entry points precedence over directory copies. Latest
Hermes gives an explicit directory copy precedence, so a stale directory can
shadow a PyPI upgrade. Its installer also prepares dependencies transactionally
and refuses a non-interactive active replacement without dependency consent.
On v0.21.6, `hermes plugins install agentguard-run/agentguard-hermes --yes-deps
--enable` prepares the dependencies in that transaction and loads the plugin.

On supported hosts the lockdown registers before SDK imports and stays closed
on setup failure. If the middleware method is unexpectedly absent, there is no
public hook that can register an equivalent model gate. Initialization therefore
raises the fatal `AgentGuardMiddlewareRequired` exception. It deliberately does
not inherit from `Exception` or `SystemExit`, which Hermes catches before dropping
the plugin and continuing. This is a stop during initialization, not a claim that
an absent middleware API can register a lockdown. A host that skips incompatible
plugins before calling `register` cannot be protected by that plugin.

Auxiliary model calls bypass the model execution middleware. The current
`pre_auxiliary_call` and `post_auxiliary_call` hooks ignore return values and cannot
veto a call. AgentGuard now counts their metadata toward the shared spend caps
and signs it. Retry numbers distinguish physical attempts; matching middleware
identities are not counted twice. Streams without usage use a request estimate,
unknown prices remain unpriced, and missing completion hooks leave reservations
held. Hermes 0.21.0 does not emit these events, so its auxiliary calls remain
uncounted. The gateway dispatch hook accepts skip and incoming-text
rewrite directives, not a reply directive; no gateway notice is installed.
Auxiliary registration is skipped on hosts without those names in `VALID_HOOKS`.
The current catalog manifest declares them, so the 0.21.0 doctor still rejects
two unknown declared hooks. Its runtime loads successfully and is covered by
the offline feature smoke. No alternate or misleading manifest is substituted
for validation.
The Desktop chip uses only status slots, SDK components and a scoped backend
route. Its counters are those of the process serving the command, with the
persistent daily total separately labelled. A separate turn process has its
own caps; there is no global enforcement counter across processes.

The public command API supports CLI and gateway slash commands. The public
approval API supports configured transports, but its generic tool approval gate
also supports persistent scopes. AgentGuard's selected-transport request offers
only once and deny; its built-in bridge requires a matching human once response
as well as the host's allow. Automatic approvals never suffice. Unanswered,
timed-out, failed or stale replies deny the operation. With no transport the
original unattended denial message is unchanged.

Plugin-host isolation has no public bridge to its parent's approval channel or
the Desktop command registry. Unattended approvals deny and the Desktop chip
reports unavailable in that mode. The plugin never changes the operator's
isolation setting. Native approval observations and middleware remain registered.
