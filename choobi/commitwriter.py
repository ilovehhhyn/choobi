"""Docs-only commit writer. Writes verified files and commits exactly those paths.

The recursion guard is the inherited CHOOBI_GENERATING marker: the commit is created with
it set, so the post-commit hook it triggers exits immediately (build-plan §3.1). It is
never a commit-message inspection.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from . import gitio

GENERATING_ENV = {"CHOOBI_GENERATING": "1"}


def write_and_commit(root: Path, writes: Dict[str, str], message: str) -> str:
    """Write each path, then commit only those paths with `message`. Returns docs sha."""
    paths = sorted(writes)
    for rel, content in writes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return gitio.commit_paths(root, paths, message, GENERATING_ENV)
