"""Ingest against fixture databases: read-only, deduped, linked, incremental, no result bodies."""

import hashlib
import json
import sqlite3
import stat

import pytest

from backtester.index import open_index
from backtester.ingest import ingest
from backtester.sourcedb import open_readonly
from fakehermes import OK

SECRET = "sk-THIS-IS-A-RESULT-BODY-SECRET-0123456789"


def rows(fleet, query, *params):
    index = open_index(fleet.index)
    try:
        return [dict(r) for r in index.execute(query, params)]
    finally:
        index.close()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_readonly_connection_refuses_writes(fleet):
    profile = fleet.profile("default")
    sid = profile.session()
    conn = open_readonly(profile.db_path)
    for statement in (
        "INSERT INTO sessions (id, source, started_at) VALUES ('x', 'y', 0)",
        f"UPDATE sessions SET title='z' WHERE id='{sid}'",
        "DROP TABLE sessions",
        "CREATE TABLE t (a)",
    ):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(statement)
    conn.close()


def test_ingest_leaves_state_db_untouched_and_sees_the_wal(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    profile.call(sid, "terminal", {"command": "echo hi"})
    # Held open with rows still in the WAL, as a live agent would.
    wal = profile.db_path.with_name("state.db-wal")
    assert wal.stat().st_size > 0
    before = {p.name: sha(p) for p in (profile.db_path, wal)}
    ingest(fleet.root, fleet.index)
    assert {p.name: sha(p) for p in (profile.db_path, wal)} == before
    assert rows(fleet, "SELECT tool FROM calls") == [{"tool": "terminal"}]
    profile.call(sid, "terminal", {"command": "still writable by Hermes"})


def test_every_profile_is_found_and_tombstones_skipped(fleet):
    fleet.profile("default").session()
    fleet.profile("one").session()
    fleet.profile("gone").session()
    (fleet.root / "profiles" / ".deleted" / "gone").mkdir(parents=True)
    (fleet.root / "profiles" / "empty").mkdir()
    summary = ingest(fleet.root, fleet.index)
    assert sorted(p.profile for p in summary.profiles) == ["default", "one"]
    assert [p.profile for p in ingest(fleet.root, fleet.index, ["one"]).profiles] == ["one"]


def test_result_bodies_and_file_bodies_stay_out_of_the_index(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    profile.call(sid, "read_file", {"path": "config/vendor-api.yaml"}, result=json.dumps({"content": f"key: {SECRET}"}))
    profile.call(sid, "write_file", {"path": "out.txt", "content": f"copied {SECRET}"})
    profile.call(sid, "patch", {"path": "a.txt", "old_string": SECRET, "new_string": SECRET})
    ingest(fleet.root, fleet.index)
    raw = b"".join(p.read_bytes() for p in fleet.index.parent.iterdir())
    assert SECRET.encode() not in raw
    calls = rows(fleet, "SELECT tool, args FROM calls ORDER BY ts")
    assert json.loads(calls[1]["args"])["content"].startswith("<omitted")
    assert json.loads(calls[0]["args"])["path"] == "config/vendor-api.yaml"


def test_index_is_private(fleet):
    fleet.profile("demo").session()
    ingest(fleet.root, fleet.index)
    assert stat.S_IMODE(fleet.index.stat().st_mode) == 0o600
    assert stat.S_IMODE(fleet.index.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "tool,result,expected",
    [
        ("terminal", OK, "ok"),
        ("terminal", json.dumps({"output": "", "exit_code": 1, "error": None}), "error"),
        ("terminal", json.dumps({"output": "", "exit_code": 127, "error": None}), "error"),
        ("read_file", json.dumps({"error": "Access denied: /x/.env is a secret-bearing environment file"}), "denied"),
        ("terminal", json.dumps({"output": "", "exit_code": -1, "error": "BLOCKED: User denied this command."}), "denied"),
        ("terminal", json.dumps({"output": "", "exit_code": -1, "user_summary": "Denied by you"}), "denied"),
        ("read_file", json.dumps({"error": "File not found"}), "error"),
        ("skill_view", json.dumps({"success": False}), "error"),
        ("web_search", '<untrusted_tool_result source="web_search">text', "ok"),
        ("read_file", json.dumps({"content": "ok", "not_found": False}), "ok"),
        ("terminal", None, None),
    ],
)
def test_result_status(fleet, tool, result, expected):
    profile = fleet.profile("demo")
    profile.call(profile.session(), tool, {"command": "x"}, result=result)
    ingest(fleet.root, fleet.index)
    assert rows(fleet, "SELECT status FROM calls") == [{"status": expected}]


def test_compaction_copies_count_once_with_the_earliest_time_and_status(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    denied = json.dumps({"error": "Access denied: /x/.env"})
    profile.call(sid, "read_file", {"path": ".env"}, result=denied, call_id="c1", uid="u1", ts=1000.0)
    # Hermes re-inserts the same turn after compacting, with a trimmed result.
    profile.call(sid, "read_file", {"path": ".env"}, result="[trimmed]", call_id="c1", uid="u1", ts=2000.0)
    ingest(fleet.root, fleet.index)
    assert rows(fleet, "SELECT ts, status FROM calls") == [{"ts": 1000.0, "status": "denied"}]


def test_providers_that_reuse_call_ids_stay_separate(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    profile.call(sid, "terminal", {"command": "a"}, call_id="call_0", uid="u1")
    profile.call(sid, "terminal", {"command": "b"}, result=json.dumps({"exit_code": 1}), call_id="call_0", uid="u2")
    ingest(fleet.root, fleet.index)
    found = {json.loads(r["args"])["command"]: r["status"] for r in rows(fleet, "SELECT args, status FROM calls")}
    assert found == {"a": "ok", "b": "error"}


def test_cron_link_comes_from_admissions_and_executions(fleet):
    profile = fleet.profile("demo")
    good = profile.cron_run("job1", "nightly-cleanup", ["echo ok"])
    bad = profile.cron_run("job1", "nightly-cleanup", ["echo ok"], status="failed")
    short = profile.cron_run("job1", "nightly-cleanup", ["echo ok"], end_reason="cron_incomplete_no_output")
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    found = {r["session_id"]: r for r in rows(fleet, "SELECT * FROM sessions")}
    assert found[good]["job_kind"] == "cron"
    assert found[good]["job_id"] == "job1"
    assert found[good]["job_name"] == "nightly-cleanup"
    assert found[good]["run_outcome"] == "completed"
    assert found[bad]["run_outcome"] == "failed"
    assert found[short]["run_outcome"] == "incomplete"


def test_cron_link_without_cron_files_uses_the_admission(fleet):
    profile = fleet.profile("demo")
    sid = profile.cron_run("job1", "nightly-cleanup", ["echo ok"])
    ingest(fleet.root, fleet.index)
    found = rows(fleet, "SELECT job_name, run_outcome FROM sessions WHERE session_id=?", sid)
    assert found == [{"job_name": "nightly-cleanup", "run_outcome": "completed"}]


def test_older_cron_sessions_fall_back_to_the_session_id(fleet):
    profile = fleet.profile("default")
    done = profile.session(sid="cron_abc123_20260901_100000", source="cron", title="digest · Sep 01", end_reason="cron_complete")
    open_ = profile.session(sid="cron_abc123_20260902_100000", source="cron", title="digest · Sep 02", end_reason=None)
    ingest(fleet.root, fleet.index)
    found = {r["session_id"]: r for r in rows(fleet, "SELECT * FROM sessions")}
    assert found[done]["job_id"] == "abc123" and found[done]["job_name"] == "digest"
    assert found[done]["run_outcome"] == "completed"
    assert found[open_]["run_outcome"] == "unknown"


def test_kanban_link_reads_the_board(fleet):
    profile = fleet.profile("demo")
    sid = profile.session(source="kanban")
    request = 'kanban:["ops","t_1",7]'
    profile.db.execute(
        "INSERT INTO session_admissions (admission_id, request_id, target_session_id, status, outcome) VALUES ('a',?,?,'terminal','interrupted')",
        (request, sid),
    )
    board = fleet.root / "kanban" / "boards" / "ops"
    board.mkdir(parents=True)
    db = sqlite3.connect(board / "kanban.db")
    db.executescript(
        "CREATE TABLE tasks (id TEXT, title TEXT); INSERT INTO tasks VALUES ('t_1', 'Tidy reports');"
        "CREATE TABLE task_runs (id INTEGER, outcome TEXT); INSERT INTO task_runs VALUES (7, 'completed');"
    )
    db.close()
    ingest(fleet.root, fleet.index)
    assert rows(fleet, "SELECT job_kind, job_id, job_name, run_id, run_outcome FROM sessions") == [
        {"job_kind": "kanban", "job_id": "ops/t_1", "job_name": "Tidy reports", "run_id": "7", "run_outcome": "completed"}
    ]


def test_subagents_inherit_the_run_that_spawned_them(fleet):
    profile = fleet.profile("demo")
    parent = profile.cron_run("job1", "nightly-cleanup", ["echo ok"])
    child = profile.session(source="subagent", parent=parent, end_reason="agent_close")
    grandchild = profile.session(source="subagent", parent=child, end_reason="agent_close")
    profile.write_cron_files()
    ingest(fleet.root, fleet.index)
    found = {r["session_id"]: r for r in rows(fleet, "SELECT * FROM sessions")}
    assert found[grandchild]["job_id"] == "job1"
    assert found[grandchild]["run_id"] == found[parent]["run_id"]
    assert found[grandchild]["run_outcome"] == "completed"


def test_ingest_is_incremental_per_session(fleet):
    profile = fleet.profile("demo")
    first = profile.session()
    second = profile.session()
    profile.call(first, "terminal", {"command": "one"})
    profile.call(second, "terminal", {"command": "two"})
    run = ingest(fleet.root, fleet.index).profiles[0]
    assert (run.sessions_read, run.new_calls, run.calls) == (2, 2, 2)
    run = ingest(fleet.root, fleet.index).profiles[0]
    assert (run.sessions_read, run.new_calls, run.calls) == (0, 0, 2)
    profile.call(second, "terminal", {"command": "three"})
    run = ingest(fleet.root, fleet.index).profiles[0]
    assert (run.sessions_read, run.new_calls, run.calls) == (1, 1, 3)


def test_a_result_arriving_later_updates_the_status(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    profile.call(sid, "terminal", {"command": "slow"}, result=None, call_id="c1")
    ingest(fleet.root, fleet.index)
    assert rows(fleet, "SELECT status FROM calls") == [{"status": None}]
    profile.db.execute(
        "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp) VALUES (?, 'tool', ?, 'c1', 'terminal', 5)",
        (sid, json.dumps({"exit_code": 2})),
    )
    profile.bump(sid)
    ingest(fleet.root, fleet.index)
    assert rows(fleet, "SELECT status FROM calls") == [{"status": "error"}]


def test_history_outlives_hermes_pruning(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    profile.call(sid, "terminal", {"command": "old"})
    ingest(fleet.root, fleet.index)
    profile.db.execute("DELETE FROM messages")
    profile.db.execute("DELETE FROM sessions")
    ingest(fleet.root, fleet.index)
    assert len(rows(fleet, "SELECT * FROM calls")) == 1
    assert len(rows(fleet, "SELECT * FROM sessions")) == 1


def test_older_schemas_still_ingest(fleet):
    profile = fleet.profile("old", legacy=True)
    sid = profile.session(source="oneshot")
    profile.call(sid, "terminal", {"command": "ls"})
    summary = ingest(fleet.root, fleet.index)
    assert summary.profiles[0].error is None and summary.profiles[0].calls == 1


def test_malformed_tool_calls_are_skipped_not_fatal(fleet):
    profile = fleet.profile("demo")
    sid = profile.session()
    for payload in ("not json", '{"a": 1}', '[1, 2]', '[{"id": "x", "function": {}}]'):
        profile.db.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp, message_uid) VALUES (?, 'assistant', ?, 1, ?)",
            (sid, payload, payload),
        )
    profile.db.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp, message_uid) VALUES (?, 'assistant', ?, 2, 'u')",
        (sid, json.dumps([{"id": "y", "function": {"name": "terminal", "arguments": "{broken"}}])),
    )
    profile.bump(sid)
    summary = ingest(fleet.root, fleet.index)
    assert summary.profiles[0].error is None
    assert [json.loads(r["args"]) for r in rows(fleet, "SELECT args FROM calls")] == [{"_unparsed": "{broken"}]


def test_a_broken_profile_doesnt_stop_the_others(fleet):
    fleet.profile("good").session()
    broken = fleet.root / "profiles" / "broken"
    broken.mkdir(parents=True)
    (broken / "state.db").write_text("this is not a database")
    summary = {p.profile: p for p in ingest(fleet.root, fleet.index).profiles}
    assert summary["broken"].error
    assert summary["good"].error is None and summary["good"].sessions == 1
