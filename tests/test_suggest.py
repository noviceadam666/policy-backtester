"""``suggest``: exemptions pinned to the exact command each job ran, and nothing it can't pin."""

import argparse
import json
import re
from pathlib import Path

import pytest

from backtester.cli import BREAKS_EXIT, dispatch, setup_parser
from backtester.engine import diff_results, run_backtest
from backtester.evaluator import evaluate
from backtester.index import open_index
from backtester.ingest import ingest
from backtester.policy import load_policy
from backtester.report import render_suggestion
from backtester.suggest import exact_pattern, load_proposal, proposed_yaml, suggest
from fakehermes import stage_story
from test_evaluator import GOLDEN

POLICIES = Path(__file__).resolve().parent.parent / "policies"
NAIVE = load_policy(POLICIES / "naive.yaml")
REFINED = load_policy(POLICIES / "refined.yaml")


def propose(index, policy, source_text):
    before = run_backtest(index, policy)
    suggestion = suggest(before)
    text = proposed_yaml(source_text, suggestion, ["test"]) if suggestion.patterns else None
    after = run_backtest(index, load_proposal(text)) if text else before
    return suggestion, text, before, after


@pytest.fixture
def story(fleet):
    stage_story(fleet.profile("demo"))
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    yield index
    index.close()


def indexed(fleet):
    ingest(fleet.root, fleet.index)
    return open_index(fleet.index)


def test_suggest_on_naive_lands_on_refined(story):
    suggestion, text, before, after = propose(story, NAIVE, (POLICIES / "naive.yaml").read_text())
    proposal = load_proposal(text)
    assert suggestion.for_review == []
    assert [(e.rule_id, e.job.job_name, e.segments) for e in suggestion.exemptions] == [
        ("no-rm-rf", "nightly-cleanup", ("rm -rf ./cache/*",)),
        ("no-force-push", "release-job", ("git push --force origin scratch/release",)),
    ]
    rules = {r.id: r for r in proposal.rules}
    assert rules["no-rm-rf"].unless_command.pattern == {r.id: r for r in REFINED.rules}["no-rm-rf"].unless_command.pattern
    assert after.would_break == []
    refined = run_backtest(story, REFINED)
    diff = diff_results(refined, after)
    assert (diff.newly_caught, diff.newly_broken, diff.no_longer_matched) == ([], [], [])
    assert [i.session_id for i in after.incidents] == [i.session_id for i in refined.incidents]


def test_the_proposal_changes_only_unless_command_lines(story):
    _, text, *_ = propose(story, NAIVE, (POLICIES / "naive.yaml").read_text())
    naive = (POLICIES / "naive.yaml").read_text().splitlines()
    added = [line for line in text.splitlines() if line not in naive and not line.startswith("#")]
    removed = [line for line in naive if line not in text.splitlines() and not line.startswith("#")]
    assert len(added) == 2 and all(line.strip().startswith("unless_command:") for line in added)
    assert removed == []


@pytest.mark.parametrize("call,naive_ids,refined_ids", GOLDEN)
def test_the_proposal_is_never_looser_than_refined(story, call, naive_ids, refined_ids):
    _, text, *_ = propose(story, NAIVE, (POLICIES / "naive.yaml").read_text())
    tool, args = call
    assert set(refined_ids) <= set(evaluate(load_proposal(text), tool, args).rule_ids)


def test_exact_pattern_matches_the_whole_segment_only():
    pattern = re.compile(exact_pattern(["rm -rf ./cache/*"]))
    assert pattern.search("rm -rf ./cache/*") and pattern.search("  rm  -rf\t./cache/*  ")
    for other in ("rm -rf ./cache/x", "rm -rf ./cache/* /etc", "sudo rm -rf ./cache/*", "rm -rf ./cache/"):
        assert not pattern.search(other)
    both = re.compile(exact_pattern(["b (x)", "a.b"]))
    assert both.search("a.b") and both.search("b (x)") and not both.search("axb")


def test_a_command_that_varies_between_runs_is_left_for_a_human(fleet):
    profile = fleet.profile("demo")
    profile.cron_run("j1", "rotate", ["rm -rf ./build-1"])
    profile.cron_run("j1", "rotate", ["rm -rf ./build-2"])
    profile.write_cron_files()
    index = indexed(fleet)
    suggestion, text, *_ = propose(index, NAIVE, (POLICIES / "naive.yaml").read_text())
    index.close()
    assert text is None and suggestion.exemptions == []
    assert [(r.rule_id, r.job.job_name) for r in suggestion.for_review] == [("no-rm-rf", "rotate")]
    assert "2 variants" in suggestion.for_review[0].reason


def test_a_non_terminal_call_is_left_for_a_human(fleet):
    profile = fleet.profile("demo")
    sid = profile.cron_run("j1", "sync", ["echo start"])
    profile.call(sid, "read_file", {"path": "/workspace/app/.env"})
    profile.write_cron_files()
    index = indexed(fleet)
    suggestion, text, *_ = propose(index, NAIVE, (POLICIES / "naive.yaml").read_text())
    index.close()
    assert text is None
    assert [(r.rule_id, r.reason) for r in suggestion.for_review] == [
        ("no-secret-reads", "it stops a read_file call, which has no command to exempt"),
    ]


def test_a_job_that_ran_it_in_some_runs_is_exempted_and_says_how_often(fleet):
    profile = fleet.profile("demo")
    profile.cron_run("j1", "sometimes", ["rm -rf ./tmp/old"])
    profile.cron_run("j1", "sometimes", ["echo fine"])
    profile.cron_run("j1", "sometimes", ["echo fine"])
    profile.write_cron_files()
    index = indexed(fleet)
    suggestion, _, _, after = propose(index, NAIVE, (POLICIES / "naive.yaml").read_text())
    index.close()
    assert [(e.runs_hit, e.job.completed_runs) for e in suggestion.exemptions] == [(1, 3)]
    assert after.would_break == []


def test_several_commands_in_every_run_become_one_alternation(fleet):
    profile = fleet.profile("demo")
    for _ in range(2):
        profile.cron_run("j1", "clean", ["rm -rf ./cache/* && rm -rf ./tmp/*"])
    profile.write_cron_files()
    index = indexed(fleet)
    suggestion, text, _, after = propose(index, NAIVE, (POLICIES / "naive.yaml").read_text())
    index.close()
    assert suggestion.exemptions[0].segments == ("rm -rf ./cache/*", "rm -rf ./tmp/*")
    assert after.would_break == []
    proposal = load_proposal(text)
    assert evaluate(proposal, "terminal", {"command": "rm -rf ./tmp/* && rm -rf ./cache/*"}).action is None
    assert evaluate(proposal, "terminal", {"command": "rm -rf ./tmp/* /etc"}).action == "block"


def test_an_existing_exemption_is_kept(fleet):
    profile = fleet.profile("demo")
    for _ in range(2):
        profile.cron_run("j1", "clean", ["rm -rf ./cache/*", "rm -rf ./dist"])
    profile.write_cron_files()
    source = "rules:\n  - id: rm\n    tool: terminal\n    command: 'rm\\s+-rf'\n    unless_command: '^\\s*rm\\s+-rf\\s+\\./cache/\\*\\s*$'\n    action: block\n    message: m\n"
    index = indexed(fleet)
    suggestion, text, _, after = propose(index, load_proposal(source), source)
    index.close()
    assert suggestion.exemptions[0].segments == ("rm -rf ./dist",)
    assert text.count("unless_command") == 1
    proposal = load_proposal(text)
    for command in ("rm -rf ./cache/*", "rm -rf ./dist"):
        assert evaluate(proposal, "terminal", {"command": command}).action is None
    assert after.would_break == []


def test_an_unresolved_target_is_exempted_like_a_match(fleet):
    profile = fleet.profile("demo")
    for _ in range(2):
        profile.cron_run("j1", "report", ['cat "$REPORT_DIR/summary.txt"'])
    profile.write_cron_files()
    source = "rules:\n  - id: s\n    tool: terminal\n    command: '(^|\\s)cat\\s'\n    path: ['**/.env']\n    action: block\n    message: m\n    if_unresolved: approve\n"
    index = indexed(fleet)
    suggestion, text, before, after = propose(index, load_proposal(source), source)
    index.close()
    assert len(before.would_break) == 1
    assert suggestion.exemptions[0].segments == ('cat "$REPORT_DIR/summary.txt"',)
    assert after.would_break == []
    assert evaluate(load_proposal(text), "terminal", {"command": 'cat "$OTHER/x"'}).action == "approve"


def test_it_warns_when_an_exemption_also_lets_a_session_through(fleet):
    profile = fleet.profile("demo")
    for _ in range(2):
        profile.cron_run("j1", "clean", ["rm -rf ./cache/*"])
    profile.call(profile.session(), "terminal", {"command": "rm -rf ./cache/*"})
    profile.write_cron_files()
    index = indexed(fleet)
    suggestion, _, before, after = propose(index, NAIVE, (POLICIES / "naive.yaml").read_text())
    index.close()
    assert len(before.incidents) == 1 and after.incidents == []
    assert "also stop catching 1 call(s)" in render_suggestion(suggestion, diff_results(before, after))


def run_cli(fleet, *argv):
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    return dispatch(parser.parse_args(["--hermes-root", str(fleet.root), *argv]))


def test_cli_prints_the_proposal_and_never_touches_the_policy(fleet, capsys):
    stage_story(fleet.profile("demo"))
    before = (POLICIES / "naive.yaml").read_text()
    assert run_cli(fleet, "suggest", str(POLICIES / "naive.yaml")) == 0
    out = capsys.readouterr().out
    assert "jobs broken      2       0" in out
    assert "Left for a human: none" in out
    assert "unless_command: '^\\s*rm\\s+-rf\\s+\\./cache/\\*\\s*$'" in out
    assert (POLICIES / "naive.yaml").read_text() == before


def test_cli_out_writes_a_new_file_only(fleet, tmp_path, capsys):
    stage_story(fleet.profile("demo"))
    out_file = tmp_path / "proposed.yaml"
    assert run_cli(fleet, "suggest", str(POLICIES / "naive.yaml"), "--out", str(out_file)) == 0
    assert "Nothing was applied" in capsys.readouterr().out
    assert run_backtest_file(fleet, out_file).would_break == []
    assert run_cli(fleet, "suggest", str(POLICIES / "naive.yaml"), "--out", str(out_file)) == 1
    assert "never overwrites" in capsys.readouterr().err


def run_backtest_file(fleet, path):
    index = open_index(fleet.index)
    try:
        return run_backtest(index, load_policy(path))
    finally:
        index.close()


def test_cli_json_and_fail_on_break(fleet, capsys):
    profile = fleet.profile("demo")
    profile.cron_run("j1", "rotate", ["rm -rf ./build-1"])
    profile.cron_run("j1", "rotate", ["rm -rf ./build-2"])
    profile.write_cron_files()
    assert run_cli(fleet, "suggest", str(POLICIES / "naive.yaml"), "--fail-on-break") == BREAKS_EXIT
    assert "No policy proposed" in capsys.readouterr().out
    assert run_cli(fleet, "suggest", str(POLICIES / "naive.yaml"), "--format", "json", "--no-ingest") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy_yaml"] is None and report["exemptions"] == []
    assert report["for_review"][0]["rule"] == "no-rm-rf"


def test_cli_says_when_there_is_nothing_to_suggest(fleet, capsys):
    stage_story(fleet.profile("demo"))
    assert run_cli(fleet, "suggest", str(POLICIES / "refined.yaml")) == 0
    assert "Nothing to suggest: refined.yaml breaks no jobs" in capsys.readouterr().out
