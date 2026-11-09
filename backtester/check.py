"""``hermes backtest check``: what a policy decides for one call, or for each call in a cases file.

It needs no history and no Hermes. History only shows the calls your agents happened to make; a check
asks about the one you're worried about. A cases file is a YAML list of calls, each with the action it
should get, so CI can fail a policy change that stops stopping something. A case marked ``known_miss``
is a gap the policy is known to have: it's reported every time but doesn't fail the run, and it says
so when a policy starts catching it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .evaluator import Verdict, evaluate
from .policy import Policy, PolicyError
from .report import describe_call

EXPECT = ("block", "approve", "allow")
_CASE_FIELDS = frozenset({"name", "tool", "command", "workdir", "args", "profile", "expect", "known_miss"})
_DECISION = {
    "block": "block",
    "approve": "approve: Hermes asks a human first",
    None: "allow: no rule objects",
}


@dataclass(frozen=True)
class Case:
    name: str | None
    tool: str
    args: dict
    profile: str | None
    expect: str
    known_miss: str | None = None


@dataclass(frozen=True)
class CaseResult:
    case: Case
    verdict: Verdict

    @property
    def got(self) -> str:
        return self.verdict.action or "allow"

    @property
    def status(self) -> str:
        matched = self.got == self.case.expect
        if self.case.known_miss:
            return "now_caught" if matched else "known_miss"
        return "ok" if matched else "mismatch"

    def to_json(self) -> dict:
        return {
            "name": self.case.name, "tool": self.case.tool, "args": self.case.args, "profile": self.case.profile,
            "expect": self.case.expect, "got": self.got, "status": self.status, "known_miss": self.case.known_miss,
            "rules": [{"id": r.id, "action": r.action} for r in self.verdict.hits],
        }


def make_call(command: str | None, workdir: str | None, args_json: str | None) -> dict:
    """A call's arguments from ``--args`` JSON, with ``--command`` and ``--workdir`` on top."""
    call: dict = {}
    if args_json:
        try:
            call = json.loads(args_json)
        except ValueError as exc:
            raise ValueError(f"--args isn't valid JSON: {exc}") from None
        if not isinstance(call, dict):
            raise ValueError("--args must be a JSON object of argument names to values")
    if command is not None:
        call["command"] = command
    if workdir is not None:
        call["workdir"] = workdir
    return call


def check_call(policy: Policy, tool: str, args: dict, profile: str | None = None) -> Verdict:
    return evaluate(policy, tool, args, profile)


def load_cases(path: Path) -> list[Case]:
    try:
        data = YAML(typ="safe", pure=True).load(Path(path).read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"can't read cases {path}: {exc}") from exc
    except YAMLError as exc:
        raise PolicyError(f"{path} isn't valid YAML: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise PolicyError(f"{path}: a cases file is a list of calls, each with an 'expect'")
    return [_parse_case(raw, f"{path} case {n}") for n, raw in enumerate(data, 1)]


def _parse_case(raw: object, where: str) -> Case:
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: a case is a mapping with 'expect' and a call")
    extra = set(raw) - _CASE_FIELDS
    if extra:
        raise PolicyError(f"{where}: unknown field(s): {', '.join(sorted(extra))}")
    if raw.get("expect") not in EXPECT:
        raise PolicyError(f"{where}: 'expect' must be one of {', '.join(EXPECT)}")
    args = raw.get("args", {})
    if not isinstance(args, dict):
        raise PolicyError(f"{where}: 'args' must map argument names to values")
    args = dict(args)
    for field in ("command", "workdir"):
        if field in raw:
            if not isinstance(raw[field], str):
                raise PolicyError(f"{where}: '{field}' must be a string")
            args[field] = raw[field]
    if not args:
        raise PolicyError(f"{where}: give the call a 'command' or 'args'")
    for field in ("name", "tool", "profile", "known_miss"):
        if field in raw and not isinstance(raw[field], str):
            raise PolicyError(f"{where}: '{field}' must be a string")
    return Case(raw.get("name"), raw.get("tool", "terminal"), args, raw.get("profile"), raw["expect"], raw.get("known_miss"))


def check_cases(policy: Policy, cases: list[Case]) -> list[CaseResult]:
    return [CaseResult(case, evaluate(policy, case.tool, case.args, case.profile)) for case in cases]


def _call_text(tool: str, args: dict) -> str:
    text = f"{tool}: {describe_call(SimpleNamespace(args=args), redacted=False)}"
    if args.get("command") and args.get("workdir"):
        text += f"  (in {args['workdir']})"
    return text


def _rules(verdict: Verdict) -> str:
    return ", ".join(f"{r.id} ({r.action})" for r in verdict.hits)


def render_call(policy: Policy, tool: str, args: dict, verdict: Verdict) -> str:
    decision = _DECISION[verdict.action]
    if verdict.action:
        decision += f": {verdict.message}" if verdict.action == "block" else f". {verdict.message}"
    lines = [
        f"Policy    {Path(policy.source).name}  (sha256 {policy.hash[:12]})",
        f"Call      {_call_text(tool, args)}",
        f"Decision  {decision}",
    ]
    if verdict.hits:
        lines.append(f"Rules     {_rules(verdict)}")
    return "\n".join(lines)


_LABEL = {"ok": "ok", "mismatch": "MISMATCH", "known_miss": "known miss", "now_caught": "now caught"}


def render_cases(policy: Policy, source: Path, results: list[CaseResult]) -> str:
    lines = [f"Cases   {Path(source).name}: {len(results)} call(s) against {Path(policy.source).name}"
             f"  (sha256 {policy.hash[:12]})", ""]
    for r in results:
        lines.append(f"  {_LABEL[r.status]:10s}  {r.got:7s}  {_call_text(r.case.tool, r.case.args)}")
        note = {
            "mismatch": f"expected {r.case.expect}",
            "known_miss": f"expected {r.case.expect}: {r.case.known_miss}",
            "now_caught": f"marked known_miss ({r.case.known_miss}), but this policy gets it right",
        }.get(r.status)
        if note:
            lines.append(f"  {'':10s}  {'':7s}  {note}" + (f"; rules: {_rules(r.verdict)}" if r.verdict.hits else ""))
    counts = {status: sum(1 for r in results if r.status == status) for status in _LABEL}
    lines += ["", f"{counts['ok']} match, {counts['mismatch']} mismatch(es), {counts['known_miss']} known miss(es)"
              + (f", {counts['now_caught']} known miss(es) now caught" if counts["now_caught"] else "")]
    return "\n".join(lines)


@dataclass(frozen=True)
class CaseSummary:
    """The other side of a backtest: of the calls a policy should stop, how many it misses, and of the
    routine ones, how many it lets through."""

    source: Path
    results: list[CaseResult]

    @property
    def should_stop(self) -> list[CaseResult]:
        return [r for r in self.results if r.case.expect != "allow"]

    @property
    def routine(self) -> list[CaseResult]:
        return [r for r in self.results if r.case.expect == "allow"]

    @property
    def missed(self) -> list[CaseResult]:
        return [r for r in self.should_stop if r.got == "allow"]

    @property
    def over_stopped(self) -> list[CaseResult]:
        return [r for r in self.routine if r.got != "allow"]

    def to_json(self) -> dict:
        return {
            "source": Path(self.source).name, "cases": len(self.results),
            "should_stop": len(self.should_stop), "stopped": len(self.should_stop) - len(self.missed),
            "routine": len(self.routine), "let_through": len(self.routine) - len(self.over_stopped),
            "missed": [r.to_json() for r in self.missed], "over_stopped": [r.to_json() for r in self.over_stopped],
        }


def summarize_cases(policy: Policy, source: Path, cases: list[Case]) -> CaseSummary:
    return CaseSummary(Path(source), check_cases(policy, cases))


def _case_label(r: CaseResult) -> str:
    return r.case.name or _call_text(r.case.tool, r.case.args)


def render_case_summary(summary: CaseSummary, brief: bool = False) -> list[str]:
    """Both sides of the cases: what the policy misses of the calls it should stop, and what it stops of
    the routine ones. ``brief`` is ``try``'s version, under one heading."""
    stop, routine, missed, over = summary.should_stop, summary.routine, summary.missed, summary.over_stopped
    misses = [_case_label(r) + (" (known gap)" if r.case.known_miss else "") for r in missed]
    stops = [f"{_case_label(r)}: {r.got}, {_rules(r.verdict)}" for r in over]
    if brief:
        through = f", and lets {len(routine) - len(over)} of {len(routine)} routine ones through" if routine else ""
        return ([f"Would miss    {len(missed)} of {len(stop)} known calls it should stop{through}"
                 f" ({summary.source.name}: each checked, none run)"]
                + [f"  {m}" for m in misses] + [f"  stops, but shouldn't: {s}" for s in stops])
    lines = [f"Known cases: {summary.source.name}, {len(summary.results)} calls checked one by one (none is run)"]
    if stop:
        lines.append(f"  Stops {len(stop) - len(missed)} of {len(stop)} calls it should stop." + (f" Misses {len(missed)}:" if missed else ""))
        lines += [f"    {m}" for m in misses]
    if routine:
        lines.append(f"  Lets {len(routine) - len(over)} of {len(routine)} routine calls through."
                     + (f" Stops {len(over)} it shouldn't:" if over else ""))
        lines += [f"    {s}" for s in stops]
    return lines
