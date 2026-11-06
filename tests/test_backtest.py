"""The story end to end on synthetic history: the catches, the breaks and the policy diff."""

from pathlib import Path

import pytest

from backtester.engine import diff_results, run_backtest
from backtester.index import open_index
from backtester.ingest import ingest
from backtester.policy import load_policy, parse_policy
from fakehermes import T0, stage_story

POLICIES = Path(__file__).resolve().parent.parent / "policies"
NAIVE = load_policy(POLICIES / "naive.yaml")
REFINED = load_policy(POLICIES / "refined.yaml")


@pytest.fixture
def story(fleet):
    profile = fleet.profile("demo")
    sessions = stage_story(profile)
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    yield index, sessions, profile, fleet
    index.close()


def by_rule(result):
    return {r.id: r for r in result.rules}


def test_naive_catches_three_and_breaks_two_jobs(story):
    index, sessions, _, _ = story
    result = run_backtest(index, NAIVE)
    assert {i.session_id for i in result.incidents} == {sessions["incident1"], sessions["incident2"], sessions["incident3"]}
    assert [b.job_name for b in result.would_break] == ["nightly-cleanup", "release-job"]
    assert all((b.runs_hit, b.completed_runs) == (2, 2) for b in result.would_break)
    assert result.calls_replayed == 2 * (2 + 5 + 1) + 2 + 1 + 1 + 2


def test_refined_keeps_the_catches_and_breaks_nothing(story):
    index, sessions, _, _ = story
    result = run_backtest(index, REFINED)
    assert {i.session_id for i in result.incidents} == {sessions["incident1"], sessions["incident2"], sessions["incident3"]}
    assert result.would_break == []


def test_per_rule_counts(story):
    index, *_ = story
    naive = by_rule(run_backtest(index, NAIVE))
    assert (naive["no-rm-rf"].hits, naive["no-rm-rf"].would_block, naive["no-rm-rf"].incidents) == (3, 3, 1)
    assert (naive["no-force-push"].hits, naive["no-force-push"].incidents) == (2, 0)
    assert (naive["no-secret-reads"].hits, naive["no-secret-reads"].incidents) == (2, 1)
    assert (naive["no-shell-uploads"].hits, naive["no-shell-uploads"].incidents) == (2, 2)
    refined = by_rule(run_backtest(index, REFINED))
    assert refined["no-rm-rf"].hits == 1
    assert refined["no-force-push"].hits == 0


def test_diff_naive_to_refined(story):
    index, *_ = story
    diff = diff_results(run_backtest(index, NAIVE), run_backtest(index, REFINED))
    assert diff.newly_caught == []
    assert diff.newly_broken == []
    assert sorted(h.args["command"] for h in diff.no_longer_matched) == [
        "git push --force origin scratch/release", "git push --force origin scratch/release",
        "rm -rf ./cache/*", "rm -rf ./cache/*",
    ]
    assert len(diff.still_caught) == 4
    assert diff.still_broken == []


def test_diff_refined_back_to_naive_shows_the_breaks_returning(story):
    index, *_ = story
    diff = diff_results(run_backtest(index, REFINED), run_backtest(index, NAIVE))
    assert len(diff.newly_broken) == 4
    assert diff.no_longer_matched == []
    assert diff.newly_caught == []


def test_a_new_catch_shows_as_newly_caught(story):
    index, *_ = story
    tighter = parse_policy({"rules": [{"id": "status", "tool": "terminal", "command": "git status", "action": "block", "message": "m"}]})
    diff = diff_results(run_backtest(index, NAIVE), run_backtest(index, tighter))
    assert [h.args["command"] for h in diff.newly_caught] == ["git status"]


def test_approve_in_a_completed_job_counts_as_would_break(story):
    index, *_ = story
    policy = parse_policy({"rules": [{"id": "ask", "tool": "terminal", "command": r"rm\s+-rf", "action": "approve", "message": "m"}]})
    result = run_backtest(index, policy)
    assert [b.job_name for b in result.would_break] == ["nightly-cleanup"]
    assert by_rule(result)["ask"].would_escalate == 3
    assert by_rule(result)["ask"].would_block == 0
    assert len(result.incidents) == 1


def test_a_call_blocked_by_one_rule_isnt_also_escalated_by_another(story):
    index, *_ = story
    policy = parse_policy({"rules": [
        {"id": "ask", "tool": "terminal", "command": r"rm\s+-rf", "action": "approve", "message": "m"},
        {"id": "stop", "tool": "terminal", "command": r"rm\s+-rf", "action": "block", "message": "m"},
    ]})
    stats = by_rule(run_backtest(index, policy))
    assert stats["ask"].hits == 3 and stats["ask"].would_escalate == 0
    assert stats["stop"].would_block == 3


def test_allow_rules_count_hits_but_stop_nothing(story):
    index, *_ = story
    policy = parse_policy({"rules": [{"id": "note", "tool": "terminal", "command": r"rm\s+-rf", "action": "allow"}]})
    result = run_backtest(index, policy)
    assert by_rule(result)["note"].hits == 3
    assert result.incidents == [] and result.would_break == []


def test_a_block_in_a_failed_run_is_an_incident_not_a_break(fleet):
    profile = fleet.profile("demo")
    failed = profile.cron_run("job1", "flaky", ["rm -rf /tmp/x"], status="failed")
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    result = run_backtest(index, NAIVE)
    index.close()
    assert result.would_break == []
    assert [i.session_id for i in result.incidents] == [failed]


def test_denominator_counts_every_completed_run(fleet):
    profile = fleet.profile("demo")
    profile.cron_run("job1", "sometimes", ["rm -rf ./x"])
    profile.cron_run("job1", "sometimes", ["echo fine"])
    profile.cron_run("job1", "sometimes", ["echo fine"])
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    job = run_backtest(index, NAIVE).would_break[0]
    index.close()
    assert (job.runs_hit, job.completed_runs) == (1, 3)


def test_subagent_of_a_job_breaks_that_job_and_counts_once(fleet):
    profile = fleet.profile("demo")
    parent = profile.cron_run("job1", "delegating", ["echo start"])
    child = profile.session(source="subagent", parent=parent, end_reason="agent_close")
    profile.call(child, "terminal", {"command": "rm -rf /tmp/x"})
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    index = open_index(fleet.index)
    result = run_backtest(index, NAIVE)
    index.close()
    assert result.incidents == []
    assert [(b.job_name, b.runs_hit, b.completed_runs) for b in result.would_break] == [("delegating", 1, 1)]


def test_time_window_and_profile_filters(story):
    index, sessions, profile, fleet = story
    other = fleet.profile("other")
    other.call(other.session(), "terminal", {"command": "rm -rf /elsewhere"})
    ingest(fleet.root, fleet.index)
    assert len(run_backtest(index, NAIVE).incidents) == 4
    assert len(run_backtest(index, NAIVE, profiles=["other"]).incidents) == 1
    assert run_backtest(index, NAIVE, profiles=["other"]).would_break == []
    assert run_backtest(index, NAIVE, since=profile.clock + 1000).calls_replayed == 0
    assert run_backtest(index, NAIVE, until=T0).calls_replayed == 0


def test_replaying_large_history_takes_seconds(fleet):
    import time

    index = open_index(fleet.index)
    index.execute("INSERT INTO sessions (profile, session_id) VALUES ('p', 's')")
    index.executemany(
        "INSERT INTO calls (profile, session_id, call_uid, ts, tool, args) VALUES ('p', 's', ?, ?, 'terminal', ?)",
        ((str(i), float(i), '{"command": "cd /workspace && git status --short && ls -la src/ | head -20"}') for i in range(100_000)),
    )
    index.commit()
    started = time.perf_counter()
    result = run_backtest(index, NAIVE)
    elapsed = time.perf_counter() - started
    index.close()
    assert result.calls_replayed == 100_000
    assert elapsed < 15
