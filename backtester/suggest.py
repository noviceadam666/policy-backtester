"""``hermes backtest suggest``: propose the exemptions that would stop a policy breaking jobs, for a human.

For each job the policy would break, it finds the command segments each rule stopped and exempts
exactly those: the segment's words escaped, joined by ``\\s+`` and anchored to the whole segment, the
way ``policies/refined.yaml`` scopes its two rules. It does that only when every run that hit the rule
stopped on the same segments. A command that changes between runs, or a call that isn't a terminal
command, is left for a human, because widening a regex until it fits is how a guardrail stops guarding.

It never applies anything. It returns a policy file to review, and the caller backtests it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from ruamel.yaml import YAML
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


@dataclass
class ForReview:
    rule_id: str
    job: JobBreak
    reason: str


@dataclass
class Suggestion:
    source: Policy
    exemptions: list[Exemption] = field(default_factory=list)
    for_review: list[ForReview] = field(default_factory=list)
    patterns: dict[str, str] = field(default_factory=dict)


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
    exempt: dict[str, set[str]] = {}
    for job in result.would_break:
        runs: dict[str, dict[str, set[str]]] = {}
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
            variants = {frozenset(s) for s in runs[rule_id].values()}
            if len(variants) > 1:
                reason = f"it stops different commands in different runs ({len(variants)} variants)"
                suggestion.for_review.append(ForReview(rule_id, job, reason))
                continue
            segments = tuple(sorted(next(iter(variants))))
            suggestion.exemptions.append(Exemption(rule_id, job, segments, len(runs[rule_id])))
            exempt.setdefault(rule_id, set()).update(segments)
    for rule_id, segments in exempt.items():
        pattern = exact_pattern(sorted(segments))
        old = rules[rule_id].unless_command
        suggestion.patterns[rule_id] = f"(?:{old.pattern})|{pattern}" if old is not None else pattern
    return suggestion


def proposed_yaml(source_text: str, suggestion: Suggestion, header: list[str]) -> str:
    """The source policy with each exemption written as its rule's ``unless_command``, comments kept.

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
        pattern = suggestion.patterns.get(rule.get("id"))
        if pattern is None:
            continue
        value = SingleQuotedScalarString(pattern)
        if "unless_command" in rule:
            rule["unless_command"] = value
        else:
            keys = list(rule)
            at = keys.index("command") + 1 if "command" in keys else keys.index("action") if "action" in keys else len(keys)
            rule.insert(at, "unless_command", value)
    out = io.StringIO()
    yaml.dump(data, out)
    return "".join(f"# {line}\n" for line in header) + out.getvalue()


def load_proposal(text: str, source: str = "proposed") -> Policy:
    return parse_policy(YAML(typ="safe", pure=True).load(text), source=source)
