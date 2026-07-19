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
_SKIP_LINK = ("http://", "https://", "mailto:", "#")
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


def check_evidence(policy: Dict[str, Any], *chunks: str) -> None:
    """Reject secret-shaped prompt inputs before any runtime call."""
    for chunk in chunks:
        _scan_secrets(chunk, policy)


def _check_covers(root: Path, target: str, content: str) -> None:
    tracked = gitio.tracked_files(root)
    for pattern in docs._covers_globs(content, strict=True):
        if not any(docs._glob_to_re(pattern).match(path) for path in tracked):
            raise VerificationFailed(f"unresolved covers entry in {target}: {pattern}")


def _check_links(root: Path, target: str, content: str) -> None:
    doc_dir = (root / target).parent
    resolved_root = root.resolve()
    for raw in _LINK_RE.findall(content):
        link = raw.strip()
        if link.startswith("<") and link.endswith(">"):
            link = link[1:-1].strip()
        if not link or link.startswith(_SKIP_LINK):
            continue
        path_part = link.split("#", 1)[0].split(" ", 1)[0]
        if not path_part:
            continue
        candidate = (doc_dir / path_part) if not path_part.startswith("/") else (root / path_part.lstrip("/"))
        resolved = candidate.resolve(strict=False)
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise VerificationFailed(f"link escapes the repository in {target}: {link}")
        if not candidate.exists():
            raise VerificationFailed(f"broken link in {target}: {link}")


def _check_create_examples(target: str, content: str, evidence: str) -> None:
    for block in re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL):
        example = block.strip()
        if example and example not in evidence:
            raise VerificationFailed(f"{target}: created example is not present in the evidence")


def check_write(
    root: Path,
    target: str,
    content: str,
    *,
    is_create: bool,
    expected_hash: Optional[str],
    policy: Dict[str, Any],
    evidence: str = "",
) -> None:
    """Raise a typed error if writing `content` to `target` would be unsafe."""
    if not docs.is_allowed(target, policy):
        raise NotAllowedPath(f"{target} is outside the documentation allowlist")
    destination = docs.checked_path(root, target)

    try:
        content.encode("utf-8")
    except UnicodeError as exc:
        raise VerificationFailed(f"{target} is not valid UTF-8 text") from exc
    _scan_secrets(content, policy)
    _check_covers(root, target, content)
    if not gitio.working_tree_clean(root, [target]):
        raise Conflict(f"{target} has staged or unstaged changes")

    if is_create:
        if (root / target).exists():
            raise Conflict(f"{target} already exists; refusing to create over it")
        _check_create_examples(target, content, evidence)
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
