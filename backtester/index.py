"""The plugin-owned index: past tool calls plus session context, never result bodies.

It holds arguments, because policies match on them, so a secret typed into a command stays in here.
The file is created 0600 and never leaves the machine.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile TEXT PRIMARY KEY,
    db_path TEXT NOT NULL,
    schema_version INTEGER,
    ingested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    profile TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source TEXT,
    platform TEXT,
    title TEXT,
    model TEXT,
    parent_session_id TEXT,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT,
    job_kind TEXT,
    job_id TEXT,
    job_name TEXT,
    run_id TEXT,
    run_outcome TEXT,
    fingerprint TEXT,
    PRIMARY KEY (profile, session_id)
);
CREATE TABLE IF NOT EXISTS calls (
    profile TEXT NOT NULL,
    session_id TEXT NOT NULL,
    call_uid TEXT NOT NULL,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT,
    PRIMARY KEY (profile, session_id, call_uid)
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls (ts);
"""


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn
