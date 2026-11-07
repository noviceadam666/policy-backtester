"""Scheduled jobs the backtest never saw run: named, gated in CI and at enforce, never "breaks no jobs"."""

import argparse
import json
from pathlib import Path

import pytest

from backtester.cli import BREAKS_EXIT, UNVOUCHED_EXIT, dispatch, setup_parser
from backtester.ingest import scheduled_jobs
from backtester.live import LiveGuard, MemorySettings, decisions, promotions
from backtester.report import span
from fakehermes import stage_story

POLICIES = Path(__file__).resolve().parent.parent / "policies"
NAIVE, REFINED = POLICIES / "naive.yaml", POLICIES / "refined.yaml"


def cli(fleet, live, *argv):
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    return dispatch(parser.parse_args(["--hermes-root", str(fleet.root), *argv]), live)


def define_jobs(home: Path, *jobs: dict) -> None:
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": list(jobs)}))


@pytest.fixture
def new_app(fleet):
    """A profile with two scheduled jobs that never completed a run, and no history at all."""
    home = fleet.root / "profiles" / "relnotes"
    define_jobs(home, {"id": "j1", "name": "cache-cleanup", "enabled": True, "state": "scheduled"},
                {"id": "j2", "name": "changelog-push", "enabled": True, "state": "scheduled"},
                {"id": "j3", "name": "paused-job", "enabled": False, "state": "paused"},
                {"id": "j4", "name": "one-shot-done", "enabled": False, "state": "completed"})
    return home


@pytest.fixture
def ops(fleet):
    profile = fleet.profile("demo-ops")
    stage_story(profile)
    return profile


def test_scheduled_jobs_counts_enabled_unfinished_jobs_even_without_history(fleet, new_app):
    assert [(j.profile, j.name) for j in scheduled_jobs(fleet.root)] == [("relnotes", "cache-cleanup"), ("relnotes", "changelog-push")]
    assert scheduled_jobs(fleet.root, ["other"]) == []


def test_run_names_jobs_it_cant_vouch_for_and_fails_ci(fleet, ops, new_app, capsys):
    assert cli(fleet, None, "run", str(REFINED), "--profile", "relnotes", "--fail-on-break") == UNVOUCHED_EXIT
    out = capsys.readouterr().out
    assert "Jobs     2 scheduled, 0 with a completed run in this window" in out
    assert "Can't vouch for: 2 scheduled job(s) with no completed run in this window" in out
    assert "cache-cleanup (cron, relnotes)" in out and "changelog-push (cron, relnotes)" in out
    assert "checked nothing" in out
    assert cli(fleet, None, "run", str(REFINED), "--profile", "relnotes") == 0
    capsys.readouterr()
    assert cli(fleet, None, "run", str(REFINED), "--profile", "demo-ops", "--fail-on-break") == 0
    assert "Can't vouch" not in capsys.readouterr().out


def test_a_break_outranks_an_unvouched_job(fleet, ops, new_app):
    assert cli(fleet, None, "run", str(NAIVE), "--fail-on-break") == BREAKS_EXIT
    assert cli(fleet, None, "run", str(REFINED), "--fail-on-break") == UNVOUCHED_EXIT


def test_json_and_aggregate_carry_the_unvouched_jobs(fleet, ops, new_app, capsys):
    cli(fleet, None, "run", str(REFINED), "--format", "json")
    report = json.loads(capsys.readouterr().out)
    assert report["scheduled_jobs"] == 5 and report["cant_vouch"] == 2
    assert [j["job"] for j in report["cant_vouch_jobs"]] == ["cache-cleanup", "changelog-push"]
    cli(fleet, None, "run", str(REFINED), "--format", "json", "--aggregate", "--no-ingest")
    assert json.loads(capsys.readouterr().out)["cant_vouch_by_profile"] == {"relnotes": 2}
    cli(fleet, None, "run", str(REFINED), "--aggregate", "--no-ingest")
    out = capsys.readouterr().out
    assert "job names, commands and session titles are hidden" in out
    assert "relnotes 2" in out and "cache-cleanup" not in out


def test_diff_and_suggest_say_it_too(fleet, ops, new_app, capsys):
    assert cli(fleet, None, "diff", str(NAIVE), str(REFINED), "--fail-on-break") == UNVOUCHED_EXIT
    assert "Can't vouch for: 2" in capsys.readouterr().out
    assert cli(fleet, None, "suggest", str(REFINED), "--profile", "relnotes", "--fail-on-break", "--no-ingest") == UNVOUCHED_EXIT
    out = capsys.readouterr().out
    assert "Nothing to suggest: refined.yaml breaks no jobs that ran" in out and "changelog-push (cron, relnotes)" in out


def test_shadow_warns_and_enforce_refuses_until_each_job_ran(fleet, ops, new_app, capsys):
    live = MemorySettings(new_app, "relnotes")
    assert cli(fleet, live, "shadow", str(REFINED)) == 0
    out = capsys.readouterr().out
    assert "Warning: relnotes has no tool calls in this window, so this backtest checked nothing" in out
    assert "Can't vouch for 2 scheduled job(s) on relnotes: cache-cleanup, changelog-push" in out
    assert "until it breaks no jobs and each scheduled job has completed a run" in out
    assert cli(fleet, live, "enforce") == 1
    out = capsys.readouterr().out
    assert "Not enforcing: it can't vouch for 2 scheduled job(s)" in out
    assert "Keep it in shadow until each job has completed a run" in out and live.get("mode") == "shadow"
    assert cli(fleet, live, "enforce", "--force") == 0
    assert promotions(new_app)[-1]["cant_vouch"] == ["cache-cleanup", "changelog-push"] and promotions(new_app)[-1]["forced"]


def test_log_lists_scheduled_jobs_that_have_not_run(fleet, ops, new_app, capsys):
    live = MemorySettings(new_app, "relnotes")
    cli(fleet, live, "shadow", str(REFINED))
    capsys.readouterr()
    cli(fleet, live, "log")
    assert "cache-cleanup: no completed run since" in capsys.readouterr().out


def test_short_windows_read_in_minutes_and_hours():
    assert (span(300), span(5400), span(86400 * 3)) == ("5 minute(s)", "1.5 hours", "3.0 days")


# --- A promoted policy that can't be checked ------------------------------------------------------


@pytest.fixture
def enforced(fleet, ops):
    live = MemorySettings(ops.home, "demo-ops")
    cli(fleet, live, "shadow", str(REFINED), "--since", "all")
    cli(fleet, live, "enforce", "--since", "all")
    return live


def test_a_tampered_policy_blocks_every_call_and_says_so_everywhere(fleet, enforced, capsys):
    installed = Path(enforced.get("policy"))
    installed.write_text(installed.read_text().replace("action: block", "action: allow"))
    guard = LiveGuard(enforced.get, enforced.home, enforced.profile)
    for command in ("ls", "rm -rf ./cache/*"):
        verdict = guard(tool_name="terminal", args={"command": command}, task_id="cron:aaa111:x")
        assert verdict["action"] == "block" and "changed after it was promoted" in verdict["message"]
    rows = [d for d in decisions(enforced.home()) if d.get("error")]
    assert len(rows) == 2 and all(r["action"] == "block" and r["rules"] == [] for r in rows)
    assert "changed after it was promoted" in rows[0]["error"]
    capsys.readouterr()

    cli(fleet, enforced, "log", "--no-ingest")
    out = capsys.readouterr().out
    assert "2 were stopped" in out
    assert "Warning: the policy couldn't be checked, so the hook blocked 2 call(s)" in out
    assert "nightly-cleanup  blocked  policy couldn't be checked  terminal: rm -rf ./cache/*" in out

    cli(fleet, enforced, "status")
    out = capsys.readouterr().out
    assert "Live on demo-ops: enforce" in out
    assert "every tool call on this profile is being blocked" in out and "changed after it was promoted" in out
    assert "2 call(s) so far" in out


def test_status_is_quiet_when_the_promoted_policy_is_intact(fleet, enforced, capsys):
    capsys.readouterr()
    cli(fleet, enforced, "status")
    assert "can't be checked" not in capsys.readouterr().out


def test_shadow_logs_calls_it_could_not_check(fleet, ops, capsys):
    live = MemorySettings(ops.home, "demo-ops")
    cli(fleet, live, "shadow", str(REFINED), "--since", "all")
    Path(live.get("policy")).unlink()
    assert LiveGuard(live.get, live.home, live.profile)(tool_name="terminal", args={"command": "ls"}) is None
    [row] = decisions(live.home())
    assert row["action"] is None and "can't read policy" in row["error"]
    capsys.readouterr()
    cli(fleet, live, "status")
    assert "shadow is checking nothing" in capsys.readouterr().out
    cli(fleet, live, "log", "--no-ingest")
    assert "let 1 call(s) through unchecked" in capsys.readouterr().out


def test_enforce_log_says_were_stopped(fleet, enforced, capsys):
    LiveGuard(enforced.get, enforced.home, enforced.profile)(tool_name="terminal", args={"command": "cat .env"})
    capsys.readouterr()
    cli(fleet, enforced, "log", "--no-ingest")
    out = capsys.readouterr().out
    assert "1 were stopped" in out and "would be stopped" not in out
