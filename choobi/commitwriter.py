"""Docs-only commit writer. Writes verified files and commits exactly those paths.

The recursion guard is the inherited CHOOBI_GENERATING marker: the commit is created with
it set, so the post-commit hook it triggers exits immediately (build-plan §3.1). It is
never a commit-message inspection.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Dict, Optional

from . import docs, gitio
from .errors import CommitFailed, Conflict, NotAllowedPath, TargetNotFound

GENERATING_ENV = {"CHOOBI_GENERATING": "1"}


def _direct_commit(root: Path, writes: Dict[str, str], message: str) -> str:
    """Commit verified clean targets, restoring them if Git refuses the commit."""
    paths = sorted(writes)
    targets = {rel: docs.checked_path(root, rel) for rel in paths}
    for rel, path in targets.items():
        if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
            raise CommitFailed(f"{rel} is not a regular repository file")
    originals = {rel: path.read_bytes() if path.exists() else None
                 for rel, path in targets.items()}
    written_hashes = {
        rel: hashlib.sha256(content.encode()).hexdigest() for rel, content in writes.items()
    }
    try:
        for rel, content in writes.items():
            p = targets[rel]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return gitio.commit_paths(root, paths, message, GENERATING_ENV)
    except (OSError, RuntimeError) as exc:
        cleanup_error = ""
        try:
            gitio._run(root, "reset", "-q", "HEAD", "--", *paths)
        except RuntimeError as cleanup_exc:
            cleanup_error = f"; index cleanup failed: {cleanup_exc}"
        concurrent = []
        for rel, content in originals.items():
            path = root / rel
            try:
                current_hash = docs.read_snapshot(root, rel)[1]
            except (NotAllowedPath, TargetNotFound):
                current_hash = None
            if current_hash != written_hashes[rel]:
                concurrent.append(rel)
            elif content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        preserved = f"; concurrent changes preserved in {', '.join(concurrent)}" if concurrent else ""
        raise CommitFailed(f"docs commit failed: {exc}{cleanup_error}{preserved}") from exc


def _check_expected(root: Path, expected: Dict[str, Optional[str]]) -> None:
    paths = sorted(expected)
    if not gitio.working_tree_clean(root, paths):
        raise Conflict("a documentation target changed after Choobi verified it")
    for path in paths:
        try:
            actual = docs.read_snapshot(root, path)[1]
        except TargetNotFound:
            actual = None
        if actual != expected[path]:
            raise Conflict("a documentation target changed after Choobi verified it")
    if not gitio.working_tree_clean(root, paths):
        raise Conflict("a documentation target changed after Choobi verified it")


def _isolated_commit(
    root: Path, writes: Dict[str, str], message: str, source_commit: str,
    expected_hashes: Dict[str, Optional[str]],
) -> str:
    """Build off-checkout, then attach through Git after rechecking the live targets."""
    if set(expected_hashes) != set(writes):
        raise CommitFailed("isolated writes require one verified hash per target")
    if not gitio.is_ancestor(root, source_commit, gitio.resolve(root, "HEAD")):
        raise Conflict(f"source commit {source_commit[:12]} is not on the active branch")
    _check_expected(root, expected_hashes)
    paths = sorted(writes)
    base = gitio.resolve(root, "HEAD")
    pending_ref = f"refs/choobi/pending/{source_commit}"

    with tempfile.TemporaryDirectory(prefix="choobi-write-") as parent:
        worktree = Path(parent) / "worktree"
        try:
            gitio._run(root, "worktree", "add", "--detach", str(worktree), base)
            pending = _direct_commit(worktree, writes, message)
            gitio._run(root, "update-ref", pending_ref, pending)
        except RuntimeError as exc:
            raise CommitFailed(f"could not build isolated docs commit: {exc}") from exc
        finally:
            if worktree.exists():
                try:
                    gitio._run(root, "worktree", "remove", "--force", str(worktree))
                except RuntimeError as exc:
                    raise CommitFailed(f"could not remove isolated worktree: {exc}") from exc

    current_head = gitio.resolve(root, "HEAD")
    if not gitio.is_ancestor(root, source_commit, current_head):
        raise Conflict(f"active branch moved away from source commit {source_commit[:12]}")
    if gitio.has_operation_in_progress(root):
        raise Conflict("a merge/rebase/cherry-pick is in progress")
    _check_expected(root, expected_hashes)

    try:
        gitio._run(root, "update-ref", "-d", pending_ref, pending)
        gitio._run(root, "cherry-pick", pending, env=GENERATING_ENV)
    except RuntimeError as exc:
        abort_error = ""
        if gitio.has_operation_in_progress(root):
            try:
                gitio._run(root, "cherry-pick", "--abort")
            except RuntimeError as cleanup_exc:
                abort_error = f"; cherry-pick abort failed: {cleanup_exc}"
        try:
            gitio._run(root, "update-ref", pending_ref, pending)
        except RuntimeError as cleanup_exc:
            abort_error += f"; pending ref restore failed: {cleanup_exc}"
        raise Conflict(f"pending docs commit could not attach: {exc}{abort_error}") from exc
    return gitio.resolve(root, "HEAD")


def write_and_commit(
    root: Path,
    writes: Dict[str, str],
    message: str,
    *,
    source_commit: str,
    expected_hashes: Dict[str, Optional[str]],
) -> str:
    """Write and commit exactly `writes` through one isolated worktree path."""
    return _isolated_commit(root, writes, message, source_commit, expected_hashes)
