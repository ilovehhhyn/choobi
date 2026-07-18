"""Thin, synchronous git wrappers over subprocess. No libgit2, no background threads.

Every function runs `git` in a given repo root and returns plain data. The commit writer
lives here too because it is just guarded git plumbing.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def _run(root: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError("not inside a git repository")
    return Path(out.stdout.strip())


def common_dir(root: Path) -> str:
    """Absolute git common dir; shared across linked worktrees."""
    out = _run(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return out


def resolve(root: Path, rev: str) -> str:
    return _run(root, "rev-parse", rev).strip()


def commit_message(root: Path, sha: str) -> str:
    """Full raw message (subject + body), byte-preserving via %B."""
    return _run(root, "show", "-s", "--format=%B", sha).rstrip("\n")


def commit_subject(root: Path, sha: str) -> str:
    return _run(root, "show", "-s", "--format=%s", sha).strip()


def changed_files(root: Path, rev_range: str) -> List[str]:
    out = _run(root, "diff", "--name-only", rev_range)
    return [line for line in out.splitlines() if line.strip()]


def diff(root: Path, rev_range: str) -> str:
    return _run(root, "diff", rev_range)


def working_diff(root: Path, staged: bool) -> str:
    return _run(root, "diff", "--cached") if staged else _run(root, "diff")


def working_changed(root: Path, staged: bool) -> List[str]:
    args = ["diff", "--name-only"] + (["--cached"] if staged else [])
    return [line for line in _run(root, *args).splitlines() if line.strip()]


def added_files(root: Path, rev_range: str) -> List[str]:
    """Files newly added (not modified) in the range: git diff --diff-filter=A."""
    out = _run(root, "diff", "--name-only", "--diff-filter=A", rev_range)
    return [line for line in out.splitlines() if line.strip()]


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(root),
        capture_output=True,
    )
    return proc.returncode == 0


def tracked_files(root: Path) -> List[str]:
    return [f for f in _run(root, "ls-files").splitlines() if f.strip()]


def file_hash(root: Path, rel_path: str) -> Optional[str]:
    """SHA-256 of a file's current bytes, or None if absent."""
    p = root / rel_path
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def has_operation_in_progress(root: Path) -> bool:
    """True if a merge/rebase/cherry-pick is mid-flight (build-plan §3.1 guard)."""
    gitdir = Path(_run(root, "rev-parse", "--git-dir").strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    markers = ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"]
    return any((gitdir / m).exists() for m in markers)


def working_tree_clean(root: Path, paths: List[str]) -> bool:
    """True if the given paths have no staged or unstaged changes."""
    out = _run(root, "status", "--porcelain", "--", *paths) if paths else _run(root, "status", "--porcelain")
    return out.strip() == ""


def commit_paths(
    root: Path,
    paths: List[str],
    message: str,
    generating_env: Dict[str, str],
) -> str:
    """Stage only `paths` and commit them with the exact `message`. Returns the new sha.

    `generating_env` carries the recursion-guard marker so the commit's own post-commit
    hook exits immediately (build-plan §3.1). Signing policy is inherited from git config.
    """
    _run(root, "add", "--", *paths, env=generating_env)
    # --cleanup=verbatim preserves a reused source message byte-for-byte (build-plan §3.1).
    _run(root, "commit", "--cleanup=verbatim", "-m", message, "--", *paths, env=generating_env)
    return resolve(root, "HEAD")
