"""Read-only commands over signed, content-free decisions and live counters."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from agentguard_spend import GENESIS_PREVIOUS_HASH, verify_entry
from agentguard_spend.guards import RULES
from agentguard_spend.store_filesystem import _entry_from_dict

from .ledger import PROCESS_TOKEN, session_digest, sync

USAGE = "Usage: /agentguard [status | log 24h | log 7d | log session | verify]"
SOURCES = {"gateway_reported": "reported", "api_list_price_estimate": "estimated", "unpriced": "unpriced"}
KINDS = {"model_call", "tool_call", "delegate", "approval", "host_approval", "auxiliary_call"}
RULE_IDS = set(RULES) | {
    "TOOL_ALLOW", "AG_ERROR", "AG_SCAN_INCOMPLETE", "HERMES_APPROVAL", "DELEGATE_LIMIT",
    "SPEND_CAPABILITY", "SPEND_HARD_CAP", "SPEND_SOFT_CAP", "MODEL_ALLOW",
    "MODEL_PROVIDER_ERROR", "HOST_APPROVAL", "AUXILIARY_CALL", "AG_APPROVAL",
}


def money(cents):
    return f"${cents / 100:.2f}"


def records(ledger):
    """Read a complete prefix. Appends may continue; malformed rows never disappear."""
    try:
        stream = ledger.path.open("rb")
    except FileNotFoundError:
        return
    with stream:
        # An append is serialized by the same lock as Ledger.append.
        import fcntl
        with ledger.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            end = stream.seek(0, 2)
            stream.seek(0)
            fcntl.flock(lock, fcntl.LOCK_UN)
        while stream.tell() < end:
            line = stream.readline(end - stream.tell())
            if not line.endswith(b"\n"):
                raise ValueError("Incomplete receipt")
            yield json.loads(line)


def verified_records(ledger):
    previous = GENESIS_PREVIOUS_HASH
    for sequence, row in enumerate(records(ledger)):
        entry = _entry_from_dict(row)
        if (entry.sequence != sequence or entry.previousHash != previous
                or not sync(verify_entry(entry, ledger.public_key))):
            raise ValueError("Invalid receipt chain")
        previous = entry.entryHash
        yield row


def utc_now():
    return datetime.now(timezone.utc)


def status_data(runtime, now=None):
    now = now or utc_now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    mix = {name: {"calls": 0, "cents": 0} for name in SOURCES.values()}
    recorded_today = 0
    for row in records(runtime.ledger):
        decision = row["decision"]
        receipt = decision.get("governanceReceipt") or {}
        stamp = datetime.fromisoformat(decision["timestamp"])
        if not midnight <= stamp <= now or receipt.get("status") not in {"completed", "failed"}:
            continue
        if receipt.get("kind") not in {"model_call", "auxiliary_call"}:
            continue
        cents = receipt.get("cost_cents", 0)
        recorded_today += cents
        if receipt.get("process_token") == PROCESS_TOKEN:
            source = SOURCES.get(receipt.get("cost_source"), "unpriced")
            mix[source]["calls"] += 1
            mix[source]["cents"] += cents
    with runtime.model_lock:
        caps = [{"window": cap.window, "action": cap.action, "limit": cap.amountCents,
                 "spent": runtime.spend(cap)} for cap in runtime.policy.caps]
    return {"caps": caps, "mix": mix, "helpers": runtime.limits.snapshot(),
            "recorded_today": recorded_today, "failed": runtime.failed,
            "mode": runtime.policy.mode, "now": now, "midnight": midnight,
            "auxiliary_supported": runtime.auxiliary.supported}


def render_status(data):
    lines = ["AgentGuard status", "Spend counters: this process, UTC windows."]
    if data["failed"]:
        lines.append("Lockdown: accounting or signing failed. Restart after fixing the cause.")
    lines.append(f"Policy mode: {data['mode']}")
    labels = {("per_day", "downgrade"): "Today, soft cap",
              ("per_day", "block"): "Today, hard cap", ("per_minute", "block"): "Per-minute cap"}
    hard = None
    for cap in data["caps"]:
        label = labels.get((cap["window"], cap["action"]), f"{cap['window']} {cap['action']}")
        lines.append(f"{label}: {money(cap['spent'])} of {money(cap['limit'])} (includes reservations)")
        if cap["window"] == "per_day" and cap["action"] == "block":
            hard = cap if hard is None or cap["limit"] < hard["limit"] else hard
    lines.append("Cost sources today: " + "; ".join(
        f"{key}: {item['calls']} calls, {money(item['cents'])}" for key, item in data["mix"].items()))
    lines.append("Unpriced calls have unknown cost. They are not free.")
    lines.append("Auxiliary calls: observed, not blocked by their hooks." if data["auxiliary_supported"]
                 else "Auxiliary hooks unavailable or unconfirmed on this host; coverage may be incomplete.")
    lines.append(f"Recorded today across processes: {money(data['recorded_today'])}")
    for label, item in data["helpers"].items():
        lines.append(f"Helpers, {label}: {item['used']} of {item['limit']} in {item['minutes']} active minutes")
    # A simple since-midnight rate, using settled costs only. Do not project a
    # reservation or a call with unknown cost as measured spend.
    spent = sum(item["cents"] for item in data["mix"].values())
    elapsed = (data["now"] - data["midnight"]).total_seconds()
    if hard and 0 < spent < hard["limit"] and elapsed > 0:
        seconds = elapsed * hard["limit"] / spent
        if elapsed < seconds < 86400:
            when = data["midnight"] + timedelta(seconds=seconds)
            lines.append(f"At this pace the hard cap is reached at {when:%H:%M} UTC")
    return "\n".join(lines)


class Commands:
    def __init__(self, runtime):
        self.runtime = runtime

    def __call__(self, raw_args=""):
        args = raw_args.strip().split()
        try:
            if args in ([], ["status"]):
                return render_status(status_data(self.runtime))
            if args == ["verify"]:
                count = sum(1 for _ in verified_records(self.runtime.ledger))
                return f"AgentGuard: chain verified, {count} signed receipts."
            if args and args[0] == "log" and len(args) <= 2:
                return self.log(args[1] if len(args) == 2 else "24h")
        except Exception:
            # Never echo exception text or untrusted log contents.
            return "AgentGuard: could not read or verify the receipt chain. Check the local log and signing key."
        return USAGE

    def log(self, period):
        if period not in {"24h", "7d", "session"}:
            return USAGE
        session = session_digest()
        if period == "session" and not session:
            return "AgentGuard: Hermes did not provide a session identity here. Use /agentguard log 24h."
        now = utc_now()
        since = now - timedelta(hours=24 if period == "24h" else 168)
        counts, approvals, latencies = Counter(), Counter(), []
        for row in verified_records(self.runtime.ledger):
            decision = row["decision"]
            receipt = decision.get("governanceReceipt") or {}
            stamp = datetime.fromisoformat(decision["timestamp"])
            if period == "session":
                if receipt.get("session_sha256") != session:
                    continue
            elif not since <= stamp <= now:
                continue
            kind = receipt.get("kind")
            if kind not in KINDS:
                continue
            approval = receipt if kind in {"approval", "host_approval"} else receipt.get("approval")
            if approval:
                outcome = approval.get("outcome")
                if outcome in {"approved_once", "approved_session", "approved_always", "denied"}:
                    approvals["answered"] += 1
                    latency = approval.get("latency_ms")
                    if type(latency) in (int, float) and latency >= 0:
                        latencies.append(latency)
                else:
                    approvals["unanswered"] += 1
                    approvals["timed_out"] += approval.get("failure") == "timeout"
                if kind in {"approval", "host_approval"}:
                    continue
            if receipt.get("stage", "gate") != "gate":
                continue
            rules = receipt.get("rule_ids") or [receipt.get("rule_id")]
            action = receipt.get("gate_decision", receipt.get("decision"))
            action = {"allow": "allowed", "downgrade": "allowed", "ask": "asked", "deny": "denied"}.get(action)
            if action:
                for rule in set(rules):
                    counts[(kind, rule if rule in RULE_IDS else "OTHER", action)] += 1
        lines = [f"AgentGuard decisions: {period}"]
        for (kind, rule, action), count in sorted(counts.items()):
            lines.append(f"{kind} {rule}: {action} {count}")
        if not counts:
            lines.append("No operation decisions in this window.")
        lines.append(f"Approvals: answered {approvals['answered']}; timed out {approvals['timed_out']}; "
                     f"unanswered {approvals['unanswered']} (includes timeouts)")
        median = f"{statistics.median(latencies) / 1000:.2f}s" if latencies else "unavailable"
        lines.append(f"Median answer time: {median}")
        return "\n".join(lines)
