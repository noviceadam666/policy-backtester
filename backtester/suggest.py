"""``hermes backtest suggest``: propose the exemptions that would stop a policy breaking jobs, for a human.

For each job the policy would break, it finds the command segments each rule stopped and exempts
exactly those: the segment's words escaped, joined by ``\\s+`` and anchored to the whole segment, the
way ``policies/refined.yaml`` scopes its two rules. It does that only when every run that hit the rule
stopped on the same segments. A command that changes between runs, or a call that isn't a terminal
command, is left for a human, because widening a regex until it fits is how a guardrail stops guarding.

Each exemption is an ``unless`` entry. When every run shows the same folder for those segments, the
entry is pinned to it with ``workdir``, so the same command run elsewhere is still stopped. When the
runs don't show one folder, the entry isn't pinned, and the folder is left for a human.

It never applies anything. It returns a policy file to review, and the caller backtests it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import SingleQuotedScalarString

from .engine import BacktestResult, JobBreak
from .evaluator import evaluate, stopping_segments
from .policy import Policy, parse_policy


@dataclass
class Exemption:
    rule_id: str
    job: JobBreak
    segments: tuple[str, ...]
    runs_hit: int
    workdir: str | None = None


@dataclass
class ForReview:
    rule_id: str
    job: JobBreak
    reason: str
    # True when an exemption is proposed anyway and only its folder is left to decide.
    exempted: bool = False


@dataclass
class Suggestion:
    source: Policy
    exemptions: list[Exemption] = field(default_factory=list)
    for_review: list[ForReview] = field(default_factory=list)
    # Per rule, the ``unless`` entries to add: (command pattern, workdir or None).
    patterns: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)


def _escape(word: str) -> str:
    # ``-`` needs no escape outside a character class, and these patterns never have one.
    return "".join(c if c == "-" else re.escape(c) for c in word)


def exact_pattern(segments: list[str] | tuple[str, ...]) -> str:
    """A regex that matches only these segments, whole, allowing any run of whitespace between words."""
    alternatives = [r"\s+".join(_escape(word) for word in segment.split()) for segment in sorted(set(segments))]
    body = alternatives[0] if len(alternatives) == 1 else "(?:" + "|".join(alternatives) + ")"
    return rf"^\s*{body}\s*$"


def suggest(result: BacktestResult) -> Suggestion:
    policy = result.policy
    rules = {r.id: r for r in policy.rules}
    suggestion = Suggestion(policy)
    for job in result.would_break:
        runs: dict[str, dict[str, set[tuple[str, str | None]]]] = {}
        unexemptable: dict[str, str] = {}
        for call in job.calls:
            for hit in evaluate(policy, call.tool, call.args, call.profile).hits:
                if hit.action not in ("block", "approve"):
                    continue
                segments = stopping_segments(rules[hit.id], call.args)
                if segments is None:
                    unexemptable.setdefault(hit.id, f"it stops a {call.tool} call, which has no command to exempt")
                elif not segments:
                    unexemptable.setdefault(hit.id, "it couldn't tell which part of the command the rule stops")
                else:
                    runs.setdefault(hit.id, {}).setdefault(call.run_key, set()).update(segments)
        for rule_id in sorted(set(runs) | set(unexemptable)):
            if rule_id in unexemptable:
                suggestion.for_review.append(ForReview(rule_id, job, unexemptable[rule_id]))
                continue
            variants = {frozenset(segment for segment, _ in pairs) for pairs in runs[rule_id].values()}
            if len(variants) > 1:
                reason = f"it stops different commands in different runs ({len(variants)} variants)"
                suggestion.for_review.append(ForReview(rule_id, job, reason))
                continue
            segments = tuple(sorted(next(iter(variants))))
            folders = {folder for pairs in runs[rule_id].values() for _, folder in pairs}
            workdir = next(iter(folders)) if len(folders) == 1 else None
            suggestion.exemptions.append(Exemption(rule_id, job, segments, len(runs[rule_id]), workdir))
            if workdir is None:
                reason = "folder not pinned, so its exemption works from any folder: " + _folder_reason(runs[rule_id])
                suggestion.for_review.append(ForReview(rule_id, job, reason, exempted=True))
            entry = (exact_pattern(segments), workdir)
            if entry not in suggestion.patterns.setdefault(rule_id, []):
                suggestion.patterns[rule_id].append(entry)
    return suggestion


def _folder_reason(runs: dict[str, set[tuple[str, str | None]]]) -> str:
    known = sorted({folder for pairs in runs.values() for _, folder in pairs if folder})
    unknown = sum(1 for pairs in runs.values() if any(folder is None for _, folder in pairs))
    reasons = [f"it ran from {len(known)} folders ({', '.join(known)})"] if len(known) > 1 else []
    if unknown:
        reasons.append(f"{unknown} of {len(runs)} run(s) didn't show which folder it ran in (Hermes's terminal keeps"
                       " the folder an earlier call left)")
    return " and ".join(reasons)


def proposed_yaml(source_text: str, suggestion: Suggestion, header: list[str]) -> str:
    """The source policy with each exemption added to its rule's ``unless`` list, comments kept.

    The source's leading comment describes the source, so it's replaced by ``header``.
    """
    lines = source_text.splitlines()
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    data = yaml.load("\n".join(lines) + "\n")
    for rule in data["rules"]:
        entries = suggestion.patterns.get(rule.get("id"))
        if not entries:
            continue
        if "unless" not in rule:
            keys = list(rule)
            after = next((k for k in ("unless_command", "command") if k in keys), None)
            at = keys.index(after) + 1 if after else keys.index("action") if "action" in keys else len(keys)
            rule.insert(at, "unless", CommentedSeq())
        for pattern, workdir in entries:
            entry = CommentedMap(command=SingleQuotedScalarString(pattern))
            if workdir:
                entry["workdir"] = workdir
            rule["unless"].append(entry)
    out = io.StringIO()
    yaml.dump(data, out)
    return "".join(f"# {line}\n" for line in header) + out.getvalue()


def load_proposal(text: str, source: str = "proposed") -> Policy:
    return parse_policy(YAML(typ="safe", pure=True).load(text), source=source)
