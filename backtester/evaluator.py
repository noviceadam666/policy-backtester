"""The evaluator: one tool call in, one verdict out. No I/O, so backtest and live hook agree.

Every applicable rule is checked. Precedence follows Hermes's hook rules: block beats approve beats
no directive. ``allow`` means no objection, never approval, so it can't override anything.

A rule with ``if_unresolved`` that doesn't match a command, but can't tell what the command touches
because the shell would expand part of it, hits with that action instead of passing it. A wildcard
that can't name any of the rule's paths (``./cache/*`` against ``**/config/vendor-api.yaml``) doesn't
count, and the message names the word it couldn't resolve.

``args`` is a precondition on any tool: every named argument must be present and match its regex.

``unless_command`` and each ``unless`` entry exempt the segments they match. An ``unless`` entry with a
``workdir`` exempts a segment only when the call shows it runs in that folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from .matchers import (any_glob_matches, could_reach, normalize_path, segment_candidates, segment_folders, segment_spans,
                       span_at, unresolved_pieces)
from .policy import Policy, Rule


def unresolved_note(word: str) -> str:
    return f"(It can't tell what {word} names until the shell expands it.)"

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
    if not args_match(rule, args):
        return None
    if _rule_fires(rule, args):
        return rule
    command = args.get("command")
    if rule.if_unresolved and isinstance(command, str):
        unresolved = _unresolved_spans(rule, command, args)
        if unresolved:
            return replace(rule, action=rule.if_unresolved, message=f"{rule.message} {unresolved_note(unresolved[0][1])}")
    return None


def args_match(rule: Rule, args: dict) -> bool:
    """Whether every argument ``args`` names is present and matches. Non-strings match as JSON."""
    for name, pattern in rule.args:
        if name not in args:
            return False
        value = args[name]
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
        if not pattern.search(text):
            return False
    return True


def stopping_segments(rule: Rule, args: dict) -> list[tuple[str, str | None]] | None:
    """The command segments that make ``rule`` hit this call, stripped, each with the folder it runs in
    (None when the call doesn't show it), for ``hermes backtest suggest``.

    None when the call has no command, so only a human can scope it. Exempting every segment listed
    here stops the rule hitting the call.
    """
    command = args.get("command")
    if not isinstance(command, str):
        return None
    if not args_match(rule, args):
        return []
    spans = _firing_spans(rule, command, args)
    if rule.if_unresolved:
        spans += [s for s, _ in _unresolved_spans(rule, command, args) if s not in spans]
    folders = segment_folders(command, args.get("workdir"))
    return [(command[start:end].strip(), folders.get((start, end))) for start, end in spans]


def _spans(command: str) -> list[tuple[int, int]]:
    return [s for s in segment_spans(command) if command[s[0]:s[1]].strip()] or [(0, len(command))]


def _exemptor(rule: Rule, command: str, args: dict):
    """Whether a segment of this call is exempt from the rule, by ``unless_command`` or an ``unless`` entry."""
    folders = segment_folders(command, args.get("workdir")) if any(e.workdir for e in rule.unless) else {}

    def exempt(span: tuple[int, int]) -> bool:
        text = command[span[0]:span[1]]
        if rule.unless_command and rule.unless_command.search(text):
            return True
        return any(e.command.search(text) and (e.workdir is None or e.workdir == folders.get(span)) for e in rule.unless)

    return exempt


def _unresolved_spans(rule: Rule, command: str, args: dict) -> list[tuple[tuple[int, int], str]]:
    """Segments the rule cares about that hold an expansion no exemption covers and that could name
    one of its paths, each with the first such word."""
    spans = _spans(command)
    relevant = set(spans)
    if rule.command is not None:
        relevant = {span_at(spans, m.start()) for m in rule.command.finditer(command)}
    exempt = _exemptor(rule, command, args)
    workdir = args.get("workdir")
    cwd = normalize_path(workdir) if isinstance(workdir, str) and workdir else None
    found = []
    for span in spans:
        segment = command[span[0]:span[1]]
        before = cwd
        _, cwd = segment_candidates(segment, cwd)
        if span not in relevant or exempt(span):
            continue
        word = next((w for w in unresolved_pieces(segment) if could_reach(w, rule.paths, before)), None)
        if word is not None:
            found.append((span, word))
    return found


def _rule_fires(rule: Rule, args: dict) -> bool:
    command = args.get("command") if isinstance(args.get("command"), str) else None
    if command is not None:
        return bool(_firing_spans(rule, command, args))
    if rule.command is not None:
        return False
    return not rule.globs or any_glob_matches(rule.globs, _arg_paths(args))


def _firing_spans(rule: Rule, command: str, args: dict) -> list[tuple[int, int]]:
    """The segments a rule fires on, applying ``unless_command`` per segment.

    An exemption covers only the segment it matches, or ``rm -rf ./cache/x && rm -rf /`` would get
    past a rule scoped to ``./cache/``. ``command`` itself sees the whole string, so a pattern can span
    a pipe.
    """
    spans = _spans(command)
    relevant: set[tuple[int, int]] = set()
    if rule.command is None and not rule.globs:
        relevant.update(spans)
    if rule.command is not None:
        matched = {span_at(spans, m.start()) for m in rule.command.finditer(command)}
        if not matched:
            return []
        relevant |= matched
    if rule.globs:
        matched = _segments_mentioning(rule, command, spans, args)
        if not matched:
            return []
        relevant |= matched
    exempt = _exemptor(rule, command, args)
    return sorted(s for s in relevant if not exempt(s))


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
