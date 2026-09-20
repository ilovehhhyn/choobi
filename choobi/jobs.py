"""Durable local queue for post-commit documentation work.

The hook commits the event to SQLite before it starts a disposable worker. A worker claims one
row atomically and records its terminal state. If a worker process dies, the next worker recovers
the abandoned `running` row after obtaining the per-repository worker lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config, gitio, history

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass(frozen=True)
class Job:
    id: int
    repo_id: str
    repo_path: str
    source_commit: str
    state: str
    attempts: int
    error: str


def _job(row: object) -> Job:
    return Job(**{key: row[key] for key in (
        "id", "repo_id", "repo_path", "source_commit", "state", "attempts", "error",
    )})


def enqueue(root: Path, source_commit: str) -> int:
    """Persist one idempotent commit event and return its stable job id."""
    repo_id = config.checkout_id(gitio.common_dir(root))
    now = history._now()
    conn = history.connect()
    with conn:
        conn.execute(
            """INSERT INTO jobs
               (repo_id, repo_path, source_commit, state, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo_id, source_commit) DO NOTHING""",
            (repo_id, str(root), source_commit, QUEUED, now, now),
        )
        row = conn.execute(
            "SELECT id FROM jobs WHERE repo_id=? AND source_commit=?",
            (repo_id, source_commit),
        ).fetchone()
    conn.close()
    history.register_repo(repo_id, str(root))
    return int(row["id"])


def list_jobs(repo_id: str) -> List[Job]:
    conn = history.connect()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE repo_id=? ORDER BY id", (repo_id,)
    ).fetchall()
    conn.close()
    return [_job(row) for row in rows]


def recover_running(repo_id: str) -> None:
    """Return jobs abandoned by a previous worker process to the queue."""
    conn = history.connect()
    with conn:
        conn.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE repo_id=? AND state=?",
            (QUEUED, history._now(), repo_id, RUNNING),
        )
    conn.close()


def claim_next(repo_id: str) -> Optional[Job]:
    """Atomically claim the oldest queued job for one repository."""
    conn = history.connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM jobs WHERE repo_id=? AND state=? ORDER BY id LIMIT 1",
        (repo_id, QUEUED),
    ).fetchone()
    if row is None:
        conn.commit()
        conn.close()
        return None
    conn.execute(
        "UPDATE jobs SET state=?, attempts=attempts+1, updated_at=? WHERE id=?",
        (RUNNING, history._now(), row["id"]),
    )
    conn.commit()
    claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    conn.close()
    return _job(claimed)


def finish(job_id: int, state: str, error: str = "") -> None:
    if state not in (SUCCEEDED, FAILED):
        raise ValueError(f"invalid terminal job state: {state}")
    conn = history.connect()
    with conn:
        conn.execute(
            "UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?",
            (state, error, history._now(), job_id),
        )
    conn.close()
