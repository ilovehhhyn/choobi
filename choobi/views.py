"""CLI presentation for the read-only browsing commands (docs, changelog, show, style).

These render the SAME data the UI panels read (history, docs, baseline) — the CLI and the
window share the data layer and differ only in presentation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import baseline, config, docs as docs_mod

_GLYPH = {"committed": "✓", "no_op": "·", "failed": "✕"}


def _when(ts: str) -> str:
    return ts[:16].replace("T", " ")


def render_docs(root: Path) -> str:
    policy = baseline.policy()
    paths = docs_mod.writable_docs(root, policy)
    if not paths:
        return "no writable docs in this repo (choobi writes README, docs/**, *-plan.md)."
    width = max(len(p) for p in paths)
    lines = ["docs choobi can update in this repo:"]
    for p in paths:
        text = (root / p).read_text(errors="replace")
        covers = docs_mod._covers_globs(text)
        suffix = f"   covers: {', '.join(covers)}" if covers else ""
        lines.append(f"  {p.ljust(width)}{suffix}".rstrip())
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
    header = (f"# resolved style — personal override ({personal})"
              if active else "# resolved style — baseline (no personal override yet)")
    return header + "\n\n" + baseline.resolved_style()
