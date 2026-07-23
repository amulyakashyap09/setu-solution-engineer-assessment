"""SQLite connection handling.

One connection per request (FastAPI dependency), WAL mode so that the
read-heavy reporting endpoints are never blocked by an ingest write.

Route handlers are declared as sync `def`, which means FastAPI runs them
in a worker threadpool. That is the correct pairing for the stdlib
sqlite3 driver, which is blocking - pretending it is async would only
stall the event loop.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from .config import settings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# A single shared in-memory connection is required when DATABASE_PATH is
# ":memory:", otherwise each connect() would get its own private database.
_memory_conn: sqlite3.Connection | None = None
_memory_lock = threading.Lock()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA synchronous = NORMAL")
    # `isolation_level = None` hands transaction control to us explicitly,
    # so ingest can open a single BEGIN IMMEDIATE for a whole batch.
    conn.isolation_level = None


def connect(database_path: str | None = None) -> sqlite3.Connection:
    path = database_path or settings.database_path

    if path == ":memory:":
        global _memory_conn
        with _memory_lock:
            if _memory_conn is None:
                _memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                _configure(_memory_conn)
                init_schema(_memory_conn)
            return _memory_conn

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    _configure(conn)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text())


def init_db(database_path: str | None = None) -> None:
    """Create the schema if it does not already exist."""
    conn = connect(database_path)
    try:
        init_schema(conn)
    finally:
        if conn is not _memory_conn:
            conn.close()


def reset_memory_db() -> None:
    """Drop the shared in-memory connection. Test-support only."""
    global _memory_conn
    with _memory_lock:
        if _memory_conn is not None:
            _memory_conn.close()
            _memory_conn = None


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding a per-request connection."""
    conn = connect()
    try:
        yield conn
    finally:
        if conn is not _memory_conn:
            conn.close()
