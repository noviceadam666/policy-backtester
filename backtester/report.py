"""Render backtest results as a CLI table or JSON. Redacted by default.

Redacted output drops session titles, which echo the user's request, and Kanban board and task names,
which can name clients. ``aggregate`` goes further for fleet views: counts, tool names and rule hits only.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .engine import BacktestResult, CallHit, Incident, JobBreak, PolicyDiff
from .redact import redact

_SNIPPET = 110


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def describe_call(call: CallHit, redacted: bool = True) -> str:
    args = call.args
    text = args.get("command") or args.get("path") or args.get("file_path")
    text = str(text) if text else json.dumps(args, sort_keys=True)
    text = " ".join(text.split())
    if redacted:
        text = redact(text)
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 1] + "…"


def job_label(job: JobBreak, redacted: bool = True) -> str:
    if job.kind == "kanban" and redacted:
        return f"kanban task {job.job_id.rsplit('/', 1)[-1]}"
    return job.job_name or f"{job.kind} {job.job_id}"


def _status_note(call: CallHit) -> str:
    return {"denied": "Hermes denied it", "error": "failed", "ok": "ran", None: "no result"}.get(call.status, call.status)


def _policy_label(result: BacktestResult, redacted: bool) -> str:
    return Path(result.policy.source).name if redacted else result.policy.source


def _table(rows: list[list[str]], indent: str = "  ") -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [indent + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


def _coverage(result: BacktestResult) -> str:
    if result.oldest_call is None:
        return "no calls in this window"
    days = (result.newest_call - result.oldest_call) / 86400
    return f"{_when(result.oldest_call)} to {_when(result.newest_call)} ({days:.1f} days of history)"


def render_table(result: BacktestResult, redacted: bool = True, aggregate: bool = False) -> str:
    lines = [
        f"Policy   {_policy_label(result, redacted)}  (sha256 {result.policy.hash[:12]})",
        f"History  {_coverage(result)}",
        f"Replayed {result.calls_replayed} calls in {result.sessions_replayed} sessions"
        f" across {len(result.profiles)} profile(s)",
        "",
        "Rules",
    ]
    rows = [["id", "action", "hits", "would-block", "would-escalate", "incidents"]]
    rows += [[r.id, r.action, str(r.hits), str(r.would_block), str(r.would_escalate), str(r.incidents)] for r in result.rules]
    lines += _table(rows)
    if result.calls_replayed == 0:
        lines += ["", "No tool calls in this window yet. Hermes records them as your agents work; to see the",
                  "whole story on a synthetic fleet now, run: hermes backtest demo"]
    if any(c.after_first_stop for c in result.decisions.values()):
        lines += ["", "Caught sessions and job runs are exact. Calls marked 'after first stop' are what the agent did",
                  "with nothing blocked, so the call counts above are upper bounds."]
    lines += ["", f"Caught: {len(result.incidents)} session(s)"]
    if aggregate:
        lines += _aggregate_lines(result.incidents)
    else:
        for incident in result.incidents:
            lines += _incident_lines(incident, redacted)
    lines += ["", f"Would break: {len(result.would_break)} job(s)"]
    for job in result.would_break:
        lines += _job_lines(job, redacted, aggregate)
    return "\n".join(lines)


def _aggregate_lines(incidents: list[Incident]) -> list[str]:
    tools = Counter(c.tool for i in incidents for c in i.calls)
    return ["  calls by tool: " + ", ".join(f"{t} {n}" for t, n in tools.most_common())] if tools else []


def _incident_lines(incident: Incident, redacted: bool) -> list[str]:
    head = f"  {_when(incident.calls[0].ts)}  {incident.profile}  {incident.source or '?'}  {', '.join(incident.rule_ids)}"
    if incident.title and not redacted:
        head += f"  \"{incident.title}\""
    lines = [head]
    for call in incident.calls:
        note = _status_note(call) + (" · after first stop" if call.after_first_stop else "")
        lines.append(f"    {call.action:7s} {call.tool}: {describe_call(call, redacted)}  [{note}]")
    return lines


def _job_lines(job: JobBreak, redacted: bool, aggregate: bool) -> list[str]:
    verb = "blocked" if all(c.action == "block" for c in job.calls) else "stopped"
    if aggregate:
        return [f"  {job.kind} job on {job.profile}: {verb} in {job.runs_hit} of {job.completed_runs} completed runs · {', '.join(job.rule_ids)}"]
    head = f"  {job_label(job, redacted)} ({job.kind}, {job.profile}): {verb} in {job.runs_hit} of {job.completed_runs} completed runs · {', '.join(job.rule_ids)}"
    samples = list(dict.fromkeys(f"{c.tool}: {describe_call(c, redacted)}" for c in job.calls))
    return [head] + [f"    {s}" for s in samples]


def _hit_json(call: CallHit, redacted: bool) -> dict:
    return {
        "profile": call.profile,
        "session_id": call.session_id,
        "ts": call.ts,
        "tool": call.tool,
        "summary": describe_call(call, redacted),
        "status": call.status,
        "action": call.action,
        "rules": list(call.rule_ids),
        "after_first_stop": call.after_first_stop,
    }


def to_json(result: BacktestResult, redacted: bool = True, aggregate: bool = False) -> dict:
    report: dict = {
        "generated_at": time.time(),
        "redacted": redacted,
        "policy": {"source": _policy_label(result, redacted), "sha256": result.policy.hash},
        "window": {"since": result.since, "until": result.until, "oldest_call": result.oldest_call, "newest_call": result.newest_call},
        "totals": {"calls": result.calls_replayed, "sessions": result.sessions_replayed, "profiles": len(result.profiles)},
        "rules": [
            {"id": r.id, "action": r.action, "hits": r.hits, "would_block": r.would_block, "would_escalate": r.would_escalate, "incidents": r.incidents}
            for r in result.rules
        ],
        "caught": len(result.incidents),
        "would_break": len(result.would_break),
    }
    if aggregate:
        return report
    report["incidents"] = [
        {
            "profile": i.profile,
            "session_id": i.session_id,
            "source": i.source,
            "platform": i.platform,
            "title": None if redacted else i.title,
            "rules": i.rule_ids,
            "calls": [_hit_json(c, redacted) for c in i.calls],
        }
        for i in result.incidents
    ]
    report["jobs"] = [
        {
            "kind": j.kind,
            "profile": j.profile,
            "job": job_label(j, redacted),
            "runs_hit": j.runs_hit,
            "completed_runs": j.completed_runs,
            "rules": j.rule_ids,
            "calls": [_hit_json(c, redacted) for c in j.calls],
        }
        for j in result.would_break
    ]
    return report


def _diff_group(title: str, hits: list[CallHit], redacted: bool) -> list[str]:
    lines = [f"{title}: {len(hits)} call(s)"]
    for sample, count in Counter(f"{c.profile}  {', '.join(c.rule_ids)}  {c.tool}: {describe_call(c, redacted)}" for c in hits).items():
        lines.append(f"  {sample}" + (f"  (x{count})" if count > 1 else ""))
    return lines


def render_diff(diff: PolicyDiff, redacted: bool = True) -> str:
    old, new = diff.old, diff.new
    rows = [
        ["", "before", "after"],
        ["caught sessions", str(len(old.incidents)), str(len(new.incidents))],
        ["jobs broken", str(len(old.would_break)), str(len(new.would_break))],
    ]
    lines = [
        f"Before  {_policy_label(old, redacted)}  (sha256 {old.policy.hash[:12]})",
        f"After   {_policy_label(new, redacted)}  (sha256 {new.policy.hash[:12]})",
        f"History {_coverage(new)}",
        "",
    ]
    lines += _table(rows)
    lines.append("")
    lines += _diff_group("Newly caught", diff.newly_caught, redacted)
    lines += _diff_group("Newly broken", diff.newly_broken, redacted)
    lines += _diff_group("No longer matched", diff.no_longer_matched, redacted)
    lines.append(f"Unchanged: {len(diff.still_caught)} caught, {len(diff.still_broken)} still breaking")
    return "\n".join(lines)


def diff_to_json(diff: PolicyDiff, redacted: bool = True) -> dict:
    groups = {
        "newly_caught": diff.newly_caught,
        "newly_broken": diff.newly_broken,
        "no_longer_matched": diff.no_longer_matched,
        "still_caught": diff.still_caught,
        "still_broken": diff.still_broken,
    }
    return {
        "before": {"source": _policy_label(diff.old, redacted), "sha256": diff.old.policy.hash, "caught": len(diff.old.incidents), "would_break": len(diff.old.would_break)},
        "after": {"source": _policy_label(diff.new, redacted), "sha256": diff.new.policy.hash, "caught": len(diff.new.incidents), "would_break": len(diff.new.would_break)},
        **{name: [_hit_json(c, redacted) for c in hits] for name, hits in groups.items()},
    }
