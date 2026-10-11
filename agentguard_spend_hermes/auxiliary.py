"""Observer-only auxiliary accounting, sharing the existing spend windows."""

from __future__ import annotations

import logging
from collections import OrderedDict

from .ledger import arguments_hash, session_digest, sync
from .providers import accounting, list_price, token_count

logger = logging.getLogger(__name__)
TASKS = {"titling", "title", "compression", "vision", "approval", "moa", "moa_advisor", "moa_aggregator",
         "session_title", "smart_approval", "mcp_sampling"}


def attempt_key(context):
    request_id = context.get("api_request_id")
    retry = context.get("retry_count", 0)
    if isinstance(request_id, str) and request_id:
        return arguments_hash({"request": request_id, "retry": token_count(retry)})
    started = context.get("started_at")
    if type(started) in (int, float) and started > 0:
        return arguments_hash({"started": started, "session": context.get("session_id"),
                               "task": context.get("aux_task"), "retry": token_count(retry)})
    return None


class AuxiliaryAccounting:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = {}
        self.recent = OrderedDict()
        self.supported = None

    def remember(self, key, owner):
        if key:
            self.recent[key] = owner
            self.recent.move_to_end(key)
            while len(self.recent) > 4096:
                self.recent.popitem(last=False)

    def middleware(self, context):
        """Called at the execution gate, before its own cap check/reservation."""
        key = attempt_key(context)
        if key:
            with self.runtime.model_lock:
                pending = self.pending.pop(key, None)
                if pending:
                    self.runtime.release(pending["reservation"])
                self.remember(key, "middleware")

    def pre(self, **context):
        try:
            self.supported = True
            key = attempt_key(context)
            if key is None:
                return None  # Post can still count usage; cannot correlate a reservation.
            model = context.get("model", "")
            inputs = token_count(context.get("approx_input_tokens") or 0)
            outputs = token_count(context.get("max_tokens") or 4096)
            estimate = list_price(model, inputs, outputs) or 0
            with self.runtime.model_lock:
                if key in self.pending or key in self.recent:
                    return None
                if len(self.pending) >= 4096:
                    raise ValueError("Auxiliary accounting capacity reached")
                reached = any(cap.action == "block" and (
                    self.runtime.spend(cap) >= cap.amountCents
                    or self.runtime.spend(cap) + estimate > cap.amountCents
                ) for cap in self.runtime.policy.caps)
                for window in self.runtime.windows():
                    self.runtime.reserved[window] = self.runtime.reserved.get(window, 0) + estimate
                self.pending[key] = {"reservation": estimate, "hard_cap_reached": reached}
        except Exception:
            self.runtime.failed = True
            logger.error("AgentGuard: auxiliary accounting failed; the next model gate is closed")
        # Hermes explicitly ignores all auxiliary hook return values.
        return None

    def post(self, **context):
        try:
            from .middleware import identifier

            key = attempt_key(context)
            with self.runtime.model_lock:
                if key and key in self.recent:
                    return None
                pending = self.pending.pop(key, {})
                reservation = pending.get("reservation", 0)
                self.runtime.release(reservation)
                failed = context.get("error") is not None or context.get("error_type") is not None
                model = identifier(context.get("response_model") or context.get("model"))
                # The host's canonical prompt_tokens already includes cache tokens.
                # Do not read request, response, system_prompt or error contents.
                cents, source, usage = accounting({"usage": context.get("usage")}, model)
                basis = "usage"
                if source == "unpriced" and reservation and not failed:
                    cents, source, basis = reservation, "api_list_price_estimate", "request_estimate"
                for window in self.runtime.windows():
                    sync(self.runtime.config.spendStore.increment_window_spend(self.runtime.scope_key, window, cents))
                self.runtime.ledger.append({
                    "kind": "auxiliary_call", "rule_id": "AUXILIARY_CALL", "decision": "allow",
                    "reason": "AgentGuard: auxiliary call observed; hook cannot veto",
                    "cost_cents": cents, "cost_source": source, "cost_basis": basis, "usage": usage,
                    "status": "failed" if failed else "completed", "coverage": "observer_only",
                    "hard_cap_reached": pending.get("hard_cap_reached", False),
                    "aux_task": context.get("aux_task") if context.get("aux_task") in TASKS else "other",
                    "attempt_sha256": key, "session_sha256": session_digest(context),
                }, cost=cents, model=model, provider=identifier(context.get("provider")))
                self.remember(key, "observer")
        except Exception:
            self.runtime.failed = True
            logger.error("AgentGuard: auxiliary receipt or accounting failed; the next model gate is closed")
        return None
