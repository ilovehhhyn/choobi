"""The write boundary (build-plan §5.6). Every check here must pass before choobi writes.

If any check fails the whole patch is rejected — choobi never drops a bad claim and
commits the rest.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from . import docs, gitio
from .errors import Conflict, NotAllowedPath, VerificationFailed

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_SKIP_LINK = ("http://", "https://", "mailto:", "#", "<")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


def _headings(text: str) -> "list[str]":
    """ATX headings, skipping fenced code blocks so a `# comment` in code isn't counted."""
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                out.append(m.group(1).strip())
    return out


def _scan_secrets(content: str, policy: Dict[str, Any]) -> None:
    for pat in policy.get("secret_patterns", []):
        if re.search(pat, content):
            raise VerificationFailed(f"secret-shaped content matched /{pat}/")


def _check_links(root: Path, target: str, content: str) -> None:
    doc_dir = (root / target).parent
    for raw in _LINK_RE.findall(content):
        link = raw.strip()
        if not link or link.startswith(_SKIP_LINK):
            continue
        path_part = link.split("#", 1)[0].split(" ", 1)[0]
        if not path_part:
            continue
        candidate = (doc_dir / path_part) if not path_part.startswith("/") else (root / path_part.lstrip("/"))
        if not candidate.exists():
            raise VerificationFailed(f"broken link in {target}: {link}")


def check_write(
    root: Path,
    target: str,
    content: str,
    *,
    is_create: bool,
    expected_hash: Optional[str],
    policy: Dict[str, Any],
) -> None:
    """Raise a typed error if writing `content` to `target` would be unsafe."""
    if not docs.is_allowed(target, policy):
        raise NotAllowedPath(f"{target} is outside the documentation allowlist")

    _scan_secrets(content, policy)

    if is_create:
        if (root / target).exists():
            raise Conflict(f"{target} already exists; refusing to create over it")
    else:
        current = gitio.file_hash(root, target)
        if current is None:
            raise Conflict(f"{target} vanished before write")
        if expected_hash is not None and current != expected_hash:
            raise Conflict(f"{target} changed since choobi read it")
        # Surgical guard: an update may rename or remove at most ONE section (e.g. a signature
        # in a heading changed). Dropping several signals a wholesale rewrite (build-plan §5.5).
        old_headings = _headings((root / target).read_text(errors="replace"))
        new_headings = set(_headings(content))
        dropped = [h for h in old_headings if h not in new_headings]
        if len(dropped) > 1:
            raise VerificationFailed(f"{target}: update would drop {len(dropped)} sections: "
                                     f"{', '.join(dropped)}")

    _check_links(root, target, content)

    if gitio.has_operation_in_progress(root):
        raise Conflict("a merge/rebase/cherry-pick is in progress")
