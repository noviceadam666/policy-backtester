"""Builds Hermes-shaped history in a folder you choose. Everything here is synthetic.

The tests and ``hermes backtest demo`` use it; it never touches a real Hermes home.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (32);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, origin_json TEXT, model TEXT, parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0, title TEXT, last_activity_at REAL, rewind_count INTEGER NOT NULL DEFAULT 0,
    transcript_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0, message_uid TEXT
);
CREATE TABLE session_admissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, admission_id TEXT NOT NULL, request_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT
);
"""

_LEGACY_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT, title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL
);
"""

OK = json.dumps({"output": "done", "exit_code": 0, "error": None})
T0 = 1_793_000_000.0


class Profile:
    """One profile's home: a WAL-mode ``state.db`` plus its cron files."""

    def __init__(self, home: Path, legacy: bool = False):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "state.db"
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_LEGACY_SCHEMA if legacy else _SCHEMA)
        self.legacy = legacy
        self.clock = T0
        self.jobs: list[dict] = []
        self.executions: list[tuple[str, str, str]] = []

    def tick(self) -> float:
        self.clock += 10
        return self.clock

    def session(self, sid=None, source="oneshot", title=None, end_reason="cli_close", parent=None) -> str:
        sid = sid or f"local-{uuid.uuid4().hex}"
        if self.legacy:
            self.db.execute(
                "INSERT INTO sessions (id, source, started_at, ended_at, end_reason, title) VALUES (?,?,?,?,?,?)",
                (sid, source, self.tick(), self.clock + 5, end_reason, title),
            )
        else:
            self.db.execute(
                "INSERT INTO sessions (id, source, origin_json, model, parent_session_id, started_at, ended_at, end_reason, title) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, source, json.dumps({"platform": "local"}), "test-model", parent, self.tick(), self.clock + 5, end_reason, title),
            )
        return sid

    def call(self, sid, tool, args, result=OK, call_id=None, uid=None, ts=None) -> str:
        """An assistant tool call and, unless ``result`` is None, its tool result."""
        call_id = call_id or f"call-{uuid.uuid4().hex[:12]}"
        uid = uid or uuid.uuid4().hex
        ts = ts or self.tick()
        payload = json.dumps([{"id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}])
        if self.legacy:
            self.db.execute(
                "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)", (sid, "assistant", payload, ts)
            )
        else:
            self.db.execute(
                "INSERT INTO messages (session_id, role, tool_calls, timestamp, message_uid) VALUES (?,?,?,?,?)",
                (sid, "assistant", payload, ts, uid),
            )
        if result is not None:
            self.db.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp) VALUES (?,?,?,?,?,?)",
                (sid, "tool", result, call_id, tool, ts + 1),
            )
        self.bump(sid)
        return call_id

    def bump(self, sid):
        if not self.legacy:
            self.db.execute("UPDATE sessions SET message_count = message_count + 2 WHERE id=?", (sid,))

    def cron_run(self, job_id, name, commands, status="completed", end_reason="cron_complete") -> str:
        """One real-shaped cron run: session, admission, execution record, tool calls."""
        execution = uuid.uuid4().hex
        sid = self.session(source="cron", title=f"{name} · Nov 05 02:00", end_reason=end_reason)
        self.db.execute(
            "INSERT INTO session_admissions (admission_id, request_id, target_session_id, status, outcome) VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, f"cron:{job_id}:{execution}", sid, "terminal", "completed"),
        )
        if not any(j["id"] == job_id for j in self.jobs):
            self.jobs.append({"id": job_id, "name": name})
        self.executions.append((execution, job_id, status))
        for command in commands:
            self.call(sid, "terminal", {"command": command})
        return sid

    def write_cron_files(self):
        cron = self.home / "cron"
        cron.mkdir(exist_ok=True)
        (cron / "jobs.json").write_text(json.dumps({"jobs": self.jobs}))
        db = sqlite3.connect(cron / "executions.db")
        db.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT)")
        db.executemany("INSERT OR REPLACE INTO executions VALUES (?,?,?)", self.executions)
        db.commit()
        db.close()


class Fleet:
    def __init__(self, root: Path):
        self.root = root
        self.index = root / "backtest" / "index.db"
        root.mkdir(parents=True, exist_ok=True)

    def profile(self, name: str, legacy: bool = False) -> Profile:
        home = self.root if name == "default" else self.root / "profiles" / name
        return Profile(home, legacy=legacy)


STORY = Path(__file__).resolve().parent.parent / "fixtures" / "story.json"


def stage_story(profile: Profile) -> dict[str, str]:
    """The demo's story from ``fixtures/story.json``: two risky-looking jobs, benign volume, three incidents."""
    story = json.loads(STORY.read_text())
    for _ in range(story["nights"]):
        for job in story["jobs"]:
            profile.cron_run(job["id"], job["name"], job["commands"])
    sessions = {}
    for spec in story["sessions"]:
        sid = profile.session(title=spec["title"])
        for tool, args in spec["calls"]:
            profile.call(sid, tool, args)
        sessions[spec["key"]] = sid
    profile.write_cron_files()
    return sessions
