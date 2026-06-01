import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

from .config import DB_FILE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY,
    operation    TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    files_count  INTEGER DEFAULT 0,
    status       TEXT,
    log          TEXT
);
CREATE TABLE IF NOT EXISTS mdisc_burns (
    id          INTEGER PRIMARY KEY,
    disc_label  TEXT NOT NULL,
    burned_at   TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_size   INTEGER,
    file_mtime  REAL,
    UNIQUE(file_path, disc_label)
);
"""


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def start_run(operation: str, *, dry_run: bool) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs (operation, started_at, dry_run) VALUES (?,?,?)",
            (operation, datetime.now().isoformat(), int(dry_run)),
        )
        return cur.lastrowid  # type: ignore[return-value]


def finish_run(run_id: int, *, status: str, files_count: int, log: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE sync_runs"
            " SET finished_at=?, status=?, files_count=?, log=?"
            " WHERE id=?",
            (datetime.now().isoformat(), status, files_count, log, run_id),
        )


def last_successful_sync(operation: str) -> sqlite3.Row | None:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM sync_runs"
            " WHERE operation=? AND dry_run=0 AND status='success'"
            " ORDER BY finished_at DESC LIMIT 1",
            (operation,),
        ).fetchone()


def recent_runs(operation: str, limit: int = 5) -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM sync_runs WHERE operation=?"
            " ORDER BY started_at DESC LIMIT ?",
            (operation, limit),
        ).fetchall()


def mark_burned(
    *,
    disc_label: str,
    file_path: str,
    file_size: int,
    file_mtime: float,
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mdisc_burns"
            " (disc_label, burned_at, file_path, file_size, file_mtime)"
            " VALUES (?,?,?,?,?)",
            (disc_label, datetime.now().isoformat(), file_path, file_size, file_mtime),
        )


def burned_map() -> dict[str, float]:
    """Returns {file_path: file_mtime_at_burn} for the most recent burn of each file."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT file_path, file_mtime FROM mdisc_burns ORDER BY burned_at"
        ).fetchall()
    return {row["file_path"]: row["file_mtime"] for row in rows}


def burn_labels() -> list[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT disc_label FROM mdisc_burns ORDER BY disc_label"
        ).fetchall()
    return [row["disc_label"] for row in rows]
