"""Read-only Desktop data through the public slash-command bridge."""

import re
import os

from fastapi import APIRouter

router = APIRouter()

UNAVAILABLE = {"chip": "AgentGuard unavailable", "text": "Enable AgentGuard in this gateway profile to read its status."}


@router.get("/status")
def status():
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return {"chip": "AgentGuard status unavailable",
                "text": "Desktop cannot read live counters across plugin-host isolation. Use /agentguard in the protected runtime."}
    try:
        from hermes_cli.plugins import get_plugin_command_handler, resolve_plugin_command_result

        handler = get_plugin_command_handler("agentguard")
        if handler is None:
            return dict(UNAVAILABLE)
        text = resolve_plugin_command_result(handler("status"))
        if not isinstance(text, str) or not text.startswith("AgentGuard status\n"):
            return dict(UNAVAILABLE)
        hard = re.search(r"^Today, hard cap: (\$\d+\.\d{2}) of (\$\d+\.\d{2}) ", text, re.MULTILINE)
        helpers = re.search(r"^Helpers, short: (\d+) of (\d+) ", text, re.MULTILINE)
        chip = "AgentGuard"
        if hard:
            chip += f" {hard[1]} of {hard[2].removesuffix('.00')}"
        if helpers:
            chip += f" · {helpers[1]} of {helpers[2]} helpers"
        if "\nLockdown:" in text:
            chip = "AgentGuard lockdown"
        return {"chip": chip, "text": text}
    except Exception:
        return dict(UNAVAILABLE)
