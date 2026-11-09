"""Built-in rules that keep an agent from switching off its own guard.

Every policy gets these on top of its own rules, in the live hook and in the backtest alike: an agent
that hits a block can't run this plugin's ``off``, ``shadow`` or ``enforce``, disable or remove the
plugin, set its settings, or name the files that hold its mode, policy and logs. The backtest checks
them too, so a job that does one of these shows up as a job the policy would break before enforce
stops it. Run those steps yourself, outside the agent.

They cover Hermes's own commands, its file-writing tools and shell commands that name those files.
They're a policy gate, not a sandbox: code an agent writes and runs can still reach the files.
"""

from __future__ import annotations

from pathlib import Path

from .policy import Policy, PolicyError, parse_policy

COMMANDS_ID = "guard-commands"
FILES_ID = "guard-files"
IDS = frozenset({COMMANDS_ID, FILES_ID})
# One shell segment, so ``hermes backtest log; echo off`` isn't read as ``backtest off``.
_SEG = r"[^;&|\n()`]*"
_COMMANDS = "|".join([
    # The subcommand is the first word after backtest's own options: ``backtest --hermes-root /x off``.
    # Each option, ``--`` included, reads only one way, so a long run of options that doesn't match fails in
    # linear time: Python can't interrupt a regex, and Hermes blocks a hook that runs past 30 seconds.
    r"\bbacktest(?:\s+(?:--|-{1,2}\w[\w-]*(?:=\S+|\s+[^\s-]\S*)?))*\s+(?:off|shadow|enforce)(?![\w./-])",
    rf"\bplugins\s+(?:disable|uninstall|remove|rm)\b{_SEG}\bpolicy-backtester\b",
    rf"\bconfig\s+set\s+{_SEG}\bplugins\b",
])


def _home_glob(path: Path) -> str:
    """``~/...`` when the path is under this user's home, so ``$HOME`` and ``~`` forms match too."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def guard_rules(root: Path) -> list[dict]:
    """The built-in rules for every profile under a Hermes root, as policy-file rules."""
    root = Path(root).expanduser()
    homes = [root, root / "profiles" / "*"]
    paths = sorted({_home_glob(h / name) for h in homes for name in ("config.yaml", "backtest/**")})
    return [
        {"id": COMMANDS_ID, "tool": "terminal", "command": _COMMANDS, "action": "block",
         "message": "Only a person changes this profile's guard: run that yourself, outside the agent."},
        {"id": FILES_ID, "tool": ["terminal", "write_file", "patch"], "path": paths, "action": "block",
         "message": "The agent can't touch the files that hold its guard's mode, policy and logs."},
    ]


def with_guard(policy: Policy, root: Path) -> Policy:
    """The policy plus the built-in rules. Its hash stays the policy file's, which is what's promoted."""
    taken = IDS & {r.id for r in policy.rules}
    if taken:
        raise PolicyError(f"{policy.source}: rule id(s) {', '.join(sorted(taken))} are reserved for the built-in guard rules")
    guard = parse_policy({"rules": guard_rules(root)}, source="built-in guard")
    return Policy(policy.rules + guard.rules, policy.hash, policy.source)


def is_guard(rule_id: str) -> bool:
    return rule_id in IDS
