"""The built-in guard rules, the current-profile default, try, and the two-sided cases summary."""

import argparse
import json
from pathlib import Path

import pytest

from backtester.check import load_cases, render_case_summary, summarize_cases
from backtester.cli import dispatch, setup_parser
from backtester.engine import run_backtest
from backtester.evaluator import evaluate
from backtester.guard import with_guard
from backtester.index import open_index
from backtester.ingest import ingest, scheduled_jobs
from backtester.live import LiveGuard, MemorySettings, decisions, promotions
from backtester.policy import PolicyError, load_policy, parse_policy
from backtester.suggest import suggest
from fakehermes import stage_story

REPO = Path(__file__).resolve().parent.parent
REFINED, NAIVE = REPO / "policies" / "refined.yaml", REPO / "policies" / "naive.yaml"
CASES = REPO / "fixtures" / "cases" / "refined.yaml"
ROOT = Path.home() / ".hermes"
GUARDED = with_guard(load_policy(REFINED), ROOT)


def decide(command, workdir=None, tool="terminal"):
    args = {"command": command} if tool == "terminal" else {"path": command}
    if workdir:
        args["workdir"] = workdir
    return evaluate(GUARDED, tool, args, "ops")


@pytest.mark.parametrize("command", [
    "hermes -p ops backtest off",
    "hermes backtest shadow mine.yaml",
    "hermes backtest --hermes-root /x enforce --force",
    "cd /tmp && hermes -p ops backtest off",
    "hermes plugins disable policy-backtester",
    "hermes -p ops plugins uninstall policy-backtester",
    "hermes config set plugins.entries.policy-backtester.settings.mode off",
])
def test_the_agent_cant_run_the_guards_own_switches(command):
    verdict = decide(command)
    assert verdict.action == "block" and verdict.rule_ids == ("guard-commands",)


@pytest.mark.parametrize("command", [
    "hermes -p ops backtest log",
    "hermes backtest status",
    "hermes backtest run shadow.yaml --since 7d",
    "hermes backtest log; echo off",
    "hermes plugins list",
    "hermes config set model.default some-model",
])
def test_reading_the_guard_is_fine(command):
    assert decide(command).action is None


@pytest.mark.parametrize("tool,target,workdir", [
    ("terminal", "sed -i s/enforce/off/ ~/.hermes/profiles/ops/config.yaml", None),
    ("terminal", "sed -i s/enforce/off/ $HOME/.hermes/config.yaml", None),
    ("terminal", f"truncate -s0 {ROOT}/profiles/ops/backtest/decisions.jsonl", None),
    ("terminal", "sed -i s/enforce/off/ config.yaml", f"{ROOT}/profiles/ops"),
    ("write_file", f"{ROOT}/profiles/ops/config.yaml", None),
    ("patch", "~/.hermes/profiles/ops/backtest/policies/946d0ca6e5f5.yaml", None),
])
def test_the_agent_cant_touch_the_guards_files(tool, target, workdir):
    verdict = decide(target, workdir, tool)
    assert verdict.action == "block" and "guard-files" in verdict.rule_ids


def test_other_config_files_are_fine():
    assert decide("sed -i s/a/b/ config.yaml", "/workspace/app").action is None
    assert decide("/workspace/app/config.yaml", tool="write_file").action is None
    assert decide("~/.hermes/profiles/ops/notes.md", tool="write_file").action is None


def test_guard_rules_keep_the_policys_hash_and_reserve_their_ids():
    assert GUARDED.hash == load_policy(REFINED).hash
    clash = parse_policy({"rules": [{"id": "guard-files", "tool": "terminal", "action": "allow"}]})
    with pytest.raises(PolicyError, match="reserved"):
        with_guard(clash, ROOT)


@pytest.fixture
def ops(fleet):
    profile = fleet.profile("demo-ops")
    stage_story(profile)
    return profile


@pytest.fixture
def live(ops):
    return MemorySettings(ops.home, "demo-ops")


def cli(fleet, live, *argv):
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    return dispatch(parser.parse_args(["--hermes-root", str(fleet.root), *argv]), live)


def test_the_live_hook_guards_itself_in_both_modes(fleet, ops, live, capsys):
    guard = LiveGuard(live.get, live.home, live.profile)
    switch_off = {"command": "hermes -p demo-ops backtest off"}
    assert guard(tool_name="terminal", args=switch_off) is None and decisions(ops.home) == []
    assert cli(fleet, live, "shadow", str(REFINED), "--since", "all") == 0
    assert guard(tool_name="terminal", args=switch_off) is None
    assert decisions(ops.home)[-1]["rules"] == ["guard-commands"]
    assert cli(fleet, live, "enforce", "--since", "all") == 0
    blocked = guard(tool_name="terminal", args={"command": f"sed -i s/enforce/off/ {ops.home}/config.yaml"})
    assert blocked["action"] == "block" and "guard-files" in blocked["message"]
    assert live.get("mode") == "enforce"


def test_a_job_that_switches_the_guard_breaks_in_the_backtest_and_suggest_leaves_it(fleet):
    profile = fleet.profile("demo")
    for _ in range(2):
        profile.cron_run("j1", "self-tuner", ["hermes -p demo backtest shadow tuned.yaml"])
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    try:
        result = run_backtest(index, with_guard(load_policy(REFINED), fleet.root), scheduled=scheduled_jobs(fleet.root))
    finally:
        index.close()
    assert [(j.job_name, j.rule_ids) for j in result.would_break] == [("self-tuner", ["guard-commands"])]
    suggestion = suggest(result)
    assert not suggestion.patterns
    assert "built-in guard rule" in suggestion.for_review[0].reason


def test_history_commands_read_the_profile_youre_in(fleet, ops, live, capsys):
    stage_story(fleet.profile("other"))
    assert cli(fleet, live, "run", str(NAIVE), "--since", "all") == 0
    out = capsys.readouterr().out
    assert "Replayed 22 calls" in out and "Scope    demo-ops only, the profile you're in" in out
    assert cli(fleet, live, "run", str(NAIVE), "--since", "all", "--all-profiles") == 0
    out = capsys.readouterr().out
    assert "Replayed 44 calls" in out and "Scope" not in out
    assert cli(fleet, live, "run", str(NAIVE), "--all-profiles", "--profile", "other") == 1
    assert "don't go together" in capsys.readouterr().err


def test_try_reports_both_sides_and_starts_shadow(fleet, ops, live, capsys):
    assert cli(fleet, live, "try", "refined", "--since", "all") == 0
    out = capsys.readouterr().out
    assert "Trying refined.yaml (sha256 946d0ca6e5f5) on demo-ops, all history" in out
    assert "Would break   0 job(s). Ran untouched: nightly-cleanup, release-job, ops-check" in out
    assert "Would catch   3 session(s)" in out
    assert ("Would miss    11 of 27 known calls it should stop, and lets 4 of 4 routine ones through"
            " (refined.yaml: each checked, none run)") in out
    assert "Shadow is on for demo-ops" in out and "backtest log" in out
    assert live.get("mode") == "shadow" and live.get("policy_sha256") == load_policy(REFINED).hash
    assert promotions(ops.home)[-1]["via"] == "try"

    assert cli(fleet, live, "try", "refined", "--since", "all") == 0
    assert "Already in shadow on demo-ops" in capsys.readouterr().out
    assert len(promotions(ops.home)) == 1


def test_try_says_what_breaks_and_never_replaces_enforce(fleet, ops, live, capsys):
    assert cli(fleet, live, "try", "naive", "--since", "all", "--no-shadow") == 0
    out = capsys.readouterr().out
    assert "Would break   2 job(s). Ran untouched: ops-check" in out
    assert "Would miss    not checked" in out and "backtest suggest naive --out fixed.yaml" in out
    assert "Nothing started (--no-shadow)" in out and live.get("mode") is None

    cli(fleet, live, "shadow", str(REFINED), "--since", "all")
    cli(fleet, live, "enforce", "--since", "all")
    capsys.readouterr()
    assert cli(fleet, live, "try", "naive", "--since", "all") == 0
    assert "Enforce is already on for demo-ops" in capsys.readouterr().out
    assert live.get("mode") == "enforce" and live.get("policy_sha256") == load_policy(REFINED).hash


def test_log_says_whether_shadow_is_ready_to_enforce(fleet, ops, live, capsys):
    cli(fleet, live, "try", "refined", "--since", "all")
    capsys.readouterr()
    assert cli(fleet, live, "log", "--since", "all", "--no-ingest") == 0
    assert "Ready: every scheduled job ran untouched in shadow." in capsys.readouterr().out
    LiveGuard(live.get, live.home, live.profile)(tool_name="terminal", args={"command": "rm -rf /srv"}, task_id="cron:aaa111:x")
    cli(fleet, live, "log", "--since", "all", "--no-ingest")
    assert "Not ready: shadow would have stopped nightly-cleanup." in capsys.readouterr().out


def test_run_checks_known_cases_on_both_sides(fleet, ops, live, capsys):
    assert cli(fleet, live, "run", str(REFINED), "--since", "all") == 0
    out = capsys.readouterr().out
    assert "Known cases: refined.yaml, 31 calls checked one by one (none is run)" in out
    assert "Stops 16 of 27 calls it should stop. Misses 11:" in out
    assert cli(fleet, live, "run", str(NAIVE), "--since", "all", "--cases", str(CASES), "--format", "json") == 0
    report = json.loads(capsys.readouterr().out)["known_cases"]
    assert (report["routine"], report["let_through"]) == (4, 2)
    assert {c["name"] for c in report["over_stopped"]} == {"the nightly cleanup", "the release job's force-push"}
    assert cli(fleet, live, "run", str(NAIVE), "--since", "all") == 0
    assert "Known cases" not in capsys.readouterr().out


def test_the_case_summary_counts_both_sides():
    summary = summarize_cases(GUARDED, CASES, load_cases(CASES))
    assert (len(summary.should_stop), len(summary.missed), len(summary.routine), len(summary.over_stopped)) == (27, 11, 4, 0)
    assert render_case_summary(summary)[1] == "  Stops 16 of 27 calls it should stop. Misses 11:"


def test_status_says_when_the_settings_changed_outside_backtest(fleet, ops, live, capsys):
    cli(fleet, live, "shadow", str(REFINED), "--since", "all")
    cli(fleet, live, "enforce", "--since", "all")
    capsys.readouterr()
    assert cli(fleet, live, "status") == 0
    assert "changed outside" not in capsys.readouterr().out
    live.set("mode", "off")
    cli(fleet, live, "status")
    out = capsys.readouterr().out
    assert "Live on demo-ops: off" in out
    assert "set enforce with sha256 946d0ca6e5f5: the settings were changed outside hermes backtest" in out
    cli(fleet, live, "off")
    capsys.readouterr()
    cli(fleet, live, "status")
    assert "changed outside" not in capsys.readouterr().out


def test_the_examples_are_checked_against_refineds_cases(fleet, ops, live, capsys):
    assert cli(fleet, live, "try", "hardened", "--since", "all", "--no-shadow") == 0
    out = capsys.readouterr().out
    assert "Would miss    0 of 27 known calls it should stop, and lets 4 of 4 routine ones through" in out
