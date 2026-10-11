"""AgentGuard for Hermes Agent, using public execution middleware only."""
from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import threading
from typing import Any

logger = logging.getLogger(__name__)


class AgentGuardSetupError(RuntimeError):
    """Raised when the spend guard cannot be installed.

    Fail closed: Hermes must not silently run ungoverned because AgentGuard
    failed to start. AGENTGUARD_DISABLE=1 is the deliberate way to run
    without spend caps.
    """


# Spend windows must accumulate for the life of the process. Hermes rebuilds
# its provider clients (the OpenAI chokepoint is literally named
# _create_request_openai_client), and a fresh InMemorySpendStore per
# construction would report 0 spend every time, so per_day / per_minute caps
# could never fire. One store per (state dir, policy scope) fixes that.
#
# Only the spend store is shared. The decision log store is rebuilt per
# config on purpose: FilesystemDecisionLogStore holds an asyncio.Lock, and
# the SDK's sync bridge runs each call under a fresh event loop (and, when
# already inside a loop, on a worker thread), so a long-lived lock could bind
# to a dead loop under contention. The filesystem store re-reads get_latest()
# before every append, so per-construction instances still chain correctly.
_spend_stores: dict[tuple[str, str], Any] = {}
_spend_stores_lock = threading.Lock()


def _spend_store_key(policy: Any) -> tuple[str, str]:
    """Identity for a spend-accounting bucket.

    The state directory keeps two HERMES_HOME values apart; the SDK's own
    scope key keeps two tenants/agents apart (it hashes tenantId, userId,
    teamId, agentId, taskId and provider), and it is the exact key the store
    is read and written under, so nothing can share a bucket that the SDK
    would not have shared anyway.
    """
    from agentguard_spend import build_scope_key

    return (str(_ag_dir()), build_scope_key(policy.scope))


def _shared_spend_store(policy: Any) -> Any:
    from agentguard_spend import InMemorySpendStore

    key = _spend_store_key(policy)
    with _spend_stores_lock:
        store = _spend_stores.get(key)
        if store is None:
            store = InMemorySpendStore()
            _spend_stores[key] = store
        return store

_DEFAULT_HERMES_HOME = "~/.hermes"
_AG_DIR_NAME = "agentguard"
_POLICY_FILENAME = "policy.yaml"
_PRIVATE_KEY_FILENAME = "private.key"
_PUBLIC_KEY_FILENAME = "public.key"
_DECISIONS_FILENAME = "decisions.ndjson"

__version__ = "0.4.1"

_POLICY_YAML_TEMPLATE = """\
# AgentGuard Spend default policy (auto-generated on first install).
# {edit_hint}
id: hermes-default-v1
name: {policy_name}
version: 1
effectiveFrom: "2026-01-01T00:00:00Z"
mode: enforce

scope:
  tenantId: hermes-personal

caps:
  - amountCents: 500
    window: per_day
    action: downgrade
    downgradeTo: claude-sonnet-4-6
    reason: "{soft_cap_reason}"

  - amountCents: 2000
    window: per_day
    action: block
    reason: "{hard_cap_reason}"

  - amountCents: 200
    window: per_minute
    action: block
    reason: "{burst_reason}"

# Targets must be accepted by the provider currently in use.
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
"""


_POLICY_LOCALE_STRINGS = {
    "en-US": {
        "edit_hint": "Edit this file to change your caps. All amounts are integer cents.",
        "policy_name": "Hermes default caps",
        "soft_cap_reason": "Soft daily cap ($5/day): downgrading Opus to Sonnet",
        "hard_cap_reason": "Hard daily ceiling ($20/day)",
        "burst_reason": "Per-minute burst guard ($2/min)",
    },
    "es-419": {
        "edit_hint": "Edite este archivo para cambiar sus límites. Todos los montos son enteros en centavos.",
        "policy_name": "Límites predeterminados de Hermes",
        "soft_cap_reason": "Límite diario blando ($5/día): degradando Opus a Sonnet",
        "hard_cap_reason": "Tope diario duro ($20/día)",
        "burst_reason": "Protección de ráfaga por minuto ($2/min)",
    },
    "pt-BR": {
        "edit_hint": "Edite este arquivo para alterar seus limites. Todos os valores são números inteiros em centavos.",
        "policy_name": "Limites padrão do Hermes",
        "soft_cap_reason": "Limite diário leve ($5/dia): rebaixando Opus para Sonnet",
        "hard_cap_reason": "Teto diário rígido ($20/dia)",
        "burst_reason": "Proteção de pico por minuto ($2/min)",
    },
}


def _resolve_install_locale() -> str:
    """Resolve locale for the auto-bootstrap policy at install time.

    Uses the same priority chain as the agentguard-spend SDK runtime,
    so the bootstrapped policy reasons match the locale the runtime
    will later render block traces in.
    """
    try:
        from agentguard_spend import resolve_locale
        return resolve_locale()
    except ImportError:
        return "en-US"


def _build_default_policy_yaml() -> str:
    locale = _resolve_install_locale()
    strings = _POLICY_LOCALE_STRINGS.get(locale, _POLICY_LOCALE_STRINGS["en-US"])
    return _POLICY_YAML_TEMPLATE.format(**strings)


def _ag_dir() -> pathlib.Path:
    home = os.environ.get("HERMES_HOME", os.path.expanduser(_DEFAULT_HERMES_HOME))
    return pathlib.Path(home).expanduser() / _AG_DIR_NAME


def _decisions_path() -> pathlib.Path:
    return _ag_dir() / _DECISIONS_FILENAME


def _harden_dir(d: pathlib.Path) -> None:
    """Best-effort tighten the AgentGuard state dir to 0700.

    mkdir(mode=...) only applies when mkdir actually creates the leaf, so a
    directory left over from an earlier install can still be group/world
    readable. The Ed25519 signing key lives here.
    """
    try:
        d.chmod(0o700)
    except OSError as exc:
        logger.warning(
            "agentguard-spend: cannot tighten permissions on %s (%s)", d, exc
        )


def _write_secret_bytes(path: pathlib.Path, data: bytes) -> None:
    """Write `data` to `path` as an owner-only (0600) file.

    The secret must never exist on disk in a world-readable state, not even
    for the instant between write and chmod, so the restrictive mode is set
    at creation time via os.open. fchmod restates it because O_CREAT's mode
    is masked by umask and is ignored outright when the file already exists.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)


def _bootstrap_if_needed() -> dict[str, pathlib.Path]:
    """Create policy + keys on first run if absent. Returns the paths dict.

    Raises AgentGuardSetupError rather than returning: a plugin that quietly
    disables itself here would leave Hermes running with no spend caps at all.
    """
    d = _ag_dir()
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise AgentGuardSetupError(
            f"agentguard-spend: cannot create {d} ({exc}) - refusing to run "
            "ungoverned. Fix the path/permissions, or set AGENTGUARD_DISABLE=1 "
            "to run without spend caps."
        ) from exc
    _harden_dir(d)

    policy_path = d / _POLICY_FILENAME
    private_key_path = d / _PRIVATE_KEY_FILENAME
    public_key_path = d / _PUBLIC_KEY_FILENAME

    if not policy_path.exists():
        policy_path.write_text(_build_default_policy_yaml())
        logger.info(
            "agentguard-spend: wrote default policy to %s (locale: %s)",
            policy_path,
            _resolve_install_locale(),
        )

    if not private_key_path.exists() or not public_key_path.exists():
        try:
            from agentguard_spend import generate_keypair
        except ImportError as exc:
            raise AgentGuardSetupError(
                "agentguard-spend: the agentguard-spend SDK is not installed in "
                "the Hermes virtualenv, so no spend cap can be enforced. Run "
                "`uv pip install agentguard-spend`, or set AGENTGUARD_DISABLE=1 "
                "to run without spend caps."
            ) from exc
        priv, pub = generate_keypair()
        # Private key is written 0600 from creation; the public key is meant
        # to be shared with anyone verifying the chain, so it stays readable.
        _write_secret_bytes(private_key_path, priv)
        public_key_path.write_bytes(pub)
        logger.info(
            "agentguard-spend: generated signing keypair (private=%s, public=%s)",
            private_key_path,
            public_key_path,
        )

    return {
        "policy": policy_path,
        "private_key": private_key_path,
        "public_key": public_key_path,
    }


def _build_cap(spend_cap_cls: type, raw_cap: Any, index: int, policy_path: pathlib.Path) -> Any:
    """Build one SpendCap, dropping keys the installed SDK does not define.

    A policy.yaml carried over from an older release (or copied from stale
    docs) can contain keys SpendCap never had. `scope:` is the common one:
    per-cap scoping does not exist, enforcement keys on policy.scope. Passing
    such a key straight through as **kwargs raises TypeError out of
    register(), which would take the whole plugin down and leave Hermes
    running with no spend guard at all. Drop the unknown keys loudly and keep
    enforcing the cap.
    """
    if not isinstance(raw_cap, dict):
        raise TypeError(
            f"{policy_path}: caps[{index}] must be a mapping, "
            f"got {type(raw_cap).__name__}"
        )
    known = {f.name for f in dataclasses.fields(spend_cap_cls)}
    unknown = sorted(k for k in raw_cap if k not in known)
    if unknown:
        logger.warning(
            "agentguard-spend: %s caps[%d] has unsupported key(s) %s - ignoring them, "
            "the cap itself still applies. SpendCap has no per-cap `scope`; to give "
            "each bridge its own budget, run one Hermes process per bridge with its "
            "own HERMES_AGENT_ID and its own policy.",
            policy_path,
            index,
            ", ".join(unknown),
        )
    return spend_cap_cls(**{k: v for k, v in raw_cap.items() if k in known})


def _load_policy(policy_path: pathlib.Path) -> Any:
    try:
        import yaml
        from agentguard_spend import SpendPolicy, SpendScope, SpendCap
    except ImportError as exc:
        raise AgentGuardSetupError(
            "agentguard-spend: cannot import the SDK or PyYAML, so the policy "
            f"at {policy_path} cannot be loaded and no spend cap can be "
            "enforced. Set AGENTGUARD_DISABLE=1 to run without spend caps."
        ) from exc

    raw = yaml.safe_load(policy_path.read_text())
    return SpendPolicy(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        scope=SpendScope(**raw.get("scope", {})),
        caps=[
            _build_cap(SpendCap, c, i, policy_path)
            for i, c in enumerate(raw.get("caps", []))
        ],
        mode=raw.get("mode", "enforce"),
        version=raw.get("version", 1),
        effectiveFrom=raw["effectiveFrom"],
        requiredCapability=raw.get("requiredCapability"),
    )


def _build_config(policy: Any, private_key: bytes, public_key: bytes) -> Any:
    from agentguard_spend import (
        SpendGuardConfig,
        SigningKeys,
        FilesystemDecisionLogStore,
    )

    # The signed ledger has to outlive the process: an in-memory log store
    # drops the whole chain on exit, and README tells operators to verify
    # ~/.hermes/agentguard/decisions.ndjson. The filesystem store appends
    # there and reads the last entry back, so the hash chain stays continuous
    # across restarts.
    return SpendGuardConfig(
        policy=policy,
        spendStore=_shared_spend_store(policy),
        logStore=FilesystemDecisionLogStore(_decisions_path()),
        signingKeys=SigningKeys(privateKey=private_key, publicKey=public_key),
    )


def _setup_reason(step, error):
    """A plain, short reason for a setup failure. Never includes file contents."""
    cause = error.__cause__ if error.__cause__ is not None else error
    if isinstance(cause, ImportError):
        name = getattr(cause, "name", None) or "a required package"
        return f"{name} is not installed in the Python environment Hermes runs from"
    return f"{step} ({type(cause).__name__})"


class AgentGuardMiddlewareRequired(BaseException):
    """Fatal unsupported-host error, not a recoverable plugin setup failure.

    Hermes catches Exception and SystemExit from register(), removes the plugin,
    and continues. Neither is a safe signal when no execution gate exists.
    """


def register(ctx) -> None:
    """Called by Hermes plugin manager when the plugin is loaded."""
    if os.environ.get("AGENTGUARD_DISABLE", "").lower() in {"1", "true", "yes"}:
        logger.info("agentguard-spend: disabled via AGENTGUARD_DISABLE env var")
        return

    from .lockdown import Lockdown

    lockdown = Lockdown()
    if not callable(getattr(ctx, "register_middleware", None)):
        lockdown.fail("Hermes middleware is unavailable; use Hermes 0.16.0 or newer")
        logger.critical("%s", lockdown.message)
        # There is no public hook that can replace an absent model execution
        # boundary. Stop initialization instead of claiming to install a gate.
        raise AgentGuardMiddlewareRequired(lockdown.message)

    # Keep these callbacks registered if any later setup step fails. Returning
    # a refusal matters: Hermes advances its chain on a middleware exception.
    ctx.register_middleware("llm_execution", lockdown.llm_execution)
    ctx.register_middleware("tool_execution", lockdown.tool_execution)
    step = "the AgentGuard folder could not be prepared"
    try:
        paths = _bootstrap_if_needed()
        step = f"the policy at {paths['policy']} could not be loaded"
        policy = _load_policy(paths["policy"])
        step = "the signing keys could not be read"
        private_key = paths["private_key"].read_bytes()
        public_key = paths["public_key"].read_bytes()
        step = "the guard could not start"
        from .middleware import Runtime

        runtime = Runtime(
            policy, paths, _build_config(policy, private_key, public_key)
        )
        ctx.register_middleware("llm_request", runtime.llm_request)
        ctx.register_middleware("llm_execution", runtime.llm_execution)
        ctx.register_middleware("tool_execution", runtime.tool_execution)
        from .commands import Commands
        from .approvals import ApprovalObserver

        if callable(getattr(ctx, "register_hook", None)):
            runtime.approvals = ApprovalObserver(runtime)
            ctx.register_hook("pre_approval_request", runtime.approvals.pre)
            ctx.register_hook("post_approval_response", runtime.approvals.post)
            try:
                from hermes_cli.plugins import VALID_HOOKS
                runtime.auxiliary.supported = {"pre_auxiliary_call", "post_auxiliary_call"} <= VALID_HOOKS
            except ImportError:
                pass
            if runtime.auxiliary.supported is not False:
                ctx.register_hook("pre_auxiliary_call", runtime.auxiliary.pre)
                ctx.register_hook("post_auxiliary_call", runtime.auxiliary.post)
        if callable(getattr(ctx, "register_command", None)):
            ctx.register_command("agentguard", Commands(runtime),
                                 description="Spend, decisions and receipt verification",
                                 args_hint="[status | log 24h|7d|session | verify]")
        lockdown.open()
        logger.info(
            "AgentGuard %s: official middleware active (policy=%s)",
            __version__, paths["policy"],
        )
    except Exception as error:
        lockdown.fail(_setup_reason(step, error))
        logger.error("%s", lockdown.message)
