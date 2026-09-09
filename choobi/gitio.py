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


def _run_bytes(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=str(root), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def current_branch(root: Path) -> Optional[str]:
    """The checked-out branch name, or None when HEAD is detached."""
    proc = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def ls_tree(root: Path, rev: str) -> Dict[str, str]:
    """path -> mode for every entry in the committed tree at `rev` (recursive)."""
    out = _run_bytes(root, "ls-tree", "-r", "-z", rev)
    modes: Dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode()
        modes[path.decode(errors="replace")] = mode
    return modes


def show_blob(root: Path, rev: str, path: str) -> bytes:
    """Committed bytes of `path` at `rev`. Raises RuntimeError when absent."""
    return _run_bytes(root, "show", f"{rev}:{path}")


def commits_between(root: Path, base: str, tip: str) -> List[str]:
    """Commits in base..tip, oldest first."""
    out = _run(root, "rev-list", "--reverse", f"{base}..{tip}")
    return [line for line in out.splitlines() if line.strip()]


def upstream(root: Path) -> Optional["tuple[str, str]"]:
    """(remote, remote_branch) for the current branch's upstream, or None."""
    proc = subprocess.run(
        ["git", "rev-parse", "--symbolic-full-name", "@{u}"],
        cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    full = proc.stdout.strip()            # refs/remotes/<remote>/<branch>
    if not full.startswith("refs/remotes/"):
        return None
    branch = current_branch(root)
    if branch is None:
        return None
    remote = _run(root, "config", f"branch.{branch}.remote").strip()
    prefix = f"refs/remotes/{remote}/"
    if not remote or not full.startswith(prefix):
        return None
    return remote, full[len(prefix):]


def push_fast_forward(
    root: Path, remote: str, branch: str, sha: str, env: Dict[str, str],
) -> None:
    """Push `sha` to `refs/heads/<branch>` on `remote`. Never forces; raises on rejection."""
    _run(root, "push", "--quiet", "--no-verify", remote, f"{sha}:refs/heads/{branch}", env=env)


def pending_refs(root: Path) -> Dict[str, str]:
    """source_sha -> pending docs commit for every refs/choobi/pending/* ref."""
    out = _run(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/choobi/pending/")
    refs: Dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        refname, sha = line.split()
        refs[refname.rsplit("/", 1)[1]] = sha
    return refs
