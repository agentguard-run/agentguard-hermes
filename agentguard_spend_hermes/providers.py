"""Normalize provider accounting without retaining messages or response text."""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from agentguard_spend import compute_call_cents

from .refusals import blocked_response, model_key, model_of

__all__ = ["accounting", "blocked_response", "model_key", "model_of", "projected_cost"]


def field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def token_count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token usage")
    return value


def list_price(model, inputs, outputs):
    result = compute_call_cents(model, inputs, outputs)
    if result is None and "/" in model:
        result = compute_call_cents(model.split("/", 1)[1], inputs, outputs)
    return result


def projected_cost(request):
    # Local, conservative size estimate. No prompt text is written to the log.
    # It is also the reservation a concurrent call holds, so it counts the
    # system prompt, instructions and tool schemas that Anthropic, Codex and
    # Bedrock requests carry outside the messages. Those extra fields never
    # make a request invalid.
    chars = len(
        json.dumps(
            request.get("messages", request.get("input", [])), ensure_ascii=False
        )
    )
    for name in ("system", "instructions", "tools", "toolConfig"):
        if request.get(name):
            chars += len(json.dumps(request[name], ensure_ascii=False, default=str))
    inputs = math.ceil(chars / 4)
    inference = request.get("inferenceConfig")
    outputs = request.get(
        "max_completion_tokens",
        request.get(
            "max_output_tokens",
            request.get(
                "max_tokens",
                inference.get("maxTokens", 4096)
                if isinstance(inference, dict)
                else 4096,
            ),
        ),
    )
    outputs = 4096 if outputs is None else token_count(outputs)
    return list_price(model_of(request), inputs, outputs) or 0


def accounting(response, model):
    usage = field(response, "usage")
    if usage is None:
        usage = field(response, "usage_metadata", field(response, "usageMetadata"))
    inputs = field(
        usage,
        "prompt_tokens",
        field(
            usage,
            "input_tokens",
            field(usage, "prompt_token_count", field(usage, "promptTokenCount")),
        ),
    )
    outputs = field(
        usage,
        "completion_tokens",
        field(
            usage,
            "output_tokens",
            field(
                usage, "candidates_token_count", field(usage, "candidatesTokenCount")
            ),
        ),
    )
    has_usage = inputs is not None and outputs is not None
    inputs, outputs = token_count(inputs or 0), token_count(outputs or 0)
    # Anthropic's input_tokens excludes cache reads and writes. The base table
    # has no cache rates, so counting them at input list price is conservative.
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        inputs += token_count(field(usage, name, 0) or 0)
    reported = field(usage, "cost", field(response, "cost"))
    if reported is not None:
        try:
            cost = Decimal(str(reported))
            if not cost.is_finite() or cost < 0:
                raise ValueError("Invalid provider cost")
            cents = int((cost * 100).to_integral_value(rounding=ROUND_CEILING))
        except InvalidOperation as exc:
            raise ValueError("Invalid provider cost") from exc
        return (
            cents,
            "gateway_reported",
            {"input_tokens": inputs, "output_tokens": outputs},
        )
    cents = list_price(model, inputs, outputs) if has_usage else None
    return (
        cents or 0,
        "api_list_price_estimate" if cents is not None else "unpriced",
        {"input_tokens": inputs, "output_tokens": outputs},
    )
