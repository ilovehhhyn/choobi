"""`choobi status` — a deterministic, warm read of local state (build-plan §4.3).

The CLI wording is fixed; the typed reason rides alongside a failed line so a stuck job stays
diagnosable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import config, gitio, history, locking

PENDING = "pending — choobi still working!"
PARKED = "parked — a docs commit is waiting; run `choobi apply` to land it"
FAILED = "failed — choobi is sorry :< try again pls!"
FLAGGED = "owner review — choobi left the future-direction doc unchanged"
NOOP = "no-op, choobi decides to not write"
IDLE = "nothing running now!"


def report(root: Path) -> Dict[str, Any]:
    repo_id = config.checkout_id(gitio.common_dir(root))
    checkpoint = history.get_checkpoint(repo_id)
    failed = history.by_status(repo_id, "failed", limit=10)
    flagged = history.by_status(repo_id, "flagged", limit=10)
    no_ops = history.by_status(repo_id, "no_op", limit=50)
    running = locking.is_running(repo_id)
    pending = gitio.pending_refs(root)
    parked_records = {rec["docs_commit"]: rec
                      for rec in history.by_status(repo_id, "parked", limit=50)}
    parked = [
        {"source": source, "pending": sha,
         "reason": parked_records.get(sha, {}).get("reason", ""),
         "summary": parked_records.get(sha, {}).get("summary", "")}
        for source, sha in sorted(pending.items())
    ]
    last_commit = history.by_status(repo_id, "committed", limit=1)
    return {
        "repo_id": repo_id,
        "repo_path": str(root),
        "running": running,
        "checkpoint": checkpoint,
        "failed": failed,
        "flagged": flagged,
        "no_op_count": len(no_ops),
        "parked": parked,
        "last_push": last_commit[0].get("push", "") if last_commit else "",
    }


def render(root: Path) -> str:
    r = report(root)
    lines = []
    if r["running"]:
        lines.append(PENDING)
    for item in r["parked"]:
        why = f" — {item['reason']}" if item["reason"] else ""
        lines.append(f"{PARKED}   ({item['source'][:7]} -> {item['pending'][:7]}{why})")
    for rec in r["failed"]:
        lines.append(f"{FAILED}   ({rec['reason']})")
    for rec in r["flagged"]:
        lines.append(f"{FLAGGED}   ({rec['summary']})")
    if r["no_op_count"]:
        lines.append(f"{NOOP}   (x{r['no_op_count']})")
    cp = r["checkpoint"]
    if cp and cp.get("last_source_commit"):
        sha = cp["last_source_commit"][:7]
        subject = cp.get("last_subject", "")
        lines.append(f"checkpoint {sha}, choobi last worked on {subject}")
    if not r["running"]:
        lines.append(IDLE)
    return "\n".join(lines)
