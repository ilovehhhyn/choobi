"""Auto-push policy: Choobi pushes only where the developer already pushed.

The developer's "commit and push" must implicitly carry the docs commit into the same branch and
pull request without anyone being asked. The rule that keeps this safe:

- push only when the current branch has an upstream,
- only when that upstream already contains the source commit (the developer pushed it), and
- only as a fast-forward of the docs commit onto that upstream. Never `--force`, never a new
  branch, never a branch the developer has not published.

A rejected push is recorded and surfaced, never retried with force; the local docs commit is
still on the branch and rides the developer's next push.
"""
from __future__ import annotations

from pathlib import Path

from . import config, gitio
from .commitwriter import GENERATING_ENV
from .errors import PushRejected

PUSHED = "pushed"
DISABLED = "auto_push_disabled"
NO_UPSTREAM = "no_upstream"
NOT_PUBLISHED = "source_not_pushed_by_user"
NOT_FAST_FORWARD = "upstream_not_behind_docs_commit"


def maybe_push(root: Path, cfg: config.Config, *, source_commit: str, docs_commit: str) -> str:
    """Push `docs_commit` to the branch's upstream if the rule allows. Returns a status code."""
    if not getattr(cfg, "auto_push", True):
        return DISABLED
    target = gitio.upstream(root)
    if target is None:
        return NO_UPSTREAM
    remote, branch = target
    tracking = f"refs/remotes/{remote}/{branch}"
    if not gitio.is_ancestor(root, source_commit, tracking):
        return NOT_PUBLISHED
    if not gitio.is_ancestor(root, tracking, docs_commit):
        return NOT_FAST_FORWARD
    try:
        gitio.push_fast_forward(root, remote, branch, docs_commit, GENERATING_ENV)
    except RuntimeError as exc:
        raise PushRejected(f"{remote}/{branch} refused the docs commit: {exc}") from exc
    return PUSHED
