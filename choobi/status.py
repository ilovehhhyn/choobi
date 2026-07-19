"""`choobi status` — a deterministic, warm read of local state (build-plan §4.3).

The CLI wording is fixed; the typed reason rides alongside a failed line so a stuck job stays
diagnosable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import config, gitio, history, locking

PENDING = "pending — choobi still working!"
FAILED = "failed — choobi is sorry :< try again pls!"
NOOP = "no-op, choobi decides to not write"
IDLE = "nothing running now!"


def report(root: Path) -> Dict[str, Any]:
    repo_id = config.checkout_id(gitio.common_dir(root))
    checkpoint = history.get_checkpoint(repo_id)
    failed = history.by_status(repo_id, "failed", limit=10)
    no_ops = history.by_status(repo_id, "no_op", limit=50)
    running = locking.is_running(repo_id)
    return {
        "repo_id": repo_id,
        "repo_path": str(root),
        "running": running,
        "checkpoint": checkpoint,
        "failed": failed,
        "no_op_count": len(no_ops),
    }


def render(root: Path) -> str:
    r = report(root)
    lines = []
    if r["running"]:
        lines.append(PENDING)
    for rec in r["failed"]:
        lines.append(f"{FAILED}   ({rec['reason']})")
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
