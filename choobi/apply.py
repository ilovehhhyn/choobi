"""`choobi apply` — land parked docs commits onto the current branch.

A background run parks its verified docs commit on `refs/choobi/pending/<source>` whenever
attaching would have collided with the developer (branch switched, target being edited, git
operation in progress). This verb is the one human-initiated way to land them. It attaches
each pending commit whose source commit is on the current branch, oldest first, records the
result, and pushes under the same rule as a background run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from . import commitwriter, config, gitio, history, pushing
from .errors import Conflict, PushRejected

LANDED = "landed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ApplyOutcome:
    source: str
    pending: str
    status: str      # landed | skipped
    detail: str      # push status when landed, reason when skipped


def _paths_of(root: Path, pending: str) -> List[str]:
    out = gitio._run(root, "diff-tree", "--no-commit-id", "--name-only", "-r", pending)
    return [line for line in out.splitlines() if line.strip()]


def _ordered(root: Path, refs: "dict[str, str]") -> List[str]:
    """Pending sources oldest-first by the pending commit's committer date."""
    stamps = {}
    for source, pending in refs.items():
        stamps[source] = int(gitio._run(root, "show", "-s", "--format=%ct", pending).strip() or 0)
    return sorted(refs, key=lambda source: (stamps[source], source))


def apply_pending(root: Path, cfg: config.Config) -> List[ApplyOutcome]:
    """Attach every parked docs commit that belongs on the current branch."""
    repo_id = config.checkout_id(gitio.common_dir(root))
    refs = gitio.pending_refs(root)
    outcomes: List[ApplyOutcome] = []
    for source in _ordered(root, refs):
        pending = refs[source]
        started = time.monotonic()
        head = gitio.resolve(root, "HEAD")
        if not gitio.is_ancestor(root, source, head):
            outcomes.append(ApplyOutcome(source, pending, SKIPPED,
                                         "source commit is not on the current branch"))
            continue
        paths = _paths_of(root, pending)
        try:
            new_head = commitwriter.attach_pending(root, pending, paths=paths)
        except Conflict as exc:
            outcomes.append(ApplyOutcome(source, pending, SKIPPED, str(exc)))
            continue
        gitio._run(root, "update-ref", "-d", commitwriter.pending_ref(source), pending)
        push_status = "already_landed"
        reason = ""
        if new_head != head:
            try:
                push_status = pushing.maybe_push(root, cfg, source_commit=source,
                                                 docs_commit=new_head)
            except PushRejected as exc:
                push_status, reason = exc.reason, exc.message
        history.add_record(
            repo_id, str(root), "apply", "committed",
            source_commit=source, head_commit=head, docs_commit=new_head,
            duration_ms=int((time.monotonic() - started) * 1000),
            docs_changed=paths,
            summary=f"landed parked docs commit {pending[:7]}",
            reason=reason, push=push_status,
        )
        outcomes.append(ApplyOutcome(source, pending, LANDED, push_status))
    return outcomes


def render(outcomes: List[ApplyOutcome]) -> str:
    if not outcomes:
        return "nothing parked — no pending docs commits."
    lines = []
    for o in outcomes:
        glyph = "✓" if o.status == LANDED else "·"
        lines.append(f"{glyph} {o.source[:7]} -> {o.pending[:7]}  {o.status}  ({o.detail})")
    return "\n".join(lines)
