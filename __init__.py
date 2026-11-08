"""Policy Backtester: a test harness for Hermes guardrails. Replay a policy over real tool-call history, then shadow and enforce it."""

from pathlib import Path


class _ProfileSettings:
    """This profile's ``plugins.entries.policy-backtester.settings``, home and name, read through Hermes."""

    def __init__(self, ctx):
        self._ctx = ctx

    def get(self, key, default=None):
        return self._ctx.get_config(key, default)

    def set(self, key, value):
        self._ctx.set_config(key, value)

    def home(self):
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())

    def profile(self):
        return self._ctx.profile_name


def register(ctx):
    from .backtester.cli import dispatch, setup_parser
    from .backtester.live import LiveGuard

    settings = _ProfileSettings(ctx)
    guard = LiveGuard(settings.get, settings.home, settings.profile)

    def policy_backtester_pre_tool_call(tool_name="", args=None, **kwargs):
        return guard(tool_name, args, **kwargs)

    ctx.register_hook("pre_tool_call", policy_backtester_pre_tool_call)
    ctx.register_cli_command(
        "backtest",
        help="Test a guardrail against this profile's real work (try), then shadow and enforce it",
        setup_fn=setup_parser,
        handler_fn=lambda args: dispatch(args, settings),
        description="Read this profile's history read-only, replay a policy over it, then shadow or enforce it here.",
    )
