"""Spend-compatible receipts, serialized across threads and local processes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from agentguard_spend import (
    GENESIS_PREVIOUS_HASH,
    FilesystemDecisionLogStore,
    SpendDecision,
    build_scope_key,
    sign_decision,
)


def sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


TAIL_BYTES = 65536
PROCESS_TOKEN = uuid.uuid4().hex


def session_digest(context=None):
    """Use only host session metadata, never a prompt or a tool argument."""
    context = context or {}
    session = context.get("session_key") or context.get("session_id")
    try:
        from gateway.session_context import get_session_env
        session = (context.get("session_key") or get_session_env("HERMES_SESSION_KEY", "")
                   or context.get("session_id") or get_session_env("HERMES_SESSION_ID", ""))
    except ImportError:
        pass
    return arguments_hash({"session": session}) if isinstance(session, str) and session else None


def chain_head(path, block=None):
    """Sequence and hash of the last complete entry, read from the file's end.

    The SDK store's get_latest() parses every line on each append and stops
    at one million entries, so every guarded call got slower as the log grew
    (about 0.5 s per call at 50,000 rows) and the chain would fork past that
    size. Only the newest parseable line is needed to extend the chain. Like
    the SDK store, lines that do not parse are skipped.
    """
    block = block or TAIL_BYTES
    try:
        f = open(path, "rb")  # noqa: SIM115
    except FileNotFoundError:
        return None
    with f:
        position = f.seek(0, os.SEEK_END)
        partial = b""
        while position > 0:
            step = min(block, position)
            position -= step
            f.seek(position)
            lines = (f.read(step) + partial).split(b"\n")
            # The first piece may continue in the previous block.
            partial = lines.pop(0) if position > 0 else b""
            for raw in reversed(lines):
                try:
                    entry = json.loads(raw)
                    sequence, entry_hash = entry["sequence"], entry["entryHash"]
                except (ValueError, KeyError, TypeError):
                    continue
                if type(sequence) is int and isinstance(entry_hash, str):
                    return sequence, entry_hash
    return None


def arguments_hash(arguments):
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class Ledger:
    def __init__(self, path, policy, private_key, public_key):
        self.path, self.policy = path, policy
        self.private_key, self.public_key = private_key, public_key
        self.lock = threading.RLock()

    def append(
        self,
        receipt,
        *,
        model="",
        original_model="",
        provider="",
        cost=0,
        before=0,
        cap=None,
    ):
        receipt = {"process_token": PROCESS_TOKEN, "session_sha256": session_digest(), **receipt}
        policy = self.policy
        action = {"deny": "block", "ask": "block", "downgrade": "downgrade"}.get(
            receipt["decision"], "allow"
        )
        decision = SpendDecision(
            decisionId=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            triggeredCap=cap,
            triggeredScopeKey=build_scope_key(policy.scope),
            projectedCents=cost,
            windowSpendBefore=before,
            windowSpendAfter=before + cost,
            provider=provider,
            modelRequested=original_model or model,
            modelResolved=model,
            policyId=policy.id,
            policyVersion=policy.version,
            enforcementMode=policy.mode,
            reasons=[receipt["reason"]],
            entryType="governance",
            governanceReceipt=receipt,
        )

        async def append():
            store = FilesystemDecisionLogStore(self.path)
            # Read under the file lock below, so no writer can append between
            # this read and the store append.
            previous = chain_head(self.path)
            entry = await sign_decision(
                sequence=previous[0] + 1 if previous else 0,
                decision=decision,
                previous_hash=previous[1] if previous else GENESIS_PREVIOUS_HASH,
                private_key=self.private_key,
                public_key=self.public_key,
            )
            # Use the SDK's serialization and store, including when extending an
            # existing 0.2.x chain. Never add unsigned metadata outside the receipt.
            await store.append(entry)
            return entry

        with self.lock:
            fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return sync(append())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
