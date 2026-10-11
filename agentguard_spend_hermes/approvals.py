"""Content-free observation of host approvals. Never changes a host verdict."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from .ledger import arguments_hash, session_digest

logger = logging.getLogger(__name__)
OUTCOMES = {"once": "approved_once", "session": "approved_session",
            "always": "approved_always", "deny": "denied"}
SURFACES = {"cli", "gateway", "agentguard", "mcp-elicitation", "api_server"}
FAILURES = {"timeout", "cancelled", "error", "busy", "invalid", "stale", "interrupted", "unavailable"}
CLI_CHOICE = ContextVar("agentguard_cli_choice", default=None)


def surface_name(surface):
    if isinstance(surface, str) and surface.startswith("transport:"):
        return "transport"
    return surface if surface in SURFACES else "other"


def approval_result(choice):
    if choice in OUTCOMES:
        return OUTCOMES[choice], None
    failure = choice.removeprefix("transport_") if isinstance(choice, str) else "unavailable"
    return "unanswered", failure if failure in FAILURES else "unavailable"


class ApprovalObserver:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.pending = OrderedDict()
        self.owned = {}
        self.lock = threading.Lock()

    @contextmanager
    def capture(self, rule_key):
        result = {}
        with self.lock:
            self.owned["plugin_rule:" + rule_key] = result
        try:
            yield result
        finally:
            with self.lock:
                self.owned.pop("plugin_rule:" + rule_key, None)

    def key(self, context):
        fields = ("request_id", "session_key", "session_id", "turn_id", "tool_call_id", "surface", "pattern_key")
        return arguments_hash({name: context.get(name) for name in fields})

    def pre(self, **context):
        if context.get("surface") == "smart" or context.get("decided_by") == "aux_llm":
            return None
        try:
            key, now = self.key(context), self.clock()
            with self.lock:
                # A duplicate ambiguous identity cannot supply a reliable latency.
                self.pending[key] = None if key in self.pending else now
                self.pending.move_to_end(key)
                while len(self.pending) > 1024:
                    self.pending.popitem(last=False)
        except Exception:
            logger.error("AgentGuard: approval observation could not start")
        return None

    def post(self, *, choice=None, **context):
        if context.get("surface") == "smart" or context.get("decided_by") == "aux_llm":
            return None
        try:
            now, key = self.clock(), self.key(context)
            with self.lock:
                started = self.pending.pop(key, None)
                owned = self.owned.get(context.get("pattern_key"))
                if owned is not None:
                    # The operation receipt below signs this answer once. A
                    # model verdict is never accepted as a human response.
                    if context.get("decided_by") != "aux_llm" and context.get("surface") != "smart":
                        owned.update(choice=choice, surface=surface_name(context.get("surface")))
                    return None
            outcome, failure = approval_result(choice)
            latency = round(max(0, now - started) * 1000) if started is not None else None
            self.runtime.ledger.append({
                "kind": "host_approval", "rule_id": "HOST_APPROVAL",
                "surface": surface_name(context.get("surface")),
                "decision": "allow" if outcome.startswith("approved_") else "deny",
                "reason": "AgentGuard: Hermes approval observed",
                "outcome": outcome, "failure": failure, "latency_ms": latency,
                "session_sha256": session_digest(context),
                # Rules from other plugins may contain commands or paths. Hash them.
                "request_sha256": key,
            })
        except Exception:
            self.runtime.failed = True
            logger.error("AgentGuard: approval receipt could not be signed")
        return None


def request_unattended(runtime, tool_name, reason):
    """Public host contracts only. Never accepts an automatic approval."""
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        # The isolated context does not expose the parent approval channel.
        # Importing a fresh plugin manager here would read the wrong registry.
        return None, "unavailable", "other"
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    selected = ((config.get("security") or {}).get("approval") or {}).get("transport") or "builtin"
    if not isinstance(selected, str):
        raise ValueError("Invalid approval transport")
    selected = selected.strip().lower()
    if selected != "builtin":
        from hermes_cli.approval_transport import ApprovalRequest, invoke_approval_transport
        from hermes_cli.plugins import get_plugin_manager
        from tools.approval import get_current_session_key
        from tools.interrupt import is_interrupted

        registered = get_plugin_manager().get_approval_transport(selected)
        if registered is None:
            return None, "unavailable", "transport"
        timeout = (config.get("approvals") or {}).get("timeout", 300)
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError("Invalid approval timeout")
        rule_key = "agentguard:" + uuid.uuid4().hex
        request = ApprovalRequest.create(
            command="AgentGuard: approve one operation", description=reason,
            pattern_key=rule_key, pattern_keys=(rule_key,),
            session_key=get_current_session_key(default=""), surface="agentguard",
            allow_session=False, allow_permanent=False, timeout_seconds=timeout,
        )
        result = invoke_approval_transport(registered.present, request, timeout_seconds=timeout,
                                           is_interrupted=is_interrupted)
        return result.choice, result.failure, "transport"

    # Built-in messaging approvals use the ordinary public tool-approval API.
    # Each operation gets a fresh rule identity. Require the correlated human
    # response as well as the host's verdict: smart mode, YOLO and allowlists
    # cannot grant this extra AgentGuard approval.
    observer = getattr(runtime, "approvals", None)
    if observer is None:
        return None, "unavailable", "other"
    from tools.approval import request_tool_approval

    rule_key = "agentguard:" + uuid.uuid4().hex
    with observer.capture(rule_key) as answer:
        result = request_tool_approval(tool_name, reason, rule_key=rule_key)
        choice = answer.get("choice")
        if not isinstance(result, dict) or result.get("approved") is not True:
            choice = "deny" if choice == "once" else choice
        return choice, None if answer else "unavailable", answer.get("surface", "other")


def ask(runtime, tool_name, reason, interactive, prompt):
    started = time.monotonic()
    token = CLI_CHOICE.set(None)
    try:
        if interactive:
            accepted = prompt(tool_name, reason)
            choice = CLI_CHOICE.get() or ("once" if accepted else "deny")
            failure, surface = None, "cli"
        else:
            choice, failure, surface = request_unattended(runtime, tool_name, reason)
        outcome, inferred = approval_result(choice)
        failure = failure or inferred
        if failure is not None and failure not in FAILURES:
            failure = "error"
        allowed = choice == "once" and failure is None
        if failure:
            outcome = "unanswered"
        # Session or permanent choices offered by an older built-in UI cannot
        # grant this call or any later AgentGuard operation.
        if choice in {"session", "always"}:
            outcome, failure = "denied", "invalid"
    except ImportError:
        allowed, outcome, failure, surface = False, "unanswered", "unavailable", "other"
    except Exception:
        allowed, outcome, failure, surface = False, "unanswered", "error", "other"
    finally:
        CLI_CHOICE.reset(token)
    return allowed, {"outcome": outcome, "failure": failure, "surface": surface,
                     "latency_ms": round(max(0, time.monotonic() - started) * 1000)}
