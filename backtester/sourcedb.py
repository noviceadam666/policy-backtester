"""The only way this plugin opens a database it doesn't own."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open a Hermes-owned SQLite file so the live agent is never locked or written to.

    ``mode=ro`` still sees rows waiting in the WAL (``immutable=1`` wouldn't). Autocommit keeps each
    read snapshot short, so a long ingest can't hold back Hermes's checkpoints.
    """
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
