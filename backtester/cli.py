"""``hermes backtest ingest | run | diff | suggest | shadow | enforce | off | log | status | demo``."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .engine import diff_results, run_backtest
from .index import open_index
from .ingest import IngestSummary, ingest
from .live import MODES, decisions, install_policy, promotions, record_promotion
from .paths import default_index, default_root
from .policy import PolicyError, load_policy
from .report import describe_call, diff_to_json, render_diff, render_suggestion, render_table, suggestion_to_json, to_json

_RELATIVE_TIME = re.compile(r"^(\d+)([dh])$")
BUNDLED = Path(__file__).resolve().parent.parent / "policies"
# Exit code when --fail-on-break finds a job the policy would break, so a policy change can fail CI.
BREAKS_EXIT = 3


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-root", type=Path, help="Hermes root that holds every profile (default: this install's)")
    parser.add_argument("--index", type=Path, help="index file (default: <root>/backtest/index.db)")
    sub = parser.add_subparsers(dest="backtest_command", required=True)

    ingest_parser = sub.add_parser("ingest", help="Update the index from each profile's state.db (read-only)")
    ingest_parser.add_argument("--profile", action="append", help="only this profile (repeatable; default: all)")

    run = sub.add_parser("run", help="Replay a policy over history: hits and would-break")
    run.add_argument("policy", type=Path)
    _add_replay_options(run)
    run.add_argument("--aggregate", action="store_true", help="counts, tool names and rule hits only (for fleet views)")

    diff = sub.add_parser("diff", help="Compare two policy versions over the same history")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    _add_replay_options(diff)

    suggest = sub.add_parser("suggest", help="Propose exemptions for the jobs a policy would break, and backtest them; applies nothing")
    suggest.add_argument("policy", type=Path)
    _add_replay_options(suggest)
    suggest.add_argument("--out", type=Path, help="write the proposed policy to this new file instead of printing it")

    for stage, text in (("shadow", "Log what a policy would do on this profile's live calls, changing nothing"),
                        ("enforce", "Enforce a policy on this profile: it must have run in shadow and break no jobs")):
        promote = sub.add_parser(stage, help=text)
        promote.add_argument("policy", type=Path, nargs="?" if stage == "enforce" else None,
                             help="policy file" + (" (default: the one in shadow)" if stage == "enforce" else ""))
        promote.add_argument("--since", default="30d", help="backtest window start (default: 30d)")
        promote.add_argument("--force", action="store_true", help="promote even if the checks fail (recorded)")
        promote.add_argument("--no-ingest", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("off", help="Stop shadow or enforce on this profile")

    log = sub.add_parser("log", help="What shadow or enforce decided on this profile's live calls")
    log.add_argument("--since", help="window start: 24h, 7d or YYYY-MM-DD (default: since the last promotion)")
    log.add_argument("--no-redact", action="store_true", help="show raw commands")
    log.add_argument("--no-ingest", action="store_true", help="count calls from the index as it is")

    sub.add_parser("status", help="What the index holds, per profile, and this profile's live mode")

    demo = sub.add_parser("demo", help="Replay the whole story on a bundled synthetic fleet: no keys, nothing touched")
    demo.add_argument("--keep", type=Path, help="keep the synthetic fleet in this folder instead of a temp one")


def _add_replay_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", action="append", help="only this profile (repeatable; default: all)")
    parser.add_argument("--since", default="30d", help="window start: 30d, 12h or YYYY-MM-DD (default: 30d)")
    parser.add_argument("--until", help="window end: 12h or YYYY-MM-DD (default: now)")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--no-redact", action="store_true", help="show raw commands and session titles")
    parser.add_argument("--no-ingest", action="store_true", help="replay the index as it is, without updating it first")
    parser.add_argument("--fail-on-break", action="store_true", help=f"exit {BREAKS_EXIT} if a job would break")


def dispatch(args: argparse.Namespace, live=None) -> int:
    """``live`` connects the live commands to a Hermes profile: get/set settings, home and profile name."""
    root = (args.hermes_root or default_root()).expanduser()
    index_path = (args.index or default_index(root)).expanduser()
    try:
        if args.backtest_command == "status":
            return _cmd_status(args, root, index_path, live)
        if args.backtest_command in _LIVE_COMMANDS:
            if live is None:
                print("backtest: shadow, enforce, off and log run inside Hermes: hermes -p <profile> backtest ...", file=sys.stderr)
                return 1
            return _LIVE_COMMANDS[args.backtest_command](args, root, index_path, live)
        return _COMMANDS[args.backtest_command](args, root, index_path)
    except (PolicyError, sqlite3.Error, ValueError, OSError) as exc:
        print(f"backtest: {exc}", file=sys.stderr)
        return 1


def _cmd_ingest(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    summary = ingest(root, index_path, args.profile)
    if not summary.profiles:
        print(f"backtest: no matching profile with a state.db under {root}", file=sys.stderr)
        return 1
    _print_ingest(summary, index_path)
    return 1 if any(p.error for p in summary.profiles) else 0


def _print_ingest(summary: IngestSummary, index_path: Path) -> None:
    for row in summary.profiles:
        if row.error:
            print(f"{row.profile:26s} FAILED: {row.error}")
        else:
            print(f"{row.profile:26s} {row.sessions:5d} sessions  {row.sessions_read:5d} read  {row.calls:6d} calls  +{row.new_calls}")
    print(f"Index: {index_path}")


def _refresh(args: argparse.Namespace, root: Path, index_path: Path, profiles: list[str] | None = None) -> None:
    if args.no_ingest:
        return
    summary = ingest(root, index_path, profiles if profiles is not None else getattr(args, "profile", None))
    for row in summary.profiles:
        if row.error:
            print(f"backtest: couldn't read {row.profile}: {row.error}", file=sys.stderr)


def policy_path(path: Path) -> Path:
    """The policy file a command names: as given if it exists, else the bundled one of that name.

    After ``hermes plugins install`` the shipped policies live in the plugin's folder, so
    ``run policies/naive.yaml`` works from anywhere. A file of the same name where you are wins.
    """
    path = path.expanduser()
    if path.exists() or path.is_absolute():
        return path
    name = Path(*path.parts[1:]) if path.parts[:1] == ("policies",) and len(path.parts) > 1 else path
    bundled = BUNDLED / name
    if bundled.is_file():
        print(f"backtest: no {path} here, so using the bundled {bundled}", file=sys.stderr)
        return bundled
    names = ", ".join(sorted(f.name for f in BUNDLED.glob("*.yaml")))
    raise PolicyError(f"can't read policy {path}: no such file here, and the bundled policies are {names}")


def _cmd_run(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    policy = load_policy(policy_path(args.policy))
    _refresh(args, root, index_path)
    index = open_index(index_path)
    try:
        result = run_backtest(index, policy, _parse_time(args.since), _parse_time(args.until), args.profile)
    finally:
        index.close()
    redacted = not args.no_redact
    if args.format == "json":
        print(json.dumps(to_json(result, redacted, args.aggregate), indent=2))
    else:
        print(render_table(result, redacted, args.aggregate))
    return BREAKS_EXIT if args.fail_on_break and result.would_break else 0


def _cmd_diff(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    before, after = load_policy(policy_path(args.before)), load_policy(policy_path(args.after))
    _refresh(args, root, index_path)
    since, until = _parse_time(args.since), _parse_time(args.until)
    index = open_index(index_path)
    try:
        diff = diff_results(
            run_backtest(index, before, since, until, args.profile),
            run_backtest(index, after, since, until, args.profile),
        )
    finally:
        index.close()
    redacted = not args.no_redact
    if args.format == "json":
        print(json.dumps(diff_to_json(diff, redacted), indent=2))
    else:
        print(render_diff(diff, redacted))
    return BREAKS_EXIT if args.fail_on_break and diff.newly_broken else 0


def _cmd_suggest(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    from .suggest import load_proposal, proposed_yaml, suggest

    args.policy = policy_path(args.policy)
    policy = load_policy(args.policy)
    if args.out is not None and args.out.exists():
        raise ValueError(f"{args.out} already exists, and suggest never overwrites a file")
    _refresh(args, root, index_path)
    since, until = _parse_time(args.since), _parse_time(args.until)
    index = open_index(index_path)
    try:
        before = run_backtest(index, policy, since, until, args.profile)
        suggestion = suggest(before)
        text = None
        after = before
        if suggestion.patterns:
            text = proposed_yaml(args.policy.read_text(), suggestion, [
                f"Proposed by hermes backtest suggest from {args.policy.name} (sha256 {policy.hash[:12]}) on {datetime.now():%Y-%m-%d}.",
                "Each unless_command added exempts only the exact command a job ran. Review it, then shadow it.",
            ])
            after = run_backtest(index, load_proposal(text, str(args.out or "proposed")), since, until, args.profile)
    finally:
        index.close()
    redacted = not args.no_redact
    if not before.would_break:
        print(f"Nothing to suggest: {args.policy.name} breaks no jobs in this window.")
        return 0
    diff = diff_results(before, after)
    if args.out is not None and text is not None:
        with open(args.out, "x") as f:
            f.write(text)
    if args.format == "json":
        print(json.dumps(suggestion_to_json(suggestion, diff, None if args.out else text, redacted), indent=2))
    else:
        print(render_suggestion(suggestion, diff, redacted))
        if text is None:
            print("\nNo policy proposed: every break needs a human.")
        elif args.out is not None:
            print(f"\nWrote the proposed policy to {args.out}. Nothing was applied.")
        else:
            print("\nProposed policy, not applied (it holds the exact commands, unredacted)\n")
            print(text.rstrip())
    return BREAKS_EXIT if args.fail_on_break and after.would_break else 0


def _cmd_promote(args: argparse.Namespace, root: Path, index_path: Path, live) -> int:
    stage, home, profile = args.backtest_command, Path(live.home()), live.profile()
    source = policy_path(args.policy) if args.policy else (Path(live.get("policy", None)) if live.get("policy", None) else None)
    if source is None:
        print("backtest: no policy given, and none is in shadow on this profile", file=sys.stderr)
        return 1
    policy = load_policy(source)
    _refresh(args, root, index_path, [profile])
    since = _parse_time(args.since)
    index = open_index(index_path)
    try:
        result = run_backtest(index, policy, since, None, [profile])
    finally:
        index.close()
    broken = [j.job_name or j.job_id for j in result.would_break]
    print(f"Backtest on {profile}, {args.since}: {result.calls_replayed} calls, {len(result.incidents)} session(s) caught,"
          f" {len(broken)} job(s) would break" + (f" ({', '.join(broken)})" if broken else ""))
    problems = [f"it would break {len(broken)} job(s)"] if broken else []
    if stage == "enforce" and not any(p.get("stage") == "shadow" and p.get("policy") == policy.hash for p in promotions(home)):
        problems.append("it hasn't run in shadow on this profile")
    if problems and stage == "enforce" and not args.force:
        print(f"Not enforcing: {' and '.join(problems)}. Pass --force to enforce anyway (recorded).")
        return 1
    if broken and stage == "shadow":
        print("Shadow changes nothing, so it starts anyway; enforce will refuse this policy until it breaks no jobs.")
    already = source.parent == home / "backtest" / "policies"
    installed = source if already else install_policy(home, source, policy)
    # Promoting the installed copy keeps the name the policy was first promoted under.
    name = next((p.get("source") for p in reversed(promotions(home)) if p.get("policy") == policy.hash and p.get("source")),
                source.name) if already else source.name
    live.set("policy", str(installed))
    live.set("policy_sha256", policy.hash)
    live.set("mode", stage)
    record_promotion(home, {
        "stage": stage, "policy": policy.hash, "source": name, "profile": profile, "forced": bool(problems),
        "window_since": since, "calls": result.calls_replayed, "caught": len(result.incidents), "would_break": broken,
    })
    print(f"{stage.capitalize()} is on for {profile}: {name} (sha256 {policy.hash[:12]}), from the next tool call.")
    return 0


def _cmd_off(args: argparse.Namespace, root: Path, index_path: Path, live) -> int:
    home, profile = Path(live.home()), live.profile()
    previous = live.get("mode", "off")
    live.set("mode", "off")
    record_promotion(home, {"stage": "off", "policy": live.get("policy_sha256", None), "profile": profile, "was": previous})
    print(f"Live checks are off for {profile} (was {previous}).")
    return 0


def _cmd_log(args: argparse.Namespace, root: Path, index_path: Path, live) -> int:
    home, profile = Path(live.home()), live.profile()
    mode, sha = live.get("mode", "off"), live.get("policy_sha256", None)
    starts = [p for p in promotions(home) if p.get("stage") in ("shadow", "enforce")]
    since = _parse_time(args.since) if args.since else (starts[-1]["ts"] if starts else None)
    rows = [d for d in decisions(home) if since is None or d.get("ts", 0) >= since]
    source = next((p.get("source") for p in reversed(starts) if p.get("policy") == sha), None)
    print(f"Profile {profile} · mode {mode}" + (f" · {source or 'policy'} (sha256 {sha[:12]})" if sha else ""))
    _refresh(args, root, index_path, [profile])
    jobs, checked = _jobs_since(index_path, profile, since)
    print(f"Since {_when(since)}: {checked} tool calls ran, {sum(1 for d in rows if d.get('action'))} would be stopped")
    stops_by_job: dict[str, int] = {}
    for d in rows:
        job_id = _cron_job(d.get("task_id"))
        if job_id and d.get("action"):
            stops_by_job[job_id] = stops_by_job.get(job_id, 0) + 1
    if jobs:
        print("Scheduled jobs")
        for job_id, (name, runs) in sorted(jobs.items(), key=lambda kv: kv[1][0] or kv[0]):
            stops = stops_by_job.get(job_id, 0)
            print(f"  {name or job_id}: {runs} completed run(s), " + (f"{stops} stop(s)" if stops else "untouched"))
    redacted = not args.no_redact
    print("Decisions")
    if not rows:
        print("  none")
    for d in rows:
        job_id = _cron_job(d.get("task_id"))
        where = (jobs.get(job_id, (None, 0))[0] or f"cron {job_id}") if job_id else "chat"
        verb = {"block": "would block" if d.get("mode") == "shadow" else "blocked",
                "approve": "would ask" if d.get("mode") == "shadow" else "asked"}.get(d.get("action"), "noted")
        call = SimpleNamespace(args=d.get("args") or {})
        print(f"  {_when(d.get('ts'))}  {where}  {verb}  {', '.join(d.get('rules') or [])}  {d.get('tool')}: {describe_call(call, redacted)}")
    return 0


def _cron_job(task_id: str | None) -> str | None:
    if isinstance(task_id, str) and task_id.startswith("cron:"):
        parts = task_id.split(":")
        return parts[1] if len(parts) > 2 else None
    return None


def _jobs_since(index_path: Path, profile: str, since: float | None) -> tuple[dict[str, tuple[str | None, int]], int]:
    index = open_index(index_path)
    try:
        clause, params = ("AND s.started_at >= ?", [since]) if since is not None else ("", [])
        jobs = {
            r["job_id"]: (r["job_name"], r["runs"])
            for r in index.execute(
                f"""SELECT s.job_id, MAX(s.job_name) AS job_name, COUNT(DISTINCT COALESCE(s.run_id, s.session_id)) AS runs
                    FROM sessions s WHERE s.profile=? AND s.job_kind='cron' AND s.run_outcome='completed' {clause}
                    GROUP BY s.job_id""",
                [profile, *params],
            )
        }
        call_clause, call_params = ("AND ts >= ?", [since]) if since is not None else ("", [])
        checked = index.execute(f"SELECT COUNT(*) FROM calls WHERE profile=? {call_clause}", [profile, *call_params]).fetchone()[0]
    finally:
        index.close()
    return jobs, checked


def _cmd_status(args: argparse.Namespace, root: Path, index_path: Path, live=None) -> int:
    if live is not None:
        mode, sha = live.get("mode", "off"), live.get("policy_sha256", None)
        print(f"Live on {live.profile()}: {mode}" + (f", policy sha256 {sha[:12]}" if sha and mode in MODES[1:] else ""))
    if not index_path.exists():
        print(f"No index yet at {index_path}. Run: hermes backtest ingest")
        return 0
    index = open_index(index_path)
    try:
        rows = index.execute(
            """
            SELECT p.profile, p.ingested_at,
                   (SELECT COUNT(*) FROM sessions s WHERE s.profile=p.profile) AS sessions,
                   (SELECT COUNT(*) FROM calls c WHERE c.profile=p.profile) AS calls,
                   (SELECT MIN(started_at) FROM sessions s WHERE s.profile=p.profile AND s.started_at > 0) AS oldest,
                   (SELECT MAX(ts) FROM calls c WHERE c.profile=p.profile) AS newest
            FROM profiles p ORDER BY p.profile
            """
        ).fetchall()
    finally:
        index.close()
    print(f"{'profile':26s} {'sessions':>8s} {'calls':>7s}  {'oldest session':16s}  {'newest call':16s}")
    for r in rows:
        print(f"{r['profile']:26s} {r['sessions']:8d} {r['calls']:7d}  {_day(r['oldest']):16s}  {_day(r['newest']):16s}")
    print(f"Index: {index_path}")
    return 0


def _cmd_demo(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    from .demo import run_demo

    return run_demo(args.keep)


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "the start"


def _parse_time(value: str | None) -> float | None:
    if not value or value == "all":
        return None
    match = _RELATIVE_TIME.match(value)
    if match:
        return time.time() - int(match.group(1)) * (86400 if match.group(2) == "d" else 3600)
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise ValueError(f"can't read time {value!r}: use 30d, 12h or YYYY-MM-DD") from None


_COMMANDS = {"ingest": _cmd_ingest, "run": _cmd_run, "diff": _cmd_diff, "suggest": _cmd_suggest, "status": _cmd_status, "demo": _cmd_demo}
_LIVE_COMMANDS = {"shadow": _cmd_promote, "enforce": _cmd_promote, "off": _cmd_off, "log": _cmd_log}
