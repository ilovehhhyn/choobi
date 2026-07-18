"""Per-repo advisory lock so two updates never write the same repo at once.

flock-based, non-blocking: a second update sees the lock held and reports pending rather
than racing. This is the only concurrency control v1 needs — commits are human-paced.
"""
from __future__ import annotations

import fcntl
from pathlib import Path
from typing import Optional

from . import config


def _path(repo_id: str) -> Path:
    return config.repo_dir(repo_id) / "update.lock"


class RepoLock:
    def __init__(self, repo_id: str) -> None:
        self.path = _path(repo_id)
        self._f: Optional["open"] = None  # type: ignore[valid-type]

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        if self._f is not None:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None


def is_running(repo_id: str) -> bool:
    """True if another process currently holds the lock."""
    p = _path(repo_id)
    if not p.exists():
        return False
    with open(p, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
