"""Read-only ingest of each profile's ``state.db`` into the plugin-owned index.

Plain SQL over Hermes's tables, tolerant of columns a given schema version lacks. Incremental per
session: only sessions whose fingerprint moved get their messages re-read. Index rows are never
deleted, so attempts outlive Hermes's own pruning.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .index import open_index
from .paths import Profile, all_profiles, discover_profiles
from .sourcedb import column_names, open_readonly, table_names

# Argument values that are file or message bodies: matchers don't use them and they can hold secrets.
_BODY_ARGS = frozenset({"content", "old_string", "new_string", "patch"})

_SESSION_COLUMNS = (
    "id", "source", "origin_json", "title", "model", "parent_session_id", "started_at", "ended_at",
    "end_reason", "message_count", "tool_call_count", "transcript_revision", "rewind_count",
    "last_activity_at",
)
_CRON_SESSION_ID = re.compile(r"^cron_([0-9a-f]+)_")
_DENIED_ERROR = re.compile(r"^(Access denied|BLOCKED)")


@dataclass
class ProfileSummary:
    profile: str
    sessions: int = 0
    sessions_read: int = 0
    calls: int = 0
    new_calls: int = 0
    error: str | None = None


@dataclass
class IngestSummary:
    profiles: list[ProfileSummary] = field(default_factory=list)

    @property
    def new_calls(self) -> int:
        return sum(p.new_calls for p in self.profiles)


@dataclass(frozen=True)
class ScheduledJob:
    kind: str
    profile: str
    job_id: str
    name: str | None


@dataclass(frozen=True)
class JobLink:
    kind: str
    job_id: str
    job_name: str | None
    run_id: str | None
    outcome: str


def ingest(root: Path, index_path: Path, only: list[str] | None = None) -> IngestSummary:
    summary = IngestSummary()
    index = open_index(index_path)
    try:
        for profile in discover_profiles(root, only):
            row = ProfileSummary(profile.name)
            summary.profiles.append(row)
            try:
                _ingest_profile(profile, root, index, row)
            except sqlite3.Error as exc:
                index.rollback()
                row.error = str(exc)
    finally:
        index.close()
    return summary


def _ingest_profile(profile: Profile, root: Path, index: sqlite3.Connection, row: ProfileSummary) -> None:
    source = open_readonly(profile.state_db)
    try:
        sessions = _read_sessions(source)
        links = _resolve_job_links(source, sessions, profile, root)
        seen = {
            r["session_id"]: r["fingerprint"]
            for r in index.execute("SELECT session_id, fingerprint FROM sessions WHERE profile=?", (profile.name,))
        }
        latest_message = dict(
            source.execute("SELECT session_id, MAX(id) FROM messages GROUP BY session_id").fetchall()
        )
        before = _count_calls(index, profile.name)
        for session in sessions:
            sid = session["id"]
            fingerprint = _fingerprint(session, latest_message.get(sid))
            _upsert_session(index, profile.name, session, links.get(sid), fingerprint)
            if seen.get(sid) != fingerprint:
                _ingest_messages(source, index, profile.name, sid)
                row.sessions_read += 1
        version = _schema_version(source)
    finally:
        source.close()
    index.execute(
        "INSERT INTO profiles (profile, db_path, schema_version, ingested_at) VALUES (?,?,?,?) "
        "ON CONFLICT(profile) DO UPDATE SET db_path=excluded.db_path, "
        "schema_version=excluded.schema_version, ingested_at=excluded.ingested_at",
        (profile.name, str(profile.state_db), version, time.time()),
    )
    index.commit()
    row.sessions = len(sessions)
    row.calls = _count_calls(index, profile.name)
    row.new_calls = row.calls - before


def _count_calls(index: sqlite3.Connection, profile: str) -> int:
    return index.execute("SELECT COUNT(*) FROM calls WHERE profile=?", (profile,)).fetchone()[0]


def _schema_version(source: sqlite3.Connection) -> int | None:
    if "schema_version" not in table_names(source):
        return None
    row = source.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else None


def _read_sessions(source: sqlite3.Connection) -> list[dict]:
    present = column_names(source, "sessions")
    select = ", ".join(c if c in present else f"NULL AS {c}" for c in _SESSION_COLUMNS)
    return [dict(r) for r in source.execute(f"SELECT {select} FROM sessions")]


def _fingerprint(session: dict, latest_message: int | None) -> str:
    parts = (
        session["message_count"], session["tool_call_count"], session["ended_at"], session["end_reason"],
        session["transcript_revision"], session["rewind_count"], session["last_activity_at"], latest_message,
    )
    return "|".join(str(p) for p in parts)


def _platform(session: dict) -> str | None:
    try:
        origin = json.loads(session["origin_json"] or "{}")
    except ValueError:
        return None
    return origin.get("platform") if isinstance(origin, dict) else None


def _upsert_session(index: sqlite3.Connection, profile: str, session: dict, link: JobLink | None, fingerprint: str) -> None:
    index.execute(
        """
        INSERT INTO sessions (profile, session_id, source, platform, title, model, parent_session_id, started_at,
            ended_at, end_reason, job_kind, job_id, job_name, run_id, run_outcome, fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(profile, session_id) DO UPDATE SET
            source=excluded.source, platform=excluded.platform, title=excluded.title, model=excluded.model,
            parent_session_id=excluded.parent_session_id, started_at=excluded.started_at, ended_at=excluded.ended_at,
            end_reason=excluded.end_reason, job_kind=excluded.job_kind, job_id=excluded.job_id,
            job_name=excluded.job_name, run_id=excluded.run_id, run_outcome=excluded.run_outcome,
            fingerprint=excluded.fingerprint
        """,
        (
            profile, session["id"], session["source"], _platform(session), session["title"], session["model"],
            session["parent_session_id"], session["started_at"], session["ended_at"], session["end_reason"],
            link.kind if link else None, link.job_id if link else None, link.job_name if link else None,
            link.run_id if link else None, link.outcome if link else None, fingerprint,
        ),
    )


# --- Tool calls ------------------------------------------------------------------------------------


def _ingest_messages(source: sqlite3.Connection, index: sqlite3.Connection, profile: str, session_id: str) -> None:
    """Read one session's calls and result statuses.

    Compaction re-inserts old rows into the same session, so one call can appear many times. The
    ``message_uid`` survives those copies, and it plus the tool call id is the call's identity.
    """
    uid_column = "message_uid" if "message_uid" in column_names(source, "messages") else "NULL AS message_uid"
    rows = source.execute(
        f"""
        SELECT id, role, tool_calls, tool_call_id, tool_name, timestamp, {uid_column},
               CASE WHEN role='tool' THEN content END AS result
        FROM messages
        WHERE session_id=? AND (role='tool' OR (tool_calls IS NOT NULL AND tool_calls NOT IN ('', '[]')))
        ORDER BY id
        """,
        (session_id,),
    )
    latest_uid_by_tool_call: dict[str, str] = {}
    for row in rows:
        if row["role"] == "tool":
            call_uid = latest_uid_by_tool_call.get(row["tool_call_id"] or "")
            if call_uid:
                index.execute(
                    "UPDATE calls SET status=COALESCE(status, ?) WHERE profile=? AND session_id=? AND call_uid=?",
                    (_result_status(row["tool_name"], row["result"]), profile, session_id, call_uid),
                )
            continue
        message_uid = row["message_uid"] or f"m{row['id']}"
        for position, call in enumerate(_parse_tool_calls(row["tool_calls"])):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            tool = function.get("name")
            if not tool:
                continue
            tool_call_id = call.get("id") or f"#{position}"
            call_uid = f"{message_uid}:{tool_call_id}"
            latest_uid_by_tool_call[tool_call_id] = call_uid
            index.execute(
                """
                INSERT INTO calls (profile, session_id, call_uid, ts, tool, args) VALUES (?,?,?,?,?,?)
                ON CONFLICT(profile, session_id, call_uid) DO UPDATE SET ts=MIN(ts, excluded.ts)
                """,
                (profile, session_id, call_uid, row["timestamp"], tool, _clean_args(function.get("arguments"))),
            )


def _parse_tool_calls(raw: str) -> list[dict]:
    try:
        calls = json.loads(raw)
    except ValueError:
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _clean_args(arguments) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {"_unparsed": arguments}
    if not isinstance(arguments, dict):
        arguments = {}
    kept = {
        key: f"<omitted {len(value)} chars>" if key in _BODY_ARGS and isinstance(value, str) else value
        for key, value in arguments.items()
    }
    return json.dumps(kept, sort_keys=True)


def _result_status(tool: str | None, body: str | None) -> str:
    """Classify a result as ok, error or denied, matching Hermes's own ``post_tool_call`` status.

    The body is parsed here and dropped: only this one word reaches the index.
    """
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return "ok"
    if not isinstance(data, dict):
        return "ok"
    error = data.get("error")
    if data.get("user_summary") or (isinstance(error, str) and _DENIED_ERROR.match(error)):
        return "denied"
    if error or data.get("success") is False:
        return "error"
    if tool == "terminal" and data.get("exit_code") not in (None, 0):
        return "error"
    return "ok"


# --- Cron and Kanban links -------------------------------------------------------------------------


def _resolve_job_links(source: sqlite3.Connection, sessions: list[dict], profile: Profile, root: Path) -> dict[str, JobLink]:
    """Which scheduled run each session belongs to, and whether that run completed.

    ``session_admissions.request_id`` holds ``cron:<job>:<execution>`` or
    ``kanban:["<board>","<task>",<run>]``. Older sessions have no row, so cron falls back to the
    session id and ``end_reason``. A subagent inherits the run of the session that spawned it.
    """
    admissions = _read_admissions(source)
    cron = _CronLookup(profile.home)
    kanban = _KanbanLookup(root)
    links: dict[str, JobLink] = {}
    parents: dict[str, str] = {}
    for session in sessions:
        sid = session["id"]
        if session["parent_session_id"]:
            parents[sid] = session["parent_session_id"]
        admission = admissions.get(sid)
        if admission and admission["request_id"].startswith("cron:"):
            links[sid] = cron.link_from_admission(admission, session)
        elif admission and admission["request_id"].startswith("kanban:"):
            link = kanban.link_from_admission(admission)
            if link:
                links[sid] = link
        elif session["source"] == "cron":
            links[sid] = cron.link_from_session(session)
        elif session["source"] == "kanban":
            links[sid] = JobLink("kanban", sid, None, None, "unknown")
    for sid, parent in parents.items():
        seen = {sid}
        while sid not in links and parent and parent not in seen:
            if parent in links:
                links[sid] = links[parent]
                break
            seen.add(parent)
            parent = parents.get(parent)
    return links


def _read_admissions(source: sqlite3.Connection) -> dict[str, dict]:
    """The latest cron or Kanban admission per session; other admissions say nothing about jobs."""
    if "session_admissions" not in table_names(source):
        return {}
    rows = source.execute(
        "SELECT target_session_id, request_id, status, outcome FROM session_admissions "
        "WHERE request_id LIKE 'cron:%' OR request_id LIKE 'kanban:%' ORDER BY seq"
    )
    return {r["target_session_id"]: dict(r) for r in rows}


def _read_jobs(home: Path) -> list[dict]:
    try:
        jobs = json.loads((home / "cron" / "jobs.json").read_text()).get("jobs", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [j for j in jobs if isinstance(j, dict) and j.get("id")] if isinstance(jobs, list) else []


def scheduled_jobs(root: Path, only: list[str] | None = None) -> list[ScheduledJob]:
    """The cron jobs each profile has scheduled now: enabled in ``cron/jobs.json`` and not finished.

    A profile counts even before it has a ``state.db``, since a new app's jobs are the ones with no
    history. Kanban tasks are one-off work with no schedule to read, so only cron jobs are listed.
    """
    return [
        ScheduledJob("cron", profile.name, str(job["id"]), job.get("name"))
        for profile in all_profiles(root, only)
        for job in _read_jobs(profile.home)
        if job.get("enabled", True) is not False and job.get("state") != "completed"
    ]


class _CronLookup:
    """Job names from ``cron/jobs.json`` and run status from ``cron/executions.db``, when present."""

    def __init__(self, home: Path):
        self._names = {j["id"]: j.get("name") for j in _read_jobs(home)}
        self._statuses = self._read_statuses(home / "cron" / "executions.db")

    @staticmethod
    def _read_statuses(path: Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        try:
            conn = open_readonly(path)
            try:
                return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM executions")}
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    def _name(self, job_id: str, session: dict) -> str | None:
        title = session["title"] or ""
        return self._names.get(job_id) or (title.split(" · ")[0] if " · " in title else None)

    def link_from_admission(self, admission: dict, session: dict) -> JobLink:
        _, job_id, execution_id = (admission["request_id"].split(":", 2) + ["", ""])[:3]
        status = self._statuses.get(execution_id)
        if status is None:
            status = admission["outcome"] or ("running" if admission["status"] != "terminal" else "unknown")
        # The scheduler books a run as complete only once its last message is a real assistant reply.
        if status == "completed" and session["end_reason"] not in (None, "cron_complete"):
            status = "incomplete"
        return JobLink("cron", job_id, self._name(job_id, session), execution_id or None, status)

    def link_from_session(self, session: dict) -> JobLink:
        match = _CRON_SESSION_ID.match(session["id"])
        job_id = match.group(1) if match else session["id"]
        outcome = "completed" if session["end_reason"] == "cron_complete" else "unknown"
        return JobLink("cron", job_id, self._name(job_id, session), None, outcome)


class _KanbanLookup:
    """Task titles and run outcomes from each board's own database, when present."""

    def __init__(self, root: Path):
        self._root = root

    def link_from_admission(self, admission: dict) -> JobLink | None:
        try:
            board, task_id, run_id = json.loads(admission["request_id"][len("kanban:"):])
        except (ValueError, TypeError):
            return None
        title, outcome = self._read_run(board, task_id, run_id)
        if outcome is None:
            outcome = admission["outcome"] or ("running" if admission["status"] != "terminal" else "unknown")
        return JobLink("kanban", f"{board}/{task_id}", title, str(run_id), outcome)

    def _read_run(self, board: str, task_id: str, run_id) -> tuple[str | None, str | None]:
        path = self._root / "kanban" / "boards" / str(board) / "kanban.db"
        if not path.is_file():
            return None, None
        try:
            conn = open_readonly(path)
            try:
                task = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
                run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None, None
        return (task["title"] if task else None), (run["outcome"] if run else None)
