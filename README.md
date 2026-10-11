# AgentGuard for Hermes Agent

Local spend caps, tool-call guarding, sub-agent limits and Ed25519-signed decisions for Hermes Agent. The PyPI package name remains `agentguard-spend-hermes`.

## Install

Hermes v0.21.6 and later run from a managed environment under `HERMES_HOME`. Install from the AgentGuard repository so Hermes prepares the Python dependencies itself:

```bash
hermes plugins install agentguard-run/agentguard-hermes --yes-deps --enable
```

Or install the PyPI package into the environment Hermes runs from, then enable it:

```bash
# Hermes v0.21.6 and later (managed environment)
uv pip install --python "$(ls -td ~/.hermes/installs/*/environments/*/venv/bin/python | head -1)" agentguard-spend-hermes
# Hermes before v0.21.6
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python agentguard-spend-hermes

hermes plugins enable agentguard-spend
```

The PyPI entry point and a directory copy share the plugin name `agentguard-spend`. Latest Hermes gives an explicit directory copy precedence, so remove an outdated directory installation before relying on a PyPI upgrade. Hermes loads one winning copy.

After a maintainer lists this plugin in the Hermes catalog, the catalog install path will be `hermes plugins install agentguard-spend`. It is not listed by this change. The draft entry is in `docs/catalog-entry.yaml`; its repository destination and SHA still need release review.

The rule pack now ships in the SDK dependency. The plugin scan can still report caution findings for documented blocked operations and package-install instructions. These are disclosed for human review. No rule pattern was rewritten or hidden to change the scanner result.

Hermes 0.21.0 displays declared Python dependencies without installing them. Latest Hermes prepares them as part of its installation transaction and requires consent to replace an active plugin. That path needs the released SDK containing the guards module. If a dependency is missing, or the policy or keys cannot be read on a supported host, AgentGuard registers a lockdown before setup, and a failed setup leaves it refusing every model call and every tool call with this message, also written to the log:

```text
AgentGuard setup failed: <reason>. Fix it, or set AGENTGUARD_DISABLE=1 to run without AgentGuard.
```

When setup succeeds the lockdown passes every call through. Enablement is required for both the PyPI entry point and directory installation. Restart the Hermes process you want to protect after installation or a policy change.

On first load AgentGuard creates `agentguard/policy.yaml`, a signing keypair and, on the first decision, `agentguard/decisions.ndjson` under `HERMES_HOME` (default `~/.hermes`). The directory is owner-only, with a private key created using mode `0600`.

The startup log identifies the active path:

```text
AgentGuard 0.4.0: official middleware active (policy=...)
```

Hermes 0.16.0 is the first released version with `register_middleware`; 0.21.0 qualifies. Client wrappers have been removed. On a supported host, setup failures leave the registered lockdown closed. If middleware is unavailable despite the version requirement, initialization stops with `AgentGuardMiddlewareRequired`, a fatal exception naming the required version. Without that API a plugin cannot register a model gate. Hermes catches ordinary exceptions and `SystemExit` and discards a failed plugin, so neither is used for this case. `AGENTGUARD_DISABLE=1` explicitly disables the plugin, including the lockdown. Hermes may skip plugins whose declared version floor is unmet; do not use an older host for protection.

## Commands

The public Hermes slash-command API exposes these in the CLI and supported
messaging adapters. Discord receives an argument hint for the same command.

- `/agentguard` or `/agentguard status`: UTC daily soft and hard caps, the
  per-minute cap, reservations, reported and estimated cost counts, unpriced
  calls, and helper launches in both active-time windows. A since-midnight pace
  estimate appears only when it reaches the hard cap later today.
- `/agentguard log [24h|7d|session]`: allowed, asked and denied operation counts
  by kind and rule, answered and timed-out approvals, and median answer time.
  The receipt chain is verified before these counts are shown.
- `/agentguard verify`: signatures, hash links, sequence continuity and complete
  JSON records. A malformed or incomplete row fails verification.

Live caps and helper counters belong to this Hermes process and reset when it
restarts. Status separately labels the total recorded today across processes.
Unpriced calls have unknown cost and are not free. Session filtering uses a hash
of the host's conversation key, or session ID in the CLI. If Hermes supplies
neither, the command reports that limitation instead of showing another session.
Commands never include prompts, completions, raw arguments or raw session IDs.
Status reads the local ledger; use verify to check its integrity explicitly.

## Desktop status

The assembled folder includes a public Desktop SDK contribution in
`desktop/plugin.js` and a read-only `GET /api/plugins/agentguard-spend/status`
backend. Enable the Python plugin in the gateway profile and its Desktop
contribution in Capabilities. The chip shows the daily hard cap and short helper
window; its popover shows the same details as `/agentguard status`. It refreshes
every 30 seconds while mounted and clears old data on profile or connection
changes. Disable or unmount removes its timer. No core UI is patched.

The chip describes the process serving that command. Separate turn processes
have separate counters. With `plugins.isolation: host`, the public SDK has no
bridge to those live counters, so the chip reports unavailable and directs the
user to `/agentguard`. The PyPI entry point alone does not install Desktop files;
use the assembled unified folder for this optional contribution.

## What it blocks

The shared AgentGuard rule pack checks terminal commands, text typed into a background process with Hermes's `process` tool, Python code execution and file writes. It covers credential uploads (including GnuPG, SSH and cloud credentials), destructive deletion, shared Git history changes, unsafe infrastructure changes, sensitive file writes, secret-shaped arguments, security setting changes, remote package sources, disk formatting and database wipes.

For example, uploading `~/.gnupg` with rsync is denied. Copying `.env.example` to `.env` within a project is allowed. Code execution checks literal calls to terminal, subprocess and file-writing functions as well as the shared native-code rules. Computed command or path arguments require approval.

Hermes's existing availability, approval and deny checks remain in its execution chain. AgentGuard can add a restriction; it never replaces a Hermes restriction with an allow. A gate error denies operations with effects and permits known read-only tools to continue through Hermes's checks.

## How ASK works unattended

An ASK decision in an interactive CLI with a TTY uses Hermes's own approval prompt. Approval applies to one operation. A decline, timeout or prompt error denies it.

Without a TTY, AgentGuard uses Hermes's public `ApprovalRequest` and
`invoke_approval_transport` contracts for the selected
`security.approval.transport`. Only once and deny are offered. Requests are
uniquely bound to one operation; stale, missing, timed-out, interrupted or failed
answers deny it. The timeout comes from `approvals.timeout`, with a plugin limit
of one hour. Invalid configuration denies without trying a different surface.

With the built-in transport, Hermes's public tool approval gate routes the
request to a configured messaging bridge. AgentGuard requires both a correlated
human once response and the host's positive verdict. Automatic smart-mode or
allowlist results cannot substitute for a person. Older built-in interfaces may
offer session or permanent approval; AgentGuard rejects those choices and uses
a new rule identity for every request, so they cannot approve later operations.
The signed operation receipt includes the outcome, surface and answer latency.

With no answering transport, ASK keeps the existing tool error exactly:

```text
AgentGuard: this needs a person's approval
```

The latest isolated plugin host does not expose its parent's live approval
channel through the public context API. Under `plugins.isolation: host`,
unattended ASK keeps this same denial. AgentGuard does not change isolation
settings or import a duplicate transport registry to bypass the boundary.

Rules default to their shared settings. An operator can change a rule in `policy.yaml`, for example `guard_rules: {GP009: warn}` to ask before a sensitive write. `stop` denies, `warn` asks, and `off` disables that AgentGuard rule. Hermes's own checks still apply.

## Spend and delegation policy

The default policy has a $5 daily soft cap, a $20 daily hard cap and a $2 per-minute hard cap. Amounts are integer USD cents. At the soft cap, request middleware swaps to the provider's mapped model only when that exact model id appears in the provider's catalog data Hermes keeps locally and costs less per input token and per output token than the current model. Otherwise it keeps the model, logs one line, and the hard cap stops spending. Execution middleware checks hard caps before calling the provider and records cost afterwards.

The default downgrade targets are Sonnet for Anthropic and OpenRouter, GPT-5 mini for OpenAI, and Gemini 3.1 Flash Lite for Gemini. A provider without a mapping never swaps. Prices come from the Spend price table, Hermes's own price table and Hermes's on-disk copy of OpenRouter's model list; AgentGuard never fetches them. Existing 0.2.x policies keep their caps and can add this mapping:

```yaml
model_downgrades:
  openrouter: anthropic/claude-sonnet-4.6
  anthropic: claude-sonnet-4-6
  openai: gpt-5-mini
  gemini: gemini-3.1-flash-lite-preview

delegation:
  short_launches: 15
  short_minutes: 15
  long_launches: 40
  long_minutes: 120
  idle_seconds: 60
```

The 16th launch inside 15 active minutes, or the 41st inside 120 active minutes, requires approval. A batch counts each child. List, steer and stop operations do not count as launches. Accepted attempts count even if the downstream tool later fails, so failures cannot reset the limit.

An old policy's Anthropic downgrade target is translated to the default for direct OpenAI or Gemini calls and to OpenRouter's dotted id on OpenRouter, and then passes the same catalog and price checks. A configured `requiredCapability` stays closed because Hermes does not supply Spend capability claims.

The active clock advances during model/tool execution. Between calls it advances by at most `idle_seconds`; long idle gaps do not clear launch history. Counters are shared by child agents using the same runtime. Before each model call, AgentGuard reserves the call's estimated cost in every spend window under a short lock. The provider call runs without the lock, then the actual cost replaces the reservation; a failed, interrupted or cancelled call releases it. Reserved estimates count as spent, so calls running at the same time cannot spend the same remaining budget, and sessions, cron jobs and background sub-agents no longer wait for each other's model calls or for a pending approval prompt. Calls in flight together can pass a hard cap only by the amount their actual cost exceeds their estimates.

## Signed decisions and privacy

The public `pre_approval_request` and `post_approval_response` hooks observe
Hermes's own prompts. Their receipts include the surface, approved once,
approved for session, approved permanently, denied or unanswered outcome, and
answer latency when the two hooks can be correlated. This is observation only.
Commands, descriptions, transport names and arbitrary host rule text are never
copied into these receipts. Request identity and session identity are hashed.
Unmatched or ambiguous responses have no claimed latency. If signing fails,
the next AgentGuard model gate closes; the observer never changes Hermes's
approval result. Hosts that do not emit these hooks cannot supply this history.

Every guarded model call, tool decision and delegation is a signed Spend receipt in `decisions.ndjson`. The existing Spend schema holds the metadata under `decision.governanceReceipt`: `kind` (`model_call`, `tool_call` or `delegate`), `rule_id`, `decision` and a reason. Tool receipts also include `tool_name`, `arguments_sha256` and the initial `gate_decision`, so an approved ASK remains visible.

Tool gates are recorded before execution. If Hermes itself then blocks a terminal, code, file-write or process call, or leaves it waiting for approval, a second receipt with `stage: host_result` records that restriction and the result is preserved. Results of other tools carry outside data and are not read for this.

AgentGuard never stores prompts, completions or raw tool arguments in this log. Argument hashes use SHA-256 over deterministic JSON. Rule scanning runs locally and never executes inspected commands. AgentGuard itself makes no network requests. The LLM call still goes to the provider configured in Hermes.

Model receipts identify the cost source:

| Source | Meaning |
| --- | --- |
| `gateway_reported` | Provider response includes a cost in USD, including OpenRouter usage cost. |
| `api_list_price_estimate` | Usage tokens are priced with Spend's local price table. |
| `unpriced` | No provider cost and no usable local price or usage information. |

Reported USD cost rounds up to integer cents. Estimates use Spend's rounding and include Anthropic cache tokens at base input price. These are local accounting records, not provider invoices.

Verify with the existing Spend command using your public key encoded as hex:

```bash
agentguard verify --trace ~/.hermes/agentguard/decisions.ndjson --public-key PUBLIC_KEY_HEX
```

The private key never belongs in that command. Existing 0.2.x receipts can remain at the beginning of the chain. New receipt appends use a local file lock so concurrent writers preserve sequence and signature linkage.

## Limits

This is a lexical guard, not a sandbox. Encoded programs, files fetched and executed in separate calls, and arbitrary dynamic Python can exceed what local rule matching can infer. Python cannot determine every effect of an unknown function. Known limitations of the shared guard fixtures remain visible in parity tests.

Budget and launch counters live in the Hermes process and reset on restart. Separate processes do not share live caps, even when their signed decisions share a file. Use a separate `HERMES_HOME` for separate policies. Scope labels identify the budget; they do not split one policy into independent budgets.

Before a provider call, cost is a character/token estimate over the messages, system prompt, instructions and tool schemas, with `max_tokens` (or 4096) as the output. A provider's actual charge can exceed it. Unpriced calls cannot be charged accurately and are recorded as unpriced; previously reached hard caps still block them. A missing or malformed model payload is denied. A malformed usage response latches the model gate closed until restart. A signing failure is logged as an error and prevents subsequent model calls; tool gate failures follow the read-only rule above.

Protection applies at Hermes's registered middleware boundaries. The plugin must be enabled and importable; if Hermes cannot import it at all (for example its files are removed), Hermes runs without it. The `codex_app_server` runtime hands whole turns to Codex and bypasses both boundaries.

Current Hermes auxiliary calls bypass `llm_execution`. AgentGuard observes
`pre_auxiliary_call` and `post_auxiliary_call` for titling, compression, vision,
approval review and other tasks sent through the auxiliary client. Each physical
attempt, including retries, contributes cost to the same caps. These hooks
cannot veto a call, even after the hard cap. The next middleware-gated call is
blocked, but auxiliary traffic can continue past the limit. Hermes 0.21.0 does
not emit these hooks, so its auxiliary traffic remains uncounted. Status shows
when the host does not confirm hook support.
Registration skips auxiliary hook names unsupported by the host. The common
manifest still declares both for the latest catalog validator. Hermes 0.21.0's
doctor therefore reports two unknown declared hooks, although its runtime loads
the plugin and supports the remaining features. A single truthful manifest for
the latest catalog cannot pass that older doctor's hook-name allowlist.

Before an auxiliary call, a local price estimate reserves budget. Observed
usage replaces it. A stream without final usage keeps the request estimate;
unknown prices remain explicitly unpriced. Failed attempts release reservations
and record any usage the host supplies, which may omit partial provider billing.
A missing completion hook leaves its reservation held until restart. Calls
seen by both hooks and execution middleware are counted once using the host
request ID and retry number. Deduplication retains the latest 4,096 completed
identities. No request or response content enters the auxiliary receipt.

The public gateway dispatch hook supports allow, skip and incoming-text rewrite,
but no reply action. This plugin does not install a gateway notice or drop a
message through that hook. Normal middleware refusals still return to Hermes.

Hermes does not pass the working directory to middleware, so AgentGuard cannot tell which branch a git command runs on and treats every branch as shared: `git reset --hard` and `git push --force` always ask or deny, on any branch. They deny by default; set `GP003` or `GP004` to `warn` under `guard_rules` to be asked instead.

## Assemble and test offline

From this monorepo:

```bash
node packages/agentguard-spend-hermes/scripts/export-guard-pack.cjs
uv run python packages/agentguard-spend-hermes/scripts/assemble-hermes-repo.py
```

This creates `dist-hermes-repo/` containing the native `plugin.yaml` and root entry point plus the Python package and catalog disclosure. Guard patterns are supplied only by the SDK dependency. Tests, monorepo scripts, virtual environments and caches are excluded. It does not publish anything and refuses to overwrite an existing folder. That folder is the root layout for an eventual `owner/repo` installation.

Run pytest and ruff in an environment containing the local Spend SDK, pytest, ruff and PyYAML. Set `HERMES_TEST_PYTHON` to the installed Hermes venv's Python and `HERMES_TEST_ROOT` to its source folder to include the real-loader smoke. The smoke uses temporary homes, fake provider calls and disabled network sockets. It writes no files to the Hermes installation.

The Node exporter copies rules and matcher constants from `guard-pack.cjs` and `guard-dangerous.cjs`. Tests require the checked-in JSON to match a fresh export and compare Python and Node scans and decisions on the same original fixtures. Rule changes belong in those shared sources first.

## Catalog disclosure

The plugin registers `llm_request`, `llm_execution` and `tool_execution`
middleware; `pre_approval_request`, `post_approval_response`,
`pre_auxiliary_call` and `post_auxiliary_call` hooks; and the `agentguard`
slash command. It registers no model tools or approval transport of its own.
The optional Desktop contribution uses `statusBar.right` and its own GET route.
All declared environment settings are optional: `HOME`, `HERMES_HOME`,
`HERMES_SESSION_KEY`, `HERMES_SESSION_ID`, `HERMES_PLUGIN_HOST_PROCESS`, `AGENTGUARD_DISABLE`,
`AGENTGUARD_HOME`, `AGENTGUARD_LOCALE`, `LC_ALL`, `LC_MESSAGES` and `LANG`.
The last four choose policy language. No provider credential is required by
AgentGuard itself.

Disclosure: the guard pack quotes the commands it blocks and ships intact in
`agentguard-spend`. The Python plugin makes no direct HTTP requests. An
operator-selected Hermes approval transport can contact its configured service
with the content-free approval request. Desktop uses the authenticated SDK GET
route on the connected Hermes gateway for status. There is no analytics upload,
shell command or background process. It reads its policy, signing keys and decision
log; the SDK's local cost overrides; and Hermes's local model cache and approval
context and approval configuration. It writes `policy.yaml`, `private.key`, `public.key`,
`decisions.ndjson` and `decisions.lock` beneath `HERMES_HOME/agentguard`.
It does not read provider keys, browser credentials or another CLI's tokens.
Hermes performs provider and tool calls. Middleware-gated calls require the guard
to allow them; auxiliary calls are observed but cannot be vetoed by their hooks.

The SDK contains optional network APIs that this plugin does not invoke: license
validation and seat registration, an opt-in usage beacon, managed OpenRouter keys,
consent verification, model-catalog refresh, remote workflow receipts, advisor
model calls, key verification, and remote storage or signing adapters. The complete
endpoint, trigger, file and credential inventory is in
[the catalog PR disclosure](docs/catalog-disclosure.md). A synchronous receipt
append may briefly use a worker thread when Hermes already runs an event loop.
Hermes runs an approval transport on a bounded worker thread; a hung transport
can outlive its denied request until that callback exits. No plugin child
process is started. Desktop polls only while the contribution is mounted.

## License

BUSL-1.1. Free for production deployments processing **up to 10,000 enforcement calls per calendar month**. Commercial licensing for higher volumes or service redistribution: `invest@agentguard.run`.

Certain AgentGuard technologies are patent pending in the United States. See https://agentguard.run/patents.
