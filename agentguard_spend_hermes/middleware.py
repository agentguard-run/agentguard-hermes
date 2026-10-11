"""Hermes middleware gates. Pre-call failures return refusals, never raise.

Hermes intentionally advances its middleware chain on a callback exception
before next_call. Returning a local refusal is necessary for a closed gate.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading

import yaml
from agentguard_spend import build_scope_key
from agentguard_spend.guards import RULES, decision_for, scan_guard_pack, scan_code
from agentguard_spend.policy import _validate_policy
from agentguard_spend.store_memory import WINDOW_DURATION_MS

from . import catalog
from .approvals import CLI_CHOICE, ask
from .auxiliary import AuxiliaryAccounting
from .ledger import Ledger, arguments_hash, session_digest, sync
from .limits import DelegationLimit, launch_count
from .providers import accounting, projected_cost
from .refusals import blocked_response, blocked_tool, model_key, model_of

logger = logging.getLogger(__name__)
UNATTENDED = "AgentGuard: this needs a person's approval"
READ_ONLY = frozenset(
    {
        "read_file",
        "search_files",
        "list_files",
        "list_directory",
        "web_search",
        "web_extract",
        "browser_snapshot",
        "memory_search",
        "session_search",
        "session_read",
    }
)
WRITE_TOOLS = frozenset({"write_file", "patch", "edit_file", "apply_patch"})
TERMINAL_TOOLS = frozenset({"terminal", "bash", "shell", "exec_command", "run_command"})
CODE_TOOLS = frozenset({"execute_code", "python", "run_code"})
# Soft-cap targets for old policies that only name a Claude model.
DEFAULT_TARGETS = {
    "openrouter": "anthropic/claude-sonnet-4.6",
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5-mini",
    "gemini": "gemini-3.1-flash-lite-preview",
}


def tool_kind(name):
    name = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) else ""
    if name in TERMINAL_TOOLS:
        return "terminal"
    if name in CODE_TOOLS:
        return "code"
    if name in WRITE_TOOLS:
        return "write"
    if name == "delegate_task":
        return "delegate"
    if name == "process":
        return "stdin"
    return "read" if name in READ_ONLY else "other"


def identifier(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:@+-]{1,256}", value)
        else "unknown"
    )


def hermes_interactive():
    from tools import approval

    # Context-local Hermes markers take priority over inherited environment.
    unattended = any(
        getattr(approval, name, lambda: False)()
        for name in (
            "_is_cron_approval_context",
            "_is_gateway_approval_context",
            "_is_unattended_platform_approval_context",
            "_is_single_query_approval_context",
        )
    )
    return (
        not unattended
        and approval._is_interactive_cli()
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def ask_person(tool_name, reason):
    from tools import approval

    # Never show raw arguments in our prompt or log. Hermes resolves its own
    # thread-local CLI callback, including its normal approval timeout.
    answer = approval.prompt_dangerous_approval(
        f"AgentGuard: approve one {tool_name} call",
        reason,
        allow_permanent=False,
        allow_session=False,
        approval_callback=approval._resolve_cli_approval_callback(),
    )
    CLI_CHOICE.set(answer)
    return answer == "once"


def native_decision(context):
    """Honor host decision envelopes if present; unknown envelopes are closed."""
    rank = {"allow": 0, "ask": 1, "deny": 2}
    result = "allow"
    for key in ("decision", "permission_decision", "approval", "hermes_decision"):
        if key not in context:
            continue
        value = context[key]
        if isinstance(value, dict):
            value = value.get("decision", value.get("permissionDecision"))
        value = (
            {
                "approved": "allow",
                "denied": "deny",
                "block": "deny",
                "pending": "ask",
            }.get(value, value)
            if isinstance(value, str)
            else None
        )
        if value not in rank:
            return "deny"
        if rank[value] > rank[result]:
            result = value
    return result


# Hermes reports its own command block or pending approval in `status`.
HOST_STATUS = {"blocked": "deny", "pending_approval": "ask", "approval_required": "ask"}


def host_restriction(kind, result):
    """Hermes's own deny or ask in a tool result, for the audit log only.

    Only tools whose results Hermes shapes itself are read. Web, MCP and other
    results carry outside data whose keys mean nothing here.
    """
    if kind not in ("terminal", "code", "write", "stdin"):
        return None
    host = result
    if isinstance(result, str):
        try:
            host = json.loads(result)
        except (ValueError, RecursionError):
            return None
    if not isinstance(host, dict):
        return None
    status = host.get("status")
    found = [
        native_decision(host),
        HOST_STATUS.get(status, "allow") if isinstance(status, str) else "allow",
    ]
    decision = "deny" if "deny" in found else "ask" if "ask" in found else "allow"
    return None if decision == "allow" else decision


def openrouter_claude(model):
    """OpenRouter writes Claude versions with a dot, as Hermes's catalog does:
    claude-sonnet-4-6 there is anthropic/claude-sonnet-4.6."""
    return "anthropic/" + re.sub(r"^(claude-[a-z]+-\d+)-(\d{1,2})$", r"\1.\2", model)


class Runtime:
    def __init__(self, policy, paths, config):
        _validate_policy(policy)
        for cap in policy.caps:
            if cap.window not in WINDOW_DURATION_MS:
                raise ValueError("Unknown spend window")
        self.policy, self.config = policy, config
        self.scope_key = build_scope_key(policy.scope)
        raw = yaml.safe_load(paths["policy"].read_text())
        self.rules = raw.get("guard_rules", {})
        if not isinstance(self.rules, dict) or any(
            key not in RULES or value not in ("stop", "warn", "off")
            for key, value in self.rules.items()
        ):
            raise ValueError("Invalid guard rules")
        self.downgrades = raw.get("model_downgrades", {})
        if not isinstance(self.downgrades, dict) or any(
            not isinstance(v, str) or not v for v in self.downgrades.values()
        ):
            raise ValueError("Invalid model downgrades")
        self.limits = DelegationLimit(raw.get("delegation", {}))
        self.ledger = Ledger(
            config.logStore.path,
            policy,
            config.signingKeys.privateKey,
            config.signingKeys.publicKey,
        )
        self.model_lock = threading.RLock()
        self.reserved = {}
        self.failed = False
        self.no_swap_noted = set()
        self.auxiliary = AuxiliaryAccounting(self)

    def spend(self, cap):
        # Recorded spend plus estimates held by calls still in flight. Called
        # with model_lock held.
        if cap.window == "per_call":
            return 0
        recorded = sync(
            self.config.spendStore.get_window_spend(self.scope_key, cap.window)
        )
        return recorded + self.reserved.get(cap.window, 0)

    def cap_for(self, request, action):
        cost = projected_cost(request)
        for cap in self.policy.caps:
            before = self.spend(cap)
            if cap.action == action and (
                before + cost > cap.amountCents
                or cap.window != "per_call"
                and before >= cap.amountCents
            ):
                return cap, before
        return None, 0

    def validate_request(self, request):
        if not isinstance(request, dict) or identifier(model_of(request)) == "unknown":
            raise ValueError("Invalid model request")
        projected_cost(request)

    def swap_target(self, provider, cap):
        if provider in self.downgrades:
            return self.downgrades[provider]
        target = cap.downgradeTo
        if not isinstance(target, str) or not target:
            return None
        # Carry an old policy's Claude-only target to the provider in use.
        if provider == "openrouter" and target.startswith("claude-"):
            return openrouter_claude(target)
        if provider in ("openai", "gemini") and target.startswith(
            ("claude-", "anthropic/")
        ):
            return DEFAULT_TARGETS[provider]
        return target

    def downgrade(self, request, provider, base_url=""):
        cap, _ = self.cap_for(request, "downgrade")
        if not cap or self.policy.mode == "shadow":
            return request
        provider = catalog.provider_for(provider, base_url)
        current = model_of(request)
        target = self.swap_target(provider, cap)
        if target == current:
            return request
        # Swap only to an id the provider's local catalog lists, and only when
        # it costs less per input and per output token. Otherwise keep the
        # model and let the hard cap stop spending.
        if (
            target
            and catalog.served(provider, target)
            and catalog.cheaper(provider, current, target)
        ):
            return {**request, model_key(request): target}
        if (provider, current) not in self.no_swap_noted:
            self.no_swap_noted.add((provider, current))
            logger.warning(
                "AgentGuard: soft spend cap reached, but no cheaper model is mapped "
                "for %s, so %s stays until the hard cap.",
                identifier(provider),
                identifier(current),
            )
        return request

    def llm_request(self, *, request=None, **context):
        try:
            self.validate_request(request)
            with self.model_lock:
                effective = self.downgrade(
                    request, context.get("provider", ""), context.get("base_url", "")
                )
            if model_of(effective) != model_of(request):
                return {
                    "request": effective,
                    "source": "AgentGuard",
                    "reason": "AgentGuard soft spend cap",
                }
        except Exception as error:
            # llm_execution revalidates and blocks. Do not let a request hook
            # failure erase the execution gate or expose input in an exception.
            logger.error("AgentGuard request gate error: %s", type(error).__name__)
        return None

    def record_model(
        self,
        request,
        context,
        decision,
        rule_id,
        reason,
        *,
        cost=0,
        source="unpriced",
        usage=None,
        status="blocked",
        before=0,
        cap=None,
    ):
        original = context.get("original_request")
        model = identifier(model_of(request))
        original_model = (
            identifier(model_of(original) or model)
            if isinstance(original, dict)
            else model
        )
        receipt = {
            "kind": "model_call",
            "rule_id": rule_id,
            "decision": decision,
            "reason": reason,
            "cost_source": source,
            "cost_cents": cost,
            "usage": usage or {},
            "status": status,
            "session_sha256": session_digest(context),
        }
        try:
            self.ledger.append(
                receipt,
                model=model,
                original_model=original_model,
                provider=identifier(context.get("provider")),
                cost=cost,
                before=before,
                cap=cap,
            )
        except Exception:
            # A model call that cannot be signed must not be followed by
            # unsigned ones. This latch lasts until Hermes restarts.
            self.failed = True
            raise

    def model_error(self, request, context, error, *, latch=True):
        # Latch only when spend or signing state is unknown (an accounting or
        # log failure). A malformed request denies that call alone; latching
        # on it would let one odd payload stop every later call.
        if latch:
            self.failed = True
        reason = "AgentGuard: model gate error"
        logger.error("%s (%s)", reason, type(error).__name__)
        try:
            self.record_model(request, context, "deny", "AG_ERROR", reason)
        except Exception as log_error:
            logger.error("AgentGuard decision log error: %s", type(log_error).__name__)
        model = identifier(model_of(request))
        return blocked_response(reason, model, context.get("api_mode", ""))

    def windows(self):
        return {cap.window for cap in self.policy.caps if cap.window != "per_call"}

    def reserve(self, request):
        """Hold the call's estimated cost in every window until it settles."""
        cost = projected_cost(request)
        for window in self.windows():
            self.reserved[window] = self.reserved.get(window, 0) + cost
        return cost

    def release(self, cost):
        for window in self.windows():
            self.reserved[window] = max(0, self.reserved.get(window, 0) - cost)

    def llm_execution(self, *, request=None, next_call=None, **context):
        # The lock covers only the decision with its reservation, and later the
        # settlement. Reserved estimates count as spent, so calls running at
        # the same time cannot all see the same remaining budget, and no call
        # waits for another call's provider round trip. Child agents share
        # this registration.
        with self.model_lock:
            try:
                self.auxiliary.middleware(context)
                self.validate_request(request)
                if self.failed or not callable(next_call):
                    raise ValueError("Unavailable model gate")
                if native_decision(context) != "allow":
                    reason = "AgentGuard: Hermes approval is required or denied"
                    self.record_model(
                        request, context, "deny", "HERMES_APPROVAL", reason
                    )
                    return blocked_response(
                        reason, model_of(request), context.get("api_mode", "")
                    )
                if self.policy.requiredCapability:
                    reason = "AgentGuard: required spending capability is unavailable"
                    self.record_model(
                        request, context, "deny", "SPEND_CAPABILITY", reason
                    )
                    return blocked_response(
                        reason, model_of(request), context.get("api_mode", "")
                    )
                request = self.downgrade(
                    request, context.get("provider", ""), context.get("base_url", "")
                )
                cap, before = self.cap_for(request, "block")
                if cap and self.policy.mode != "shadow":
                    reason = "AgentGuard: hard spend cap reached"
                    self.record_model(
                        request,
                        context,
                        "deny",
                        "SPEND_HARD_CAP",
                        reason,
                        before=before,
                        cap=cap,
                    )
                    return blocked_response(
                        reason, model_of(request), context.get("api_mode", "")
                    )
                reserved = self.reserve(request)
            except Exception as error:
                return self.model_error(request, context, error, latch=False)
        try:
            self.limits.begin()
            try:
                response = next_call(request)
            except Exception:
                # The host marks downstream errors separately. Re-raise them
                # without any retry, response text or exception text in our log.
                try:
                    self.record_model(
                        request,
                        context,
                        "allow",
                        "MODEL_PROVIDER_ERROR",
                        "AgentGuard: provider call failed",
                        status="failed",
                    )
                except Exception as error:
                    self.failed = True
                    logger.error(
                        "AgentGuard decision log error: %s", type(error).__name__
                    )
                raise
            finally:
                self.limits.end()
            try:
                cents, source, usage = accounting(response, model_of(request))
                with self.model_lock:
                    self.release(reserved)
                    reserved = None
                    for window in self.windows():
                        sync(
                            self.config.spendStore.increment_window_spend(
                                self.scope_key, window, cents
                            )
                        )
                original = context.get("original_request")
                downgraded = isinstance(original, dict) and (
                    model_of(original) != model_of(request)
                )
                self.record_model(
                    request,
                    context,
                    "downgrade" if downgraded else "allow",
                    "SPEND_SOFT_CAP" if downgraded else "MODEL_ALLOW",
                    "AgentGuard: model call recorded",
                    cost=cents,
                    source=source,
                    usage=usage,
                    status="completed",
                )
                return response
            except Exception as error:
                return self.model_error(request, context, error)
        finally:
            # A failed, interrupted or cancelled call spends nothing it had
            # reserved. BaseException paths (KeyboardInterrupt, cancellation)
            # pass through here too.
            if reserved is not None:
                with self.model_lock:
                    self.release(reserved)

    def scan_tool(self, tool_name, args, context):
        kind = tool_kind(tool_name)
        if kind == "terminal":
            if not isinstance(args.get("command"), str):
                raise ValueError("Missing terminal command")
            tool, payload = "Bash", args
        elif kind == "code":
            if not isinstance(args.get("code"), str):
                raise ValueError("Missing code")

            return scan_code(args["code"], context)
        elif kind == "write":
            if not isinstance(
                args.get("path", args.get("file_path", args.get("patch"))), str
            ):
                raise ValueError("Missing write target")
            tool, payload = "Write", args
        elif kind == "delegate":
            tool, payload = "Task", args
        elif kind == "stdin" and args.get("action") in ("write", "submit"):
            # Hermes's process tool types text into a background process,
            # often a shell started with terminal(background=true). Check that
            # text with the terminal rules so it is not a way around them.
            if not isinstance(args.get("data", ""), str):
                raise ValueError("Missing process input")
            tool, payload = "Bash", {"command": args.get("data", "")}
        else:
            tool, payload = tool_name, args
        return scan_guard_pack(tool, payload, {"cwd": context.get("cwd")})

    def record_tool(
        self,
        tool_name,
        args,
        kind,
        decision,
        rule_ids,
        reason,
        *,
        gate_decision=None,
        launches=0,
        stage="gate",
        approval=None,
        context=None,
    ):
        try:
            digest = arguments_hash(args)
        except Exception:
            # No repr(args): even a malformed object could contain a secret.
            digest = arguments_hash({"invalid_payload": True})
        self.ledger.append(
            {
                "kind": "delegate" if kind == "delegate" else "tool_call",
                "tool_name": identifier(tool_name),
                "arguments_sha256": digest,
                "rule_id": rule_ids[0],
                "rule_ids": rule_ids,
                "decision": decision,
                "gate_decision": gate_decision or decision,
                "reason": reason,
                "launches": launches,
                "stage": stage,
                "session_sha256": session_digest(context),
                **({"approval": approval} if approval else {}),
            }
        )

    def tool_execution(self, *, tool_name=None, args=None, next_call=None, **context):
        kind = tool_kind(tool_name)
        # Holding the launch lock through approval and reservation prevents two
        # parallel batches from spending the same last launch. Release it before
        # invoking next_call so nested child model and tool calls can proceed.
        with self.limits.lock:
            try:
                self.limits.touch()
                if (
                    identifier(tool_name) == "unknown"
                    or not isinstance(args, dict)
                    or not callable(next_call)
                ):
                    raise ValueError("Invalid tool middleware payload")
                arguments_hash(
                    args
                )  # Reject cycles and non-JSON values before the gate.
                native = native_decision(context)
                if native != "allow":
                    reason = "AgentGuard: Hermes approval is required or denied"
                    self.record_tool(
                        tool_name, args, kind, native, ["HERMES_APPROVAL"], reason, context=context
                    )
                    return blocked_tool(reason, "HERMES_APPROVAL", native)
                scan = self.scan_tool(tool_name, args, context)
                original = context.get("original_args", args)
                if not isinstance(original, dict):
                    raise ValueError("Invalid original tool arguments")
                if original != args:
                    original_scan = self.scan_tool(tool_name, original, context)
                    scan["ruleIds"] = sorted(
                        set(scan["ruleIds"] + original_scan["ruleIds"])
                    )
                    if original_scan.get("reason") == "guard_scan_incomplete":
                        scan["reason"] = "guard_scan_incomplete"
                decision = decision_for(scan, self.rules)
                count = launch_count(args) if kind == "delegate" else 0
                ids = scan["ruleIds"] or ["TOOL_ALLOW"]
                reason = "AgentGuard: tool call allowed"
                if decision == "deny":
                    reason = "AgentGuard: blocked by " + ", ".join(ids)
                elif decision == "ask":
                    ids = scan["ruleIds"] or ["AG_SCAN_INCOMPLETE"]
                    reason = "AgentGuard: " + (
                        " ".join(
                            RULES[rule_id]["reason"] for rule_id in scan["ruleIds"]
                        )
                        or "This tool call could not be fully checked."
                    )
                if count and self.limits.exceeded(count) and decision != "deny":
                    decision, ids = "ask", ["DELEGATE_LIMIT"]
                    reason = "AgentGuard: sub-agent launch limit reached"
                gate_decision = decision
                approval = None
                if decision == "ask":
                    accepted, approval = ask(self, tool_name, reason, hermes_interactive(), ask_person)
                    if approval["failure"] == "unavailable":
                        decision, reason = "deny", UNATTENDED
                    elif accepted:
                        decision, reason = "allow", "AgentGuard: approved for this call"
                    else:
                        decision, reason = (
                            "deny",
                            "AgentGuard: approval was not granted",
                        )
                self.record_tool(
                    tool_name,
                    args,
                    kind,
                    decision,
                    ids,
                    reason,
                    gate_decision=gate_decision,
                    launches=count if decision == "allow" else 0,
                    approval=approval,
                    context=context,
                )
                if decision != "allow":
                    return blocked_tool(reason, ids[0])
                if count:
                    self.limits.reserve(count)
            except Exception as error:
                # Unclassified tools may have effects, so only known reads are
                # allowed when gate state is broken. Host checks still run.
                native = native_decision(context)
                decision = (
                    "allow"
                    if kind == "read" and native == "allow" and callable(next_call)
                    else "deny"
                )
                reason = "AgentGuard: tool gate error"
                logger.error("%s (%s)", reason, type(error).__name__)
                try:
                    self.record_tool(
                        tool_name, args, kind, decision, ["AG_ERROR"], reason, context=context
                    )
                except Exception as log_error:
                    logger.error(
                        "AgentGuard decision log error: %s", type(log_error).__name__
                    )
                if decision != "allow":
                    return blocked_tool(reason, "AG_ERROR")
        self.limits.begin()
        try:
            # Preserve the host's result verbatim, including an approval or deny
            # produced by a guard inside next_call. Never call the handler again.
            result = next_call(args)
            restriction = host_restriction(kind, result)
            if restriction:
                try:
                    self.record_tool(
                        tool_name,
                        args,
                        kind,
                        restriction,
                        ["HERMES_APPROVAL"],
                        "AgentGuard: Hermes returned a restricted result",
                        stage="host_result",
                        context=context,
                    )
                except Exception as error:
                    logger.error(
                        "AgentGuard decision log error: %s", type(error).__name__
                    )
            return result
        finally:
            self.limits.end()
