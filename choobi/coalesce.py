"""Coalescing for background commit events.

The post-commit hook launches one detached job per commit and the jobs serialize on the
repository lock. When a developer (or their coding agent) lands several commits in quick
succession, the older jobs are still waiting when the newer commits exist. Running each of them
would produce one docs commit per source commit, interleaved with the developer's own history.

Instead, a job that acquires the lock and finds newer *human* commits on its branch records
itself as `coalesced` and exits; the newest commit's own job widens its diff range back over the
coalesced commits so nothing is skipped. Choobi's own docs commits never count as newer human
work, so a job is not coalesced into nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import gitio, history

RUN = "run"
COALESCED = "coalesced"
UNREACHABLE = "unreachable"
MAX_WIDEN = 50


@dataclass(frozen=True)
class Decision:
    action: str                 # run | coalesced | unreachable
    rev_range: Optional[str]    # for run: the widened range this job should review
    into: Optional[str] = None  # for coalesced: the newer commit whose job takes over


def _parent(root: Path, sha: str) -> Optional[str]:
    try:
        return gitio.resolve(root, sha + "^")
    except RuntimeError:
        return None


def widened_range(root: Path, repo_id: str, source_commit: str, empty_tree: str) -> str:
    """`source_commit^..source_commit`, extended back over commits recorded as coalesced."""
    start = _parent(root, source_commit)
    steps = 0
    while start is not None and steps < MAX_WIDEN:
        record = history.find_by_source(repo_id, start)
        if not record or record["status"] != "coalesced":
            break
        start = _parent(root, start)
        steps += 1
    return f"{start or empty_tree}..{source_commit}"


def decide(root: Path, repo_id: str, source_commit: str, empty_tree: str) -> Decision:
    """What a queued post-commit job for `source_commit` should do now that it holds the lock."""
    head = gitio.resolve(root, "HEAD")
    if not gitio.is_ancestor(root, source_commit, head):
        if not gitio.branches_containing(root, source_commit):
            return Decision(UNREACHABLE, None)
        # On another branch: run normally; the engine builds on that branch's tip and parks.
        return Decision(RUN, widened_range(root, repo_id, source_commit, empty_tree))
    newer = gitio.commits_between(root, source_commit, head)
    if newer:
        choobi_commits = history.docs_commits(repo_id) | set(gitio.pending_refs(root).values())
        human = [sha for sha in newer if sha not in choobi_commits]
        if human:
            return Decision(COALESCED, None, into=human[-1])
    return Decision(RUN, widened_range(root, repo_id, source_commit, empty_tree))
