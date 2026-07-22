"""Personal activity store — the source of truth for what choobi did (build-plan §8.2).

SQLite under ~/.choobi/choobi.db. Records every run (committed / no_op / flagged / failed) and a
per-repo checkpoint. The source_commit -> record mapping gives idempotency and recovery.
No source code, secrets, or chat transcripts are stored.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       TEXT NOT NULL,
    repo_path     TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    source_commit TEXT,
    head_commit   TEXT,
    docs_commit   TEXT,
    ts            TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    docs_changed  TEXT NOT NULL DEFAULT '[]',
    summary       TEXT NOT NULL DEFAULT '',
    patch         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    in_tokens     INTEGER NOT NULL DEFAULT 0,
    out_tokens    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checkpoints (
    repo_id    TEXT PRIMARY KEY,
    repo_path  TEXT NOT NULL,
    last_source_commit TEXT,
    last_subject TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    repo_id     TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    initialized INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_repo ON records(repo_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(repo_id, source_commit);
"""

_COLUMNS = [
    "id", "repo_id", "repo_path", "trigger", "source_commit", "head_commit",
    "docs_commit", "ts", "duration_ms", "docs_changed", "summary", "patch",
    "status", "reason", "in_tokens", "out_tokens",
]


def connect() -> sqlite3.Connection:
    config.db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_record(
    repo_id: str,
    repo_path: str,
    trigger: str,
    status: str,
    *,
    source_commit: Optional[str] = None,
    head_commit: Optional[str] = None,
    docs_commit: Optional[str] = None,
    duration_ms: int = 0,
    docs_changed: Optional[List[str]] = None,
    summary: str = "",
    patch: str = "",
    reason: str = "",
    in_tokens: int = 0,
    out_tokens: int = 0,
) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO records
               (repo_id, repo_path, trigger, source_commit, head_commit, docs_commit,
                ts, duration_ms, docs_changed, summary, patch, status, reason,
                in_tokens, out_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (repo_id, repo_path, trigger, source_commit, head_commit, docs_commit,
             _now(), duration_ms, json.dumps(docs_changed or []), summary, patch,
             status, reason, in_tokens, out_tokens),
        )
    conn.close()
    register_repo(repo_id, repo_path)
    return int(cur.lastrowid)


def register_repo(repo_id: str, path: str, initialized: bool = False) -> None:
    """Record a repo choobi knows about. `initialized` marks that `choobi init` ran here."""
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO repos (repo_id, path, initialized, first_seen, last_seen)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 path=excluded.path,
                 last_seen=excluded.last_seen,
                 initialized=MAX(repos.initialized, excluded.initialized)""",
            (repo_id, path, 1 if initialized else 0, _now(), _now()),
        )
    conn.close()


def list_repos() -> List[Dict[str, Any]]:
    """All known repos, newest activity first, with a per-repo record count."""
    conn = connect()
    rows = conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM records x WHERE x.repo_id=r.repo_id) AS records
           FROM repos r ORDER BY r.last_seen DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_repo(repo_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    row = conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_by_source(repo_id: str, source_commit: str) -> Optional[Dict[str, Any]]:
    """Latest completed record for a source commit (idempotency lookup)."""
    conn = connect()
    row = conn.execute(
        """SELECT * FROM records
           WHERE repo_id=? AND source_commit=? AND status IN ('committed','no_op','flagged')
           ORDER BY id DESC LIMIT 1""",
        (repo_id, source_commit),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def recent(repo_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    conn = connect()
    if repo_id:
        rows = conn.execute(
            "SELECT * FROM records WHERE repo_id=? ORDER BY id DESC LIMIT ?",
            (repo_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get(record_id: int) -> Optional[Dict[str, Any]]:
    conn = connect()
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def by_status(repo_id: str, status: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM records WHERE repo_id=? AND status=? ORDER BY id DESC LIMIT ?",
        (repo_id, status, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_checkpoint(repo_id: str, repo_path: str, source_commit: str, subject: str) -> None:
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO checkpoints (repo_id, repo_path, last_source_commit, last_subject, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 repo_path=excluded.repo_path,
                 last_source_commit=excluded.last_source_commit,
                 last_subject=excluded.last_subject,
                 updated_at=excluded.updated_at""",
            (repo_id, repo_path, source_commit, subject, _now()),
        )
    conn.close()


def get_checkpoint(repo_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    row = conn.execute("SELECT * FROM checkpoints WHERE repo_id=?", (repo_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
