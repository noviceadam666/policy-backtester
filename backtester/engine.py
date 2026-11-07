"""The backtest: replay indexed calls through the evaluator, then compare policy versions.

Each call is replayed on its own. The replay can say a call would have been stopped, but not what the
agent would have done next. A session's or run's first stop is exact, since nothing before it was
stopped, so caught sessions and runs hit are exact. Calls after it are marked ``after_first_stop``, and
call counts that include them are upper bounds.

A scheduled job with no completed run in the window can't break in the replay, because there's nothing
of it to replay. Given the jobs each profile has scheduled, the result names those as ``cant_vouch``, so
"breaks no jobs" is never said about a job the backtest didn't see.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .evaluator import evaluate
from .ingest import ScheduledJob
from .policy import Policy

CAUGHT = "caught"
BREAKS = "breaks"


@dataclass
class CallHit:
    profile: str
    session_id: str
    call_uid: str
    ts: float
    tool: str
    args: dict
    status: str | None
    action: str
    rule_ids: tuple[str, ...]
    outcome: str
    run_key: str = ""
    after_first_stop: bool = False


@dataclass
class RuleStats:
    id: str
    action: str
    hits: int = 0
    would_block: int = 0
    would_escalate: int = 0
    incident_sessions: set = field(default_factory=set)
    # Calls the rule's tool and profile scope cover, and ``args`` names none of them carried: a
    # misspelled name never matches, and nothing else would say so.
    checked: int = 0
    unseen_args: list = field(default_factory=list)

    @property
    def incidents(self) -> int:
        return len(self.incident_sessions)


@dataclass
class Incident:
    profile: str
    session_id: str
    source: str | None
    platform: str | None
    title: str | None
    started_at: float | None
    job_name: str | None
    calls: list[CallHit] = field(default_factory=list)

    @property
    def rule_ids(self) -> list[str]:
        return sorted({r for c in self.calls for r in c.rule_ids})


@dataclass
class JobBreak:
    kind: str
    profile: str
    job_id: str
    job_name: str | None
    completed_runs: int
    calls: list[CallHit] = field(default_factory=list)

    @property
    def runs_hit(self) -> int:
        return len({c.run_key for c in self.calls})

    @property
    def rule_ids(self) -> list[str]:
        return sorted({r for c in self.calls for r in c.rule_ids})


@dataclass
class BacktestResult:
    policy: Policy
    since: float | None
    until: float | None
    calls_replayed: int = 0
    sessions_replayed: int = 0
    profiles: tuple[str, ...] = ()
    oldest_call: float | None = None
    newest_call: float | None = None
    rules: list[RuleStats] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    would_break: list[JobBreak] = field(default_factory=list)
    decisions: dict = field(default_factory=dict)
    # None when the caller didn't say which jobs are scheduled, so coverage wasn't checked.
    scheduled: list[ScheduledJob] | None = None
    cant_vouch: list[ScheduledJob] = field(default_factory=list)


def _window_clause(since: float | None, until: float | None, profiles: list[str] | None) -> tuple[str, list]:
    clauses, params = [], []
    if since is not None:
        clauses.append("c.ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("c.ts < ?")
        params.append(until)
    if profiles:
        clauses.append(f"c.profile IN ({','.join('?' * len(profiles))})")
        params.extend(profiles)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def run_backtest(
    index: sqlite3.Connection,
    policy: Policy,
    since: float | None = None,
    until: float | None = None,
    profiles: list[str] | None = None,
    scheduled: list[ScheduledJob] | None = None,
) -> BacktestResult:
    result = BacktestResult(policy, since, until)
    stats = {r.id: RuleStats(r.id, r.action) for r in policy.rules}
    result.rules = list(stats.values())
    where, params = _window_clause(since, until, profiles)
    rows = index.execute(
        f"""
        SELECT c.profile, c.session_id, c.call_uid, c.ts, c.tool, c.args, c.status,
               s.source, s.platform, s.title, s.started_at, s.job_kind, s.job_id, s.job_name, s.run_id, s.run_outcome
        FROM calls c LEFT JOIN sessions s ON s.profile=c.profile AND s.session_id=c.session_id
        {where} ORDER BY c.ts
        """,
        params,
    )
    seen_args: dict[str, set[str]] = {r.id: set() for r in policy.rules if r.args}
    incidents: dict[tuple[str, str], Incident] = {}
    breaks: dict[tuple[str, str], JobBreak] = {}
    sessions: set[tuple[str, str]] = set()
    seen_profiles: set[str] = set()
    for row in rows:
        result.calls_replayed += 1
        sessions.add((row["profile"], row["session_id"]))
        seen_profiles.add(row["profile"])
        result.oldest_call = row["ts"] if result.oldest_call is None else result.oldest_call
        result.newest_call = row["ts"]
        args = json.loads(row["args"])
        for rule in policy.rules:
            if rule.applies_to(row["tool"], row["profile"]):
                stats[rule.id].checked += 1
                if rule.args and isinstance(args, dict):
                    seen_args[rule.id].update(name for name, _ in rule.args if name in args)
        verdict = evaluate(policy, row["tool"], args, row["profile"])
        if not verdict.hits:
            continue
        for rule in verdict.hits:
            stat = stats[rule.id]
            stat.hits += 1
            if rule.action == "block":
                stat.would_block += 1
            elif rule.action == "approve" and verdict.action == "approve":
                stat.would_escalate += 1
        if verdict.action is None:
            continue
        # Nobody answers an approval prompt in an unattended run, so approve breaks a job as block does.
        breaks_job = bool(row["job_kind"]) and row["run_outcome"] == "completed"
        outcome = BREAKS if breaks_job else CAUGHT
        hit = CallHit(
            row["profile"], row["session_id"], row["call_uid"], row["ts"], row["tool"], args, row["status"],
            verdict.action, verdict.rule_ids, outcome, row["run_id"] or row["session_id"],
        )
        result.decisions[(hit.profile, hit.session_id, hit.call_uid)] = hit
        if breaks_job:
            key = (row["profile"], row["job_id"])
            if key not in breaks:
                breaks[key] = JobBreak(
                    row["job_kind"], row["profile"], row["job_id"], row["job_name"],
                    _completed_runs(index, row["profile"], row["job_id"], since, until),
                )
            hit.after_first_stop = any(c.run_key == hit.run_key for c in breaks[key].calls)
            breaks[key].calls.append(hit)
        else:
            key = (row["profile"], row["session_id"])
            if key not in incidents:
                incidents[key] = Incident(
                    row["profile"], row["session_id"], row["source"], row["platform"], row["title"],
                    row["started_at"], row["job_name"],
                )
            hit.after_first_stop = bool(incidents[key].calls)
            incidents[key].calls.append(hit)
            for rule_id in verdict.rule_ids:
                stats[rule_id].incident_sessions.add(key)
    for rule in policy.rules:
        if rule.args and stats[rule.id].checked:
            stats[rule.id].unseen_args = [name for name, _ in rule.args if name not in seen_args[rule.id]]
    result.incidents = sorted(incidents.values(), key=lambda i: i.calls[0].ts)
    result.would_break = sorted(breaks.values(), key=lambda b: b.calls[0].ts)
    result.sessions_replayed = len(sessions)
    result.profiles = tuple(sorted(seen_profiles))
    if scheduled is not None:
        result.scheduled = [j for j in scheduled if not profiles or j.profile in profiles]
        result.cant_vouch = [j for j in result.scheduled if not _completed_runs(index, j.profile, j.job_id, since, until)]
    return result


def _completed_runs(index: sqlite3.Connection, profile: str, job_id: str, since: float | None, until: float | None) -> int:
    # A run is one count even when subagent sessions share it.
    query = "SELECT COUNT(DISTINCT COALESCE(run_id, session_id)) FROM sessions WHERE profile=? AND job_id=? AND run_outcome='completed'"
    params: list = [profile, job_id]
    if since is not None:
        query += " AND started_at >= ?"
        params.append(since)
    if until is not None:
        query += " AND started_at < ?"
        params.append(until)
    return index.execute(query, params).fetchone()[0]


@dataclass
class PolicyDiff:
    old: BacktestResult
    new: BacktestResult
    newly_caught: list[CallHit] = field(default_factory=list)
    newly_broken: list[CallHit] = field(default_factory=list)
    no_longer_matched: list[CallHit] = field(default_factory=list)
    still_caught: list[CallHit] = field(default_factory=list)
    still_broken: list[CallHit] = field(default_factory=list)


def diff_results(old: BacktestResult, new: BacktestResult) -> PolicyDiff:
    """Compare two runs over the same window, call by call."""
    diff = PolicyDiff(old, new)
    for key, hit in new.decisions.items():
        before = old.decisions.get(key)
        if hit.outcome == CAUGHT:
            (diff.still_caught if before and before.outcome == CAUGHT else diff.newly_caught).append(hit)
        else:
            (diff.still_broken if before and before.outcome == BREAKS else diff.newly_broken).append(hit)
    diff.no_longer_matched = [hit for key, hit in old.decisions.items() if key not in new.decisions]
    return diff
