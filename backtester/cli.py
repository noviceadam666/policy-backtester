"""``hermes backtest ingest | run | diff | suggest | check | shadow | enforce | off | log | status | demo``."""

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
from .ingest import IngestSummary, ingest, scheduled_jobs
from .live import MODES, check_promoted, decisions, install_policy, promotions, record_promotion
from .paths import default_index, default_root
from .policy import PolicyError, load_policy
from .report import (cant_vouch_lines, cant_vouch_names, describe_call, diff_to_json, render_diff, render_suggestion,
                     render_table, suggestion_to_json, to_json)

_RELATIVE_TIME = re.compile(r"^(\d+)([dh])$")
BUNDLED = Path(__file__).resolve().parent.parent / "policies"
# Under fixtures/, where Hermes's install scan reads the commands they quote as test data.
BUNDLED_CASES = Path(__file__).resolve().parent.parent / "fixtures" / "cases"
# Exit codes for --fail-on-break, so a policy change can fail CI: a job the policy would break, and,
# when none would, a scheduled job with no completed run in the window that the backtest can't vouch for.
BREAKS_EXIT = 3
UNVOUCHED_EXIT = 4


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-root", type=Path, help="Hermes root that holds every profile (default: this install's)")
    parser.add_argument("--index", type=Path, help="index file (default: <root>/backtest/index.db)")
    sub = parser.add_subparsers(dest="backtest_command", required=True)

    ingest_parser = sub.add_parser("ingest", help="Update the index from each profile's state.db (read-only)")
    ingest_parser.add_argument("--profile", action="append", help="only this profile (repeatable; default: all)")

    run = sub.add_parser("run", help="Replay a policy over history: hits and would-break")
    run.add_argument("policy", type=Path)
    _add_replay_options(run)
    run.add_argument("--aggregate", action="store_true",
                     help="counts, tool names and rule hits only, for fleet views: hides job names, commands and titles")

    diff = sub.add_parser("diff", help="Compare two policy versions over the same history")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    _add_replay_options(diff)

    suggest = sub.add_parser("suggest", help="Propose exemptions for the jobs a policy would break, and backtest them; applies nothing")
    suggest.add_argument("policy", type=Path)
    _add_replay_options(suggest)
    suggest.add_argument("--out", type=Path, help="write the proposed policy to this new file instead of printing it")

    check = sub.add_parser("check", help="What a policy decides for one call, or for each call in a cases file (exit 3 on a mismatch)")
    check.add_argument("policy", type=Path)
    check.add_argument("--tool", default="terminal", help="the tool called (default: terminal)")
    check.add_argument("--command", dest="call_command", help="the terminal command")
    check.add_argument("--workdir", help="the folder the command runs in, as the call names it")
    check.add_argument("--args", dest="call_args", help="the call's other arguments, as a JSON object")
    check.add_argument("--profile", dest="call_profile", help="the profile the call runs on, for rules scoped to profiles")
    check.add_argument("--cases", type=Path, help="a YAML list of calls with the action each should get")
    check.add_argument("--format", choices=("table", "json"), default="table")

    for stage, text in (("shadow", "Log what a policy would do on this profile's live calls, changing nothing"),
                        ("enforce", "Enforce a policy on this profile: it must have run in shadow, break no jobs, and have seen each scheduled job run")):
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
    parser.add_argument("--fail-on-break", action="store_true",
                        help=f"exit {BREAKS_EXIT} if a job would break, {UNVOUCHED_EXIT} if a scheduled job never completed a run in the window")


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


def policy_path(path: Path, kind: str = "policy") -> Path:
    """The policy file a command names: as given if it exists, else the bundled one of that name.

    After ``hermes plugins install`` the shipped policies live in the plugin's folder, so
    ``run policies/naive.yaml`` works from anywhere. A file of the same name where you are wins.
    Cases files are bundled in ``fixtures/cases``, and ``cases/refined.yaml`` finds one.
    """
    path = path.expanduser()
    if path.exists() or path.is_absolute():
        return path
    folder = BUNDLED_CASES if kind == "cases" else BUNDLED
    parts = path.parts
    for prefix in ("policies", "fixtures", "cases"):
        if parts[:1] == (prefix,) and len(parts) > 1:
            parts = parts[1:]
    bundled = folder / Path(*parts)
    if bundled.is_file():
        print(f"backtest: no {path} here, so using the bundled {bundled}", file=sys.stderr)
        return bundled
    names = ", ".join(sorted(f.name for f in folder.glob("*.yaml")))
    raise PolicyError(f"can't read {kind} {path}: no such file here, and the bundled {'policies' if kind == 'policy' else 'cases files'} are {names}")


def _cmd_run(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    policy = load_policy(policy_path(args.policy))
    _refresh(args, root, index_path)
    index = open_index(index_path)
    try:
        result = run_backtest(index, policy, _parse_time(args.since), _parse_time(args.until), args.profile,
                              scheduled_jobs(root, args.profile))
    finally:
        index.close()
    redacted = not args.no_redact
    if args.format == "json":
        print(json.dumps(to_json(result, redacted, args.aggregate), indent=2))
    else:
        print(render_table(result, redacted, args.aggregate))
    return _exit_code(args, bool(result.would_break), result)


def _exit_code(args: argparse.Namespace, breaks: bool, result) -> int:
    if not args.fail_on_break:
        return 0
    return BREAKS_EXIT if breaks else UNVOUCHED_EXIT if result.cant_vouch else 0


def _cmd_diff(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    before, after = load_policy(policy_path(args.before)), load_policy(policy_path(args.after))
    _refresh(args, root, index_path)
    since, until = _parse_time(args.since), _parse_time(args.until)
    scheduled = scheduled_jobs(root, args.profile)
    index = open_index(index_path)
    try:
        diff = diff_results(
            run_backtest(index, before, since, until, args.profile, scheduled),
            run_backtest(index, after, since, until, args.profile, scheduled),
        )
    finally:
        index.close()
    redacted = not args.no_redact
    if args.format == "json":
        print(json.dumps(diff_to_json(diff, redacted), indent=2))
    else:
        print(render_diff(diff, redacted))
    return _exit_code(args, bool(diff.newly_broken), diff.new)


def _cmd_suggest(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    from .suggest import load_proposal, proposed_yaml, suggest

    args.policy = policy_path(args.policy)
    policy = load_policy(args.policy)
    if args.out is not None and args.out.exists():
        raise ValueError(f"{args.out} already exists, and suggest never overwrites a file")
    _refresh(args, root, index_path)
    since, until = _parse_time(args.since), _parse_time(args.until)
    scheduled = scheduled_jobs(root, args.profile)
    index = open_index(index_path)
    try:
        before = run_backtest(index, policy, since, until, args.profile, scheduled)
        suggestion = suggest(before)
        text = None
        after = before
        if suggestion.patterns:
            text = proposed_yaml(args.policy.read_text(), suggestion, [
                f"Proposed by hermes backtest suggest from {args.policy.name} (sha256 {policy.hash[:12]}) on {datetime.now():%Y-%m-%d}.",
                "Each unless_command added exempts only the exact command a job ran. Review it, then shadow it.",
            ])
            after = run_backtest(index, load_proposal(text, str(args.out or "proposed")), since, until, args.profile, scheduled)
    finally:
        index.close()
    redacted = not args.no_redact
    if not before.would_break:
        print(f"Nothing to suggest: {args.policy.name} breaks no jobs that ran in this window.")
        print("\n".join(cant_vouch_lines(before)).lstrip("\n"))
        return _exit_code(args, False, before)
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
    return _exit_code(args, bool(after.would_break), after)


def _cmd_check(args: argparse.Namespace, root: Path, index_path: Path) -> int:
    from .check import check_call, check_cases, load_cases, make_call, render_call, render_cases

    policy = load_policy(policy_path(args.policy))
    if args.cases is not None:
        if args.call_command or args.workdir or args.call_args:
            raise ValueError("check takes either --cases or one call, not both")
        results = check_cases(policy, load_cases(policy_path(args.cases, "cases")))
        print(json.dumps([r.to_json() for r in results], indent=2) if args.format == "json" else render_cases(policy, args.cases, results))
        return BREAKS_EXIT if any(r.status == "mismatch" for r in results) else 0
    call = make_call(args.call_command, args.workdir, args.call_args)
    if not call:
        raise ValueError("check needs a call: --command, --args or both (or --cases FILE)")
    verdict = check_call(policy, args.tool, call, args.call_profile)
    if args.format == "json":
        print(json.dumps({"tool": args.tool, "args": call, "decision": verdict.action or "allow", "message": verdict.message,
                          "rules": [{"id": r.id, "action": r.action} for r in verdict.hits]}, indent=2))
    else:
        print(render_call(policy, args.tool, call, verdict))
    return 0


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
        result = run_backtest(index, policy, since, None, [profile], scheduled_jobs(root, [profile]))
    finally:
        index.close()
    broken = [j.job_name or j.job_id for j in result.would_break]
    unvouched = cant_vouch_names(result)
    print(f"Backtest on {profile}, {args.since}: {result.calls_replayed} calls, {len(result.incidents)} session(s) caught,"
          f" {len(broken)} job(s) would break" + (f" ({', '.join(broken)})" if broken else ""))
    if not result.calls_replayed:
        print(f"Warning: {profile} has no tool calls in this window, so this backtest checked nothing. A clean result here"
              " says nothing about the policy yet.")
    if unvouched:
        print(f"Can't vouch for {len(unvouched)} scheduled job(s) on {profile}: {', '.join(unvouched)}. None has completed a run"
              " in this window, so the backtest never saw them work.")
    problems = [f"it would break {len(broken)} job(s)"] if broken else []
    if unvouched:
        problems.append(f"it can't vouch for {len(unvouched)} scheduled job(s) that haven't completed a run in this window")
    if stage == "enforce" and not any(p.get("stage") == "shadow" and p.get("policy") == policy.hash for p in promotions(home)):
        problems.append("it hasn't run in shadow on this profile")
    if problems and stage == "enforce" and not args.force:
        advice = " Keep it in shadow until each job has completed a run, then enforce again." if unvouched else ""
        print(f"Not enforcing: {' and '.join(problems)}.{advice} Pass --force to enforce anyway (recorded).")
        return 1
    if (broken or unvouched) and stage == "shadow":
        until = "it breaks no jobs" + (" and each scheduled job has completed a run" if unvouched else "")
        print(f"Shadow changes nothing, so it starts anyway; enforce will refuse this policy until {until}.")
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
        "cant_vouch": unvouched,
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
    stopped = [d for d in rows if d.get("action")]
    enforced = sum(1 for d in stopped if d.get("mode") == "enforce")
    counts = [f"{enforced} were stopped"] if enforced or mode == "enforce" else []
    if len(stopped) > enforced or mode != "enforce":
        counts.append(f"{len(stopped) - enforced} would be stopped")
    print(f"Since {_when(since)}: {checked} tool calls recorded, {', '.join(counts)}")
    failures = [d for d in rows if d.get("error")]
    if failures:
        blocked = sum(1 for d in failures if d.get("action") == "block")
        what = f"blocked {blocked} call(s)" if blocked else f"let {len(failures)} call(s) through unchecked"
        print(f"Warning: the policy couldn't be checked, so the hook {what}: {failures[-1]['error']}."
              f" Run: hermes -p {profile} backtest status")
    stops_by_job: dict[str, int] = {}
    for d in stopped:
        job_id = _cron_job(d.get("task_id"))
        if job_id:
            stops_by_job[job_id] = stops_by_job.get(job_id, 0) + 1
    never_ran = {j.job_id: j.name for j in scheduled_jobs(root, [profile]) if j.job_id not in jobs}
    if jobs or never_ran:
        print("Scheduled jobs")
        for job_id, (name, runs) in sorted(jobs.items(), key=lambda kv: kv[1][0] or kv[0]):
            stops = stops_by_job.get(job_id, 0)
            print(f"  {name or job_id}: {runs} completed run(s), " + (f"{stops} stop(s)" if stops else "untouched"))
        for job_id, name in sorted(never_ran.items(), key=lambda kv: kv[1] or kv[0]):
            stops = stops_by_job.get(job_id, 0)
            print(f"  {name or job_id}: no completed run since {_when(since)}" + (f", {stops} stop(s)" if stops else ""))
    redacted = not args.no_redact
    print("Decisions")
    if not rows:
        print("  none")
    for d in rows:
        job_id = _cron_job(d.get("task_id"))
        where = (jobs.get(job_id, (never_ran.get(job_id),))[0] or f"cron {job_id}") if job_id else "chat"
        shadow = d.get("mode") == "shadow"
        verb = {"block": "would block" if shadow else "blocked", "approve": "would ask" if shadow else "asked"}.get(
            d.get("action"), "unchecked" if d.get("error") else "noted")
        rules = ", ".join(d.get("rules") or []) or ("policy couldn't be checked" if d.get("error") else "")
        call = SimpleNamespace(args=d.get("args") or {})
        print(f"  {_when(d.get('ts'))}  {where}  {verb}  {rules}  {d.get('tool')}: {describe_call(call, redacted)}")
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
        if mode in MODES[1:]:
            _print_live_problem(live, mode)
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


def _print_live_problem(live, mode: str) -> None:
    """Say plainly when the promoted policy can't be checked: enforce is blocking every call, shadow none."""
    profile, path = live.profile(), live.get("policy", None)
    try:
        if not path:
            raise PolicyError("no policy is promoted")
        check_promoted(Path(path), live.get("policy_sha256", None))
        return
    except PolicyError as exc:
        problem = str(exc)
    starts = [p for p in promotions(Path(live.home())) if p.get("stage") in MODES[1:]]
    since = starts[-1]["ts"] if starts else 0
    failed = sum(1 for d in decisions(Path(live.home())) if d.get("error") and d.get("ts", 0) >= since)
    effect = ("every tool call on this profile is being blocked" if mode == "enforce"
              else "shadow is checking nothing, and every call goes through unchecked")
    print(f"  But the promoted policy can't be checked, so {effect}: {problem}."
          + (f" {failed} call(s) so far since {_when(since)}." if failed else ""))
    print(f"  Promote it again from its source file (hermes -p {profile} backtest {mode} FILE), or turn it off"
          f" (hermes -p {profile} backtest off).")


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


_COMMANDS = {"ingest": _cmd_ingest, "run": _cmd_run, "diff": _cmd_diff, "suggest": _cmd_suggest, "check": _cmd_check,
             "status": _cmd_status, "demo": _cmd_demo}
_LIVE_COMMANDS = {"shadow": _cmd_promote, "enforce": _cmd_promote, "off": _cmd_off, "log": _cmd_log}
