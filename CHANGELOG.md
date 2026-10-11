## Unreleased

## 0.4.1 (2026-10-11)

- Declare the environment variables the plugin reads as `optional_env`, so `hermes plugins install` no longer prompts for values Hermes sets itself, such as `HERMES_SESSION_KEY`.
- Point the package Source link at the public repository, github.com/agentguard-run/agentguard-hermes.

## 0.4.0 (2026-10-11)

- Install guidance for Hermes v0.21.6 and later, which run from a managed environment: install from the AgentGuard repository with `hermes plugins install`, or install the PyPI package into the managed environment. Requires agentguard-spend 0.16.0, the first release with `agentguard_spend.guards`.
- Exercise commands, transported approvals, native observations, auxiliary events
  and the Desktop backend through offline host smokes. Skip unsupported auxiliary
  registrations on older hosts while keeping the complete latest catalog
  declaration; document the 0.21.0 doctor's unknown-declaration limitation.

- Add an optional Desktop status chip and popover through the public SDK and
  an authenticated, read-only backend route. Discard stale profile responses
  and stop polling on unmount. Report unavailable counters or approval channels
  under plugin-host isolation rather than reading a duplicate runtime.

- Account for auxiliary provider attempts through the public observation hooks,
  deduplicate calls also seen by execution middleware, and sign cost-source
  metadata. Auxiliary hooks cannot veto calls; document this cap limitation and
  the absence of these events on Hermes 0.21.0. Leave gateway dispatch unchanged
  because its public return contract has no reply action.

- Route unattended ASK decisions through public Hermes approval transports or
  its messaging approval bridge. Accept only a correlated human once response;
  silence, stale replies, errors and automatic approvals deny the operation.
  Keep the exact no-transport denial and sign answer metadata in the operation
  receipt without duplicating a host approval observation.

- Observe Hermes approval requests and responses without changing their verdict.
  Sign the outcome, surface and answer latency in the existing chain. Unknown
  or missing responses stay unanswered; host session and permanent approvals
  remain distinguishable from one-operation approvals.

- Add the public /agentguard status, log and verify commands. Show UTC spend
  windows, source counts, live helper limits and a since-midnight pace estimate.
  Filter session logs by hashed host identity and verify signatures before
  summarizing decisions. Never show prompts, completions or raw arguments.

- Import the unchanged local guard matchers and regenerated rule pack from the
  Python SDK's public guards module. Keep Node and Python fixture parity.
- Remove legacy client wrappers. Require Hermes 0.16.0 or newer and preserve
  setup lockdown through public middleware. An unexpectedly missing middleware
  API stops initialization instead of falling back to unguarded clients.
- Declare all middleware and optional environment settings, bound dependencies,
  and add an assembled repository layout, draft catalog entry and disclosure.
- Enable the scanner in the offline installer smoke. Simulate missing SDK
  imports without changing the installed Hermes environment.

## 0.3.0 (2026-10-07)

- Use Hermes request and execution middleware for provider-independent spend caps and cost accounting. Keep the 0.2.x wrappers only when middleware is unavailable, with the active path in the startup log.
- Guard terminal, Python code and file writes with exported AgentGuard rule data, including credential uploads. Require one-operation Hermes approval for ASK and deny unattended ASK decisions.
- Limit delegation to 15 launches per 15 active minutes and 40 per 120 active minutes, configurable in policy.yaml.
- Write model, tool and delegation receipts to the existing signed Spend chain without prompts, completions or raw arguments.
- Add a native plugin manifest, public repository assembly script, Python/Node fixture parity tests and an offline real-loader smoke.
- Load from PyPI: the `hermes_agent.plugins` entry point names the module, since Hermes looks up `register` on what the entry point loads.
- Use manifest_version 1, the highest that `hermes plugins install` in Hermes 0.21.0 accepts.
- Guard Bedrock Converse calls, which name the model `modelId`, instead of refusing them.
- Deny a malformed model request without closing the model gate for later calls.
- Check text sent to a background process with Hermes's `process` tool against the terminal rules.
- Find the signed chain's head from the end of the log, so appends stay fast as the log grows.
- Use OpenRouter's dotted Claude ids for the soft-cap swap (`anthropic/claude-sonnet-4.6`), as Hermes's own model catalog does.
- Swap at the soft cap only to a model the provider's local catalog lists and that costs less per input and output token; otherwise keep the model, log one line and leave it to the hard cap.
- Reserve each model call's estimated cost under a short lock and run the provider call without it, so parallel sessions and sub-agents no longer wait for each other or for an approval prompt.
- Register a fail-closed lockdown before any setup step. If setup fails, every model and tool call is refused with a message naming the fix, instead of Hermes running without AgentGuard.

## 0.2.3 (2026-08-30)

- Release the corrected package notices and license scope with the Python SDK
  seat-counting and telemetry fixes (`35bc64d`, `90e92c6`).

## 0.2.2 (2026-08-30)

- Remove application-number enumeration from package notice surfaces (`e7aee6f`).

## 0.2.1

- Removed the enumerated patent application numbers from the LICENSE and the shipped module header in favour of the generic patent-pending notice, and corrected the trademark notice to registered (U.S. Reg. No. 8281464). The LICENSE also carried the wrong product name and scope, copied from the Python SDK.

# Changelog

## 0.2.0

Two behaviour changes that are breaking, and are the point of the release.

- **Fails closed.** If wrapping the Anthropic or OpenAI client raises, the plugin now raises instead of returning the raw, unguarded client. Previously a wrap failure logged a warning and left Hermes running with no spend enforcement at all, which is the opposite of what this package is for. The same applies to setup: a bootstrap or policy-load failure now raises rather than leaving the agent ungoverned. `AGENTGUARD_DISABLE=1` remains the deliberate opt-out and still returns unguarded clients.
- **Spend caps can actually fire.** The spend store was rebuilt on every client construction, so the window reset each time and `per_day` and `per_minute` caps could never trigger. The store is now memoized per process, keyed by home directory and policy scope so separate tenants and separate `HERMES_HOME` values never share a bucket.

Fixes:

- The Ed25519 private key is written 0600 inside a 0700 directory, and never briefly world-readable. It was previously written with the default umask.
- The signed audit log the README describes is actually written. The decision store was in-memory, so `~/.hermes/agentguard/decisions.ndjson` never existed and every receipt died with the process.
- A cap carrying an unknown key (such as the `scope:` field the old README documented, which `SpendCap` does not have) now logs a warning and keeps enforcing, instead of raising `TypeError` out of `register()` and breaking plugin load.
- Dependency floor raised from `agentguard-spend>=0.1.4` to `>=0.15.2`, so a resolver cannot install an SDK missing everything the plugin imports.
- README corrected: per-agent budgets need a separate `HERMES_HOME` per bridge, not just `HERMES_AGENT_ID`, because enforcement keys on the policy scope.
