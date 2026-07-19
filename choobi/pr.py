"""`choobi pr create` — delegate PR creation to the authenticated gh CLI and annotate it.

choobi never claims docs were updated when they were not: the annotation line is inserted
only when a committed docs record exists for this repo (build-plan §3.3).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config, gitio, history, locking
from .errors import ChoobiError, PendingDocsUpdate

ANNOTATION = "choobi updated docs."


class GhUnavailable(ChoobiError):
    reason = "gh_unavailable"


def _gh(root: Path, *args: str) -> str:
    binary = shutil.which("gh")
    if not binary:
        raise GhUnavailable("gh CLI not found on PATH")
    proc = subprocess.run([binary, *args], cwd=str(root), capture_output=True, text=True)
    if proc.returncode != 0:
        raise ChoobiError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _has_docs_commit(root: Path, base: str, head: str) -> bool:
    repo_id = config.checkout_id(gitio.common_dir(root))
    for record in history.recent(repo_id, limit=200):
        commit = record.get("docs_commit")
        if commit and gitio.is_ancestor(root, commit, head) \
                and not gitio.is_ancestor(root, commit, base):
            return True
    return False


def create(root: Path) -> str:
    """Create the PR and, if a docs commit exists, append the annotation. Returns the URL."""
    repo_id = config.checkout_id(gitio.common_dir(root))
    lock = locking.RepoLock(repo_id)
    if not lock.acquire():
        raise PendingDocsUpdate("wait for the active documentation update before creating a PR")
    try:
        url = _gh(root, "pr", "create", "--fill")
        bounds = _gh(root, "pr", "view", "--json", "baseRefOid,headRefOid", "-q",
                     '.baseRefOid + " " + .headRefOid').split()
        if len(bounds) != 2:
            raise ChoobiError("gh returned an invalid PR range")
        if _has_docs_commit(root, bounds[0], bounds[1]):
            body = _gh(root, "pr", "view", "--json", "body", "-q", ".body")
            if ANNOTATION not in body:
                new_body = (body + "\n\n" + ANNOTATION).strip()
                _gh(root, "pr", "edit", "--body", new_body)
        return url
    finally:
        lock.release()
