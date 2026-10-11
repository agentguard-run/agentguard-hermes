"""Directory entry point for Hermes's native plugin loader."""


def register(ctx):
    from .agentguard_spend_hermes import register as register_plugin

    register_plugin(ctx)


__all__ = ["register"]
