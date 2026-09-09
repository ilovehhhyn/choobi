"""Docs-only commit writer. Builds the docs commit off the checkout, then attaches it only when
it cannot collide with the developer.

The recursion guard is the inherited CHOOBI_GENERATING marker: the commit is created with
it set, so the post-commit hook it triggers exits immediately (build-plan §3.1). It is
never a commit-message inspection.

Contract (2026-09 redesign):

- The pending commit is built in a detached temporary worktree at the **source branch tip**,
  never at whatever HEAD happens to be. Its parent is therefore always a commit the developer
  made on the branch that produced the source commit.
- Every target's content hash is checked against the committed blob at that tip. A mismatch
  means the draft is stale (someone committed to the document meanwhile) and is a `Conflict`
  the engine may answer by re-running once.
- Attaching is a guarded cherry-pick onto the live checkout that runs only when the checkout is
  still on the source branch, the branch still descends from the build base, no git operation
  is in progress, and the target paths are clean. Any failed guard raises `Parked`: the commit
  stays on `refs/choobi/pending/<source>` for `choobi apply`, and nothing in the checkout moves.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import docs, gitio
from .errors import CommitFailed, Conflict, NotAllowedPath, Parked, TargetNotFound

GENERATING_ENV = {"CHOOBI_GENERATING": "1"}
_UNSET = object()


def pending_ref(source_commit: str) -> str:
    return f"refs/choobi/pending/{source_commit}"


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


def _branch_tip(root: Path, source_commit: str, source_branch: Optional[str]) -> str:
    """The commit the docs commit is built on: the source branch tip, or HEAD when detached."""
    if source_branch is None:
        base = gitio.resolve(root, "HEAD")
    else:
        try:
            base = gitio.resolve(root, f"refs/heads/{source_branch}")
        except RuntimeError as exc:
            raise Conflict(f"branch {source_branch} no longer exists") from exc
    if not gitio.is_ancestor(root, source_commit, base):
        raise Conflict(
            f"source commit {source_commit[:12]} is no longer on "
            f"{source_branch or 'the detached HEAD'}"
        )
    return base


def _check_expected_at(root: Path, rev: str, expected: Dict[str, Optional[str]]) -> None:
    """Every target must still have, at `rev`, the content hash the draft was produced from."""
    tree = docs.Tree.at(root, rev)
    for path in sorted(expected):
        try:
            actual: Optional[str] = tree.read(path)[1]
        except TargetNotFound:
            actual = None
        if actual != expected[path]:
            raise Conflict("a documentation target changed after Choobi verified it")


def _build_pending(
    root: Path, base: str, writes: Dict[str, str], message: str, source_commit: str,
) -> str:
    """Create the docs commit on top of `base` in a throwaway worktree; park it on the ref."""
    ref = pending_ref(source_commit)
    with tempfile.TemporaryDirectory(prefix="choobi-write-") as parent:
        worktree = Path(parent) / "worktree"
        try:
            gitio._run(root, "worktree", "add", "--detach", str(worktree), base)
            pending = _direct_commit(worktree, writes, message)
            gitio._run(root, "update-ref", ref, pending)
        except RuntimeError as exc:
            raise CommitFailed(f"could not build isolated docs commit: {exc}") from exc
        finally:
            if worktree.exists():
                try:
                    gitio._run(root, "worktree", "remove", "--force", str(worktree))
                except RuntimeError as exc:
                    raise CommitFailed(f"could not remove isolated worktree: {exc}") from exc
    return pending


def _cherry_pick(root: Path, pending: str) -> None:
    """Attach `pending` to HEAD; on any failure abort so the checkout is exactly as before."""
    try:
        gitio._run(root, "cherry-pick", pending, env=GENERATING_ENV)
    except RuntimeError as exc:
        detail = str(exc)
        if gitio.has_operation_in_progress(root):
            try:
                gitio._run(root, "cherry-pick", "--abort")
            except RuntimeError as cleanup_exc:
                detail += f"; cherry-pick abort failed: {cleanup_exc}"
        raise RuntimeError(detail) from exc


def _already_landed(root: Path, pending: str, paths: List[str]) -> bool:
    """True when HEAD already holds every path exactly as the pending commit does."""
    head = gitio.ls_tree(root, "HEAD")
    for path in paths:
        if path not in head:
            return False
        try:
            if gitio.show_blob(root, "HEAD", path) != gitio.show_blob(root, pending, path):
                return False
        except RuntimeError:
            return False
    return True


def collision(root: Path, paths: List[str], *, source_branch: Optional[str] = None,
              base: Optional[str] = None) -> Optional[str]:
    """Why attaching onto the live checkout right now would collide with the developer, or None."""
    if source_branch is not None or base is not None:
        branch = gitio.current_branch(root)
        if branch != source_branch:
            return (f"checked-out branch is {branch or 'detached'}, "
                    f"docs were built for {source_branch or 'a detached HEAD'}")
    if base is not None and not gitio.is_ancestor(root, base, gitio.resolve(root, "HEAD")):
        return "the branch moved away from the commit the docs were built on"
    if gitio.has_operation_in_progress(root):
        return "a merge/rebase/cherry-pick is in progress"
    if not gitio.working_tree_clean(root, paths):
        return "a documentation target has uncommitted changes in the working tree"
    return None


def attach_pending(root: Path, pending: str, *, paths: List[str]) -> str:
    """Land an already-parked docs commit onto HEAD. Returns the new HEAD.

    Used by `choobi apply`; the caller has checked that the source commit is on this branch.
    Raises `Conflict` (ref kept) when the checkout is busy or the cherry-pick does not apply.
    """
    if _already_landed(root, pending, paths):
        return gitio.resolve(root, "HEAD")
    why = collision(root, paths)
    if why:
        raise Conflict(why)
    try:
        _cherry_pick(root, pending)
    except RuntimeError as exc:
        raise Conflict(f"pending docs commit could not attach: {exc}") from exc
    return gitio.resolve(root, "HEAD")


def write_and_commit(
    root: Path,
    writes: Dict[str, str],
    message: str,
    *,
    source_commit: str,
    expected_hashes: Dict[str, Optional[str]],
    source_branch: object = _UNSET,
) -> str:
    """Build the docs commit off the source branch tip and attach it if that cannot collide.

    Returns the new HEAD. Raises `Conflict` for a stale draft (nothing built), `CommitFailed`
    when git refuses the commit, and `Parked` when the verified commit exists on the pending ref
    but attaching would have raced the developer. `source_branch` defaults to the checked-out
    branch; pass the branch captured when the job started.
    """
    if set(expected_hashes) != set(writes):
        raise CommitFailed("isolated writes require one verified hash per target")
    branch: Optional[str] = (
        gitio.current_branch(root) if source_branch is _UNSET else source_branch  # type: ignore[assignment]
    )
    base = _branch_tip(root, source_commit, branch)
    _check_expected_at(root, base, expected_hashes)
    paths = sorted(writes)

    pending = _build_pending(root, base, writes, message, source_commit)

    why = collision(root, paths, source_branch=branch, base=base)
    if why:
        raise Parked(pending, why)
    # The branch may have advanced since `base` (same branch, still descends). The target blobs
    # must be unchanged on the actual tip too, or the cherry-pick would rewrite a newer edit.
    head = gitio.resolve(root, "HEAD")
    if head != base:
        try:
            _check_expected_at(root, head, expected_hashes)
        except Conflict as exc:
            raise Parked(pending, str(exc)) from exc
    try:
        _cherry_pick(root, pending)
    except RuntimeError as exc:
        raise Parked(pending, f"pending docs commit could not attach: {exc}") from exc
    gitio._run(root, "update-ref", "-d", pending_ref(source_commit), pending)
    return gitio.resolve(root, "HEAD")
