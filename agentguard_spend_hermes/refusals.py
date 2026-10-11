"""Refusals Hermes accepts as finished results. No Spend SDK import here.

The setup lockdown uses these when the SDK itself may be missing, so this
module must import only the standard library.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def model_key(request):
    """Hermes's Bedrock Converse requests name the model `modelId`."""
    return "modelId" if "model" not in request and "modelId" in request else "model"


def model_of(request):
    return request.get(model_key(request)) if isinstance(request, dict) else None


def blocked_response(reason, model="", api_mode=""):
    """A completed local refusal, so Hermes cannot fail open or retry a gate error.

    Hermes consumes a completed response even in streaming mode: its streaming
    transport aggregates deltas before returning through this middleware.
    """
    ns = SimpleNamespace
    usage = ns(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    if api_mode == "anthropic_messages":
        return ns(
            id="agentguard-blocked",
            type="message",
            role="assistant",
            model=model,
            content=[ns(type="text", text=reason)],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=usage,
            agentguard_blocked=True,
        )
    if api_mode == "codex_responses":
        return ns(
            id="agentguard-blocked",
            model=model,
            status="completed",
            output=[
                ns(
                    type="message",
                    role="assistant",
                    content=[ns(type="output_text", text=reason, annotations=[])],
                )
            ],
            output_text=reason,
            usage=usage,
            agentguard_blocked=True,
        )
    return ns(
        id="agentguard-blocked",
        model=model,
        object="chat.completion",
        choices=[
            ns(
                index=0,
                finish_reason="stop",
                message=ns(
                    role="assistant", content=reason, tool_calls=None, refusal=None
                ),
            )
        ],
        usage=usage,
        agentguard_blocked=True,
    )


def blocked_tool(reason, rule_id, decision="deny"):
    # Tool handlers return JSON text in Hermes. Keeping that shape also makes
    # the result safe through its observer and post-tool result transforms.
    return json.dumps(
        {"error": reason, "agentguard": {"decision": decision, "rule_id": rule_id}},
        separators=(",", ":"),
    )
