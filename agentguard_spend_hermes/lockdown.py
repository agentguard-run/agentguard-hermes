"""Fail-closed stand-in registered before any AgentGuard setup step.

Hermes skips a plugin whose register() raises and keeps running without it,
so a setup failure must not raise. This middleware is registered first. It
refuses every model and tool call until setup finishes, refuses them for good
with a plain reason if setup fails, and does nothing once setup succeeds.
It imports only the standard library, because a missing Spend SDK is one of
the failures it exists for.
"""

from __future__ import annotations

from .refusals import blocked_response, blocked_tool, model_of


def setup_message(reason):
    return (
        f"AgentGuard setup failed: {reason}. Fix it, or set "
        "AGENTGUARD_DISABLE=1 to run without AgentGuard."
    )


class Lockdown:
    def __init__(self):
        self.closed = True
        self.message = setup_message("setup did not finish")

    def open(self):
        """Setup succeeded: from now on every call passes straight through."""
        self.closed = False

    def fail(self, reason):
        self.closed = True
        self.message = setup_message(reason)

    def llm_execution(self, *, request=None, next_call=None, **context):
        if not self.closed and callable(next_call):
            return next_call(request)
        model = model_of(request)
        return blocked_response(
            self.message,
            model if isinstance(model, str) else "",
            context.get("api_mode", ""),
        )

    def tool_execution(self, *, tool_name=None, args=None, next_call=None, **context):
        if not self.closed and callable(next_call):
            return next_call(args)
        return blocked_tool(self.message, "AG_SETUP_FAILED")
