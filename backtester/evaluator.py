"""The evaluator: one tool call in, one verdict out. No I/O, so backtest and live hook agree.

Every applicable rule is checked. Precedence follows Hermes's hook rules: block beats approve beats
no directive. ``allow`` means no objection, never approval, so it can't override anything.

A rule with ``if_unresolved`` that doesn't match a command, but can't tell what the command touches
because the shell would expand part of it, hits with that action instead of passing it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .matchers import any_glob_matches, has_unresolved, normalize_path, segment_candidates, segment_spans, span_at
from .policy import Policy, Rule

UNRESOLVED_NOTE = "(The command's target couldn't be resolved before it runs.)"

_PATH_ARGS = ("path", "file_path", "filepath", "filename", "paths", "files")


@dataclass(frozen=True)
class Verdict:
    action: str | None
    message: str | None
    hits: tuple[Rule, ...]

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.hits)


def evaluate(policy: Policy, tool: str, args: Any, profile: str | None = None) -> Verdict:
    args = args if isinstance(args, dict) else {}
    hits = tuple(hit for r in policy.rules if r.applies_to(tool, profile) for hit in (_hit(r, args),) if hit)
    for action in ("block", "approve"):
        decisive = next((r for r in hits if r.action == action), None)
        if decisive:
            return Verdict(action, decisive.message, hits)
    return Verdict(None, None, hits)


def _hit(rule: Rule, args: dict) -> Rule | None:
    """The rule as it hits this call: itself, its ``if_unresolved`` stand-in, or None."""
    if _rule_fires(rule, args):
        return rule
    command = args.get("command")
    if rule.if_unresolved and isinstance(command, str) and _unresolved(rule, command):
        return replace(rule, action=rule.if_unresolved, message=f"{rule.message} {UNRESOLVED_NOTE}")
    return None


def _unresolved(rule: Rule, command: str) -> bool:
    """Whether a segment the rule cares about holds an expansion ``unless_command`` doesn't exempt."""
    spans = [s for s in segment_spans(command) if command[s[0]:s[1]].strip()] or [(0, len(command))]
    if rule.command is not None:
        matched = {span_at(spans, m.start()) for m in rule.command.finditer(command)}
        spans = [s for s in spans if s in matched]
    for start, end in spans:
        segment = command[start:end]
        if has_unresolved(segment) and not (rule.unless_command and rule.unless_command.search(segment)):
            return True
    return False


def _rule_fires(rule: Rule, args: dict) -> bool:
    command = args.get("command") if isinstance(args.get("command"), str) else None
    if command is not None:
        return _fires_on_command(rule, command, args)
    if rule.command is not None:
        return False
    return not rule.globs or any_glob_matches(rule.globs, _arg_paths(args))


def _fires_on_command(rule: Rule, command: str, args: dict) -> bool:
    """Check a command, applying ``unless_command`` per segment.

    ``unless_command`` exempts only the segment it matches, or ``rm -rf ./cache/x && rm -rf /`` would
    get past a rule scoped to ``./cache/``. ``command`` itself sees the whole string, so a pattern
    can span a pipe.
    """
    spans = [s for s in segment_spans(command) if command[s[0]:s[1]].strip()] or [(0, len(command))]
    relevant: set[tuple[int, int]] = set()
    if rule.command is None and not rule.globs:
        relevant.update(spans)
    if rule.command is not None:
        matched = {span_at(spans, m.start()) for m in rule.command.finditer(command)}
        if not matched:
            return False
        relevant |= matched
    if rule.globs:
        matched = _segments_mentioning(rule, command, spans, args)
        if not matched:
            return False
        relevant |= matched
    if rule.unless_command is None:
        return True
    return any(not rule.unless_command.search(command[start:end]) for start, end in relevant)


def _segments_mentioning(rule: Rule, command: str, spans: list[tuple[int, int]], args: dict) -> set[tuple[int, int]]:
    """Segments that mention a path the rule's globs match, tracking ``cd`` across segments."""
    workdir = args.get("workdir")
    cwd = normalize_path(workdir) if isinstance(workdir, str) and workdir else None
    matched = set()
    if any_glob_matches(rule.globs, _arg_paths(args)):
        matched.add(spans[0])
    for span in spans:
        paths, cwd = segment_candidates(command[span[0]:span[1]], cwd)
        if any_glob_matches(rule.globs, paths):
            matched.add(span)
    return matched


def _arg_paths(args: dict) -> set[str]:
    paths = set()
    for key in _PATH_ARGS:
        value = args.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item:
                paths.add(normalize_path(item))
    return paths
