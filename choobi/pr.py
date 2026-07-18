"""`choobi pr create` — delegate PR creation to the authenticated gh CLI and annotate it.

choobi never claims docs were updated when they were not: the annotation line is inserted
only when a committed docs record exists for this repo (build-plan §3.3).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config, gitio, history
from .errors import ChoobiError

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


def _has_docs_commit(root: Path) -> bool:
    repo_id = config.checkout_id(gitio.common_dir(root))
    return any(r["status"] == "committed" for r in history.recent(repo_id, limit=200))


def create(root: Path) -> str:
    """Create the PR and, if a docs commit exists, append the annotation. Returns the URL."""
    url = _gh(root, "pr", "create", "--fill")
    if _has_docs_commit(root):
        body = _gh(root, "pr", "view", "--json", "body", "-q", ".body")
        if ANNOTATION not in body:
            new_body = (body + "\n\n" + ANNOTATION).strip()
            _gh(root, "pr", "edit", "--body", new_body)
    return url
