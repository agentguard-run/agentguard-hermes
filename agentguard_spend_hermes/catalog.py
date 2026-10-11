"""Offline model catalog and list prices for the soft-cap swap.

Nothing here fetches anything. The data comes only from what Hermes already
keeps locally (its own per-provider model lists, its on-disk copy of
OpenRouter's model list and its official price table) plus the Spend price
table. A missing source makes the answer unknown, and unknown never swaps.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

MILLION = Decimal(1_000_000)
# Hermes's official price table files the Gemini API under "google".
PRICE_PROVIDER = {"gemini": "google"}
_openrouter_copy = {"mtime": None, "data": {}}


def provider_for(provider, base_url=""):
    """Hermes can report an OpenRouter session as auto or custom."""
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except (TypeError, ValueError):
        host = ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    return provider if isinstance(provider, str) else ""


def provider_models(provider):
    """Model ids that Hermes's own source lists for a provider."""
    try:
        from hermes_cli import models

        if provider == "openrouter":
            return {model for model, _ in models.OPENROUTER_MODELS}
        return set(models._PROVIDER_MODELS.get(provider, []))
    except Exception:
        return set()


def openrouter_metadata():
    """Hermes's on-disk copy of OpenRouter's model list, never refreshed here."""
    try:
        from agent import model_metadata

        mtime = model_metadata._get_model_metadata_cache_path().stat().st_mtime
        if _openrouter_copy["mtime"] != mtime:
            data = model_metadata._load_model_metadata_disk_cache()
            _openrouter_copy.update(mtime=mtime, data=data)
        return _openrouter_copy["data"]
    except Exception:
        return {}


def served(provider, model):
    """True only when the provider's local catalog data lists this exact id."""
    if not isinstance(model, str) or not model:
        return False
    if provider == "openrouter":
        # Hermes also files bare aliases (claude-sonnet-4.6); OpenRouter ids
        # always carry the vendor.
        return "/" in model and (
            model in openrouter_metadata() or model in provider_models("openrouter")
        )
    return model in provider_models(provider)


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _pair(first, second):
    first, second = _decimal(first), _decimal(second)
    return None if first is None or second is None else (first, second)


def _openrouter_price(model):
    pricing = (openrouter_metadata().get(model) or {}).get("pricing") or {}
    found = _pair(pricing.get("prompt"), pricing.get("completion"))
    return None if found is None else (found[0] * MILLION, found[1] * MILLION)


def _spend_price(model):
    from agentguard_spend.cost_table import get_model_cost

    names = [model, model.split("/", 1)[-1]]
    # The Spend table spells Claude versions with a dash (claude-sonnet-4-6).
    names += [re.sub(r"^(claude-[a-z]+-\d+)\.(\d{1,2})$", r"\1-\2", n) for n in names]
    for name in dict.fromkeys(names):
        cost = get_model_cost(name)
        if cost is not None:
            # Cents per thousand tokens to dollars per million tokens.
            found = _pair(cost.inputCentsPerKtok, cost.outputCentsPerKtok)
            return None if found is None else (found[0] * 10, found[1] * 10)
    return None


def _hermes_price(provider, model):
    from agent import usage_pricing

    names = [model.lower()]
    for helper in ("_normalize_anthropic_model_name", "_normalize_bedrock_model_name"):
        normalize = getattr(usage_pricing, helper, None)
        if normalize is not None:
            names.append(normalize(model.lower()))
    for name in dict.fromkeys(names):
        entry = usage_pricing._OFFICIAL_DOCS_PRICING.get(
            (PRICE_PROVIDER.get(provider, provider), name)
        )
        if entry is not None:
            return _pair(entry.input_cost_per_million, entry.output_cost_per_million)
    return None


def price(provider, model):
    """List price in dollars per million input and output tokens, or None."""
    if not isinstance(model, str) or not model:
        return None
    lookups = [lambda: _spend_price(model), lambda: _hermes_price(provider, model)]
    if provider == "openrouter":
        vendor, _, name = model.partition("/")
        lookups = [
            lambda: _openrouter_price(model),
            lookups[0],
            lambda: _hermes_price(vendor, name),
        ]
    for lookup in lookups:
        try:
            found = lookup()
        except Exception:
            found = None
        if found is not None:
            return found
    return None


def cheaper(provider, current, target):
    """True only when both of the target's per-token prices are lower."""
    now, then = price(provider, current), price(provider, target)
    return bool(now and then and then[0] < now[0] and then[1] < now[1])
