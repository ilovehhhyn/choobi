"""The write boundary (build-plan §5.6). Every check here must pass before choobi writes.

If any check fails the whole patch is rejected — choobi never drops a bad claim and
commits the rest.

Two layers:

- `check_content` judges the proposed text against a `Tree` (allowlist, encoding, secrets,
  covers, section preservation, links, created examples). It is pure with respect to the
  working tree, so anchored runs can validate against the committed revision they read from,
  and a failure here is something the model can be asked to fix.
- `check_tree_state` judges the live checkout (target clean, hash unchanged, no git operation
  in progress). A failure here is never the model's fault; the commit writer turns it into a
  parked commit instead of a failed run.

`check_write` composes both against the working tree for callers that still want one gate.
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
    """Reject secret-shaped prompt inputs or model-authored summaries."""
    for chunk in chunks:
        _scan_secrets(chunk, policy)


def _check_covers(target: str, content: str, tree: docs.Tree) -> None:
    tracked = tree.files()
    for pattern in docs._covers_globs(content, strict=True):
        if not any(docs._glob_to_re(pattern).match(path) for path in tracked):
            raise VerificationFailed(f"unresolved covers entry in {target}: {pattern}")


def _check_links(root: Path, target: str, content: str, tree: docs.Tree) -> None:
    doc_dir = Path(target).parent
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
        if path_part.startswith("/"):
            candidate = root / path_part.lstrip("/")
        else:
            candidate = root / doc_dir / path_part
        resolved = candidate.resolve(strict=False)
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise VerificationFailed(f"link escapes the repository in {target}: {link}")
        rel = resolved.relative_to(resolved_root).as_posix() if resolved != resolved_root else ""
        if not tree.exists(rel):
            raise VerificationFailed(f"broken link in {target}: {link}")


def _check_create_examples(target: str, content: str, evidence: str) -> None:
    for block in re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL):
        example = block.strip()
        if example and example not in evidence:
            raise VerificationFailed(f"{target}: created example is not present in the evidence")


def check_content(
    root: Path,
    target: str,
    content: str,
    *,
    is_create: bool,
    old_content: Optional[str],
    policy: Dict[str, Any],
    tree: docs.Tree,
    evidence: str = "",
) -> None:
    """Raise a typed error if `content` is unsafe or unfaithful as the new text of `target`.

    `old_content` is the document text the draft was produced from (None for a create).
    Every failure here is a property of the text, so the engine may feed it back to the model.
    """
    if not docs.is_allowed(target, policy):
        raise NotAllowedPath(f"{target} is outside the documentation allowlist")
    docs.checked_path(root, target)

    try:
        content.encode("utf-8")
    except UnicodeError as exc:
        raise VerificationFailed(f"{target} is not valid UTF-8 text") from exc
    _scan_secrets(content, policy)
    _check_covers(target, content, tree)

    if is_create:
        _check_create_examples(target, content, evidence)
    else:
        # Surgical guard: an update may rename or remove at most ONE section (e.g. a signature
        # in a heading changed). Dropping several signals a wholesale rewrite (build-plan §5.5).
        old_headings = _headings(old_content or "")
        new_headings = set(_headings(content))
        dropped = [h for h in old_headings if h not in new_headings]
        if len(dropped) > 1:
            raise VerificationFailed(f"{target}: update would drop {len(dropped)} sections: "
                                     f"{', '.join(dropped)}")

    _check_links(root, target, content, tree)


def check_tree_state(
    root: Path,
    target: str,
    *,
    is_create: bool,
    expected_hash: Optional[str],
) -> None:
    """Raise `Conflict` if the live checkout is not in the state the draft assumed."""
    if not gitio.working_tree_clean(root, [target]):
        raise Conflict(f"{target} has staged or unstaged changes")
    if is_create:
        if (root / target).exists():
            raise Conflict(f"{target} already exists; refusing to create over it")
    else:
        _, current = docs.read_snapshot(root, target)
        if expected_hash is not None and current != expected_hash:
            raise Conflict(f"{target} changed since choobi read it")
    if gitio.has_operation_in_progress(root):
        raise Conflict("a merge/rebase/cherry-pick is in progress")


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
    """Raise a typed error if writing `content` to `target` into the working tree is unsafe."""
    tree = docs.Tree.working(root)
    if not docs.is_allowed(target, policy):
        raise NotAllowedPath(f"{target} is outside the documentation allowlist")
    docs.checked_path(root, target)
    try:
        content.encode("utf-8")
    except UnicodeError as exc:
        raise VerificationFailed(f"{target} is not valid UTF-8 text") from exc
    _scan_secrets(content, policy)
    _check_covers(target, content, tree)
    if not gitio.working_tree_clean(root, [target]):
        raise Conflict(f"{target} has staged or unstaged changes")
    old_content: Optional[str] = None
    if is_create:
        if (root / target).exists():
            raise Conflict(f"{target} already exists; refusing to create over it")
    else:
        old_content, current = docs.read_snapshot(root, target)
        if expected_hash is not None and current != expected_hash:
            raise Conflict(f"{target} changed since choobi read it")
    check_content(root, target, content, is_create=is_create, old_content=old_content,
                  policy=policy, tree=tree, evidence=evidence)
    if gitio.has_operation_in_progress(root):
        raise Conflict("a merge/rebase/cherry-pick is in progress")
