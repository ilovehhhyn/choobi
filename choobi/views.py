"""CLI presentation for the read-only browsing commands (docs, changelog, show, style).

These render the SAME data the UI panels read (history, docs, baseline) — the CLI and the
window share the data layer and differ only in presentation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import baseline, config, docs as docs_mod
from .runtime import Runtime

_GLYPH = {"committed": "✓", "no_op": "·", "flagged": "!", "failed": "✕"}


def _when(ts: str) -> str:
    return ts[:16].replace("T", " ")


def _kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB"


def render_docs(root: Path, scope: docs_mod.ReviewScope, runtime: Runtime) -> str:
    """The three questions: where the docs are, which ones choobi reads, which it may write.

    Read scope and write scope are different boundaries and get separate sections, because
    conflating them is what makes "why didn't choobi pick that doc" unanswerable. The budget is
    read off the runtime so the number shown is the one the next run will actually enforce.
    """
    policy = baseline.policy()
    budget = runtime.prompt_budget_bytes
    inside, outside = docs_mod.scope_census(root, scope)
    reviewed = sum(size for _, size in inside)
    lines = [
        "review scope — the docs choobi reads to choose an owner:",
        f"  {len(inside)} documents, {_kb(reviewed)} of the {_kb(budget)} context budget "
        f"({reviewed * 100 // budget if budget else 0}%)  [{runtime.model}]",
        "  include: " + ", ".join(scope.include),
        "  exclude: " + ", ".join(scope.exclude),
    ]
    if reviewed > budget:
        lines.append("  OVER BUDGET — narrow review_scope in this repo's SOP (`choobi style`).")
    if outside:
        skipped = sum(size for _, size in outside)
        lines.append(f"\nout of scope — {len(outside)} documents, {_kb(skipped)} never reviewed:")
        for path, size in sorted(outside, key=lambda item: item[1], reverse=True)[:10]:
            lines.append(f"  {_kb(size):>10}  {path}")
        if len(outside) > 10:
            lines.append(f"  ... and {len(outside) - 10} more")

    writable = docs_mod.writable_docs(root, policy)
    if not writable:
        lines.append("\nno writable docs in this repo (choobi writes README, docs/**, *-plan.md).")
        return "\n".join(lines)
    width = max(len(p) for p in writable)
    lines.append("\nwrite scope — the docs choobi may change:")
    for p in writable:
        covers = docs_mod._covers_globs((root / p).read_text(errors="replace"))
        suffix = f"   covers: {', '.join(covers)}" if covers else ""
        marker = " " if scope.covers(p) else "!"
        lines.append(f"  {marker} {p.ljust(width)}{suffix}".rstrip())
    if any(not scope.covers(p) for p in writable):
        lines.append("  (! writable but out of review scope: choobi will never select it)")
    return "\n".join(lines)


def render_changelog(records: List[Dict[str, Any]], scope_label: str) -> str:
    if not records:
        return f"no choobi activity yet {scope_label}."
    lines = [f"choobi changelog {scope_label} (newest first):"]
    for r in records:
        glyph = _GLYPH.get(r["status"], "?")
        what = r["summary"] or (r["reason"] if r["status"] != "no_op" else "stayed silent")
        lines.append(f"  #{r['id']:<4} {glyph}  {_when(r['ts'])}  {what}")
    lines.append("\nrun `choobi show <id>` for the full patch.")
    return "\n".join(lines)


def render_record(r: Optional[Dict[str, Any]]) -> str:
    if r is None:
        return "no such changelog entry."
    out = [f"#{r['id']}  {r['status']}   {_when(r['ts'])}",
           f"trigger: {r['trigger']}   duration: {r['duration_ms']}ms"]
    if r["source_commit"]:
        line = f"source: {r['source_commit'][:7]}"
        if r["docs_commit"]:
            line += f"  ->  docs: {r['docs_commit'][:7]}"
        out.append(line)
    import json
    changed = json.loads(r["docs_changed"])
    if changed:
        out.append("docs changed: " + ", ".join(changed))
    if r["summary"]:
        out.append("summary: " + r["summary"])
    if r["reason"]:
        out.append("reason: " + r["reason"])
    if r["patch"]:
        out.append("\n--- patch ---\n" + r["patch"].rstrip("\n"))
    return "\n".join(out)


def render_style() -> str:
    personal = config.personal_style_path()
    active = personal.exists() and personal.read_text().strip()
    header = (f"# resolved style — customized copy ({personal})"
              if active else "# resolved style — bundled default")
    return header + "\n\n" + baseline.resolved_style()
