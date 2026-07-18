"""Documentation surface: which files are writable, and which docs a diff touches.

v1 linkage is deliberately cheap and deterministic (build-plan §5.2): allowlist globs,
`covers:` front matter, README directory ownership, and literal path mentions. No
embeddings, no model call — that comes later in the engine.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import gitio
from .errors import AmbiguousTarget, TargetNotFound


def _glob_to_re(pattern: str) -> "re.Pattern[str]":
    """Compile a glob to regex where `**` crosses `/` and `*` does not."""
    out = ["(?s:"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append(r")\Z")
    return re.compile("".join(out))


def is_allowed(rel_path: str, policy: Dict[str, Any]) -> bool:
    return any(_glob_to_re(p).match(rel_path) for p in policy["allowlist"])


def writable_docs(root: Path, policy: Dict[str, Any]) -> List[str]:
    """Tracked files inside the writable allowlist, sorted."""
    return sorted(f for f in gitio.tracked_files(root) if is_allowed(f, policy))


def _front_matter(text: str) -> Optional[Dict[str, Any]]:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _covers_globs(text: str) -> List[str]:
    fm = _front_matter(text)
    if not fm or "covers" not in fm:
        return []
    covers = fm["covers"]
    return [covers] if isinstance(covers, str) else [str(c) for c in covers]


def candidate_docs(root: Path, changed: List[str], policy: Dict[str, Any]) -> List[str]:
    """Existing writable docs plausibly related to the changed files."""
    docs = writable_docs(root, policy)
    changed_set = set(changed)
    hits: List[str] = []
    for doc in docs:
        if doc in changed_set:
            continue  # a doc editing itself is not evidence about code
        text = (root / doc).read_text(errors="replace")
        matched = False
        # 1. covers: front matter
        for glob in _covers_globs(text):
            rx = _glob_to_re(glob)
            if any(rx.match(c) for c in changed):
                matched = True
                break
        # 2. README owns the files DIRECTLY in its directory (non-recursive, so a root
        #    README does not become a candidate for every change in the tree).
        if not matched and Path(doc).name.lower() == "readme.md":
            owner_dir = str(Path(doc).parent)
            if any(str(Path(c).parent) == owner_dir for c in changed):
                matched = True
        # 3. literal path mention in the body
        if not matched and any(c in text for c in changed):
            matched = True
        if matched:
            hits.append(doc)
    return hits


_CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".cs", ".swift", ".kt", ".php", ".scala", ".sh", ".m", ".mm",
}


def is_source(path: str) -> bool:
    """A source file worth documenting: has a code extension and is not a test."""
    lower = path.lower()
    name = Path(lower).name
    if lower.startswith("tests/") or "/tests/" in lower or name.startswith("test_") \
            or "_test." in lower or ".test." in lower:
        return False
    return Path(lower).suffix in _CODE_EXTS


def documentable_surface(root: Path, files: "set[str]", policy: Dict[str, Any]) -> List[str]:
    """Of `files` (newly added or drifted since the snapshot), the source files no doc owns.

    A file is "owned" when candidate_docs finds any existing doc for it (via covers, README
    ownership, or a path mention). The unowned source files are the recall gap: new code that
    would otherwise never reach the model. This is the reverse of the coverage index.
    """
    return [f for f in sorted(files) if is_source(f) and not candidate_docs(root, [f], policy)]


def first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def doc_index(root: Path, doc_paths: List[str]) -> str:
    """Compact one-line-per-doc index (path, first heading, covers) for the linkage step."""
    lines = []
    for d in doc_paths:
        text = (root / d).read_text(errors="replace")
        covers = _covers_globs(text)
        lines.append(f"- {d} | {first_heading(text)} | covers: {', '.join(covers) or 'none'}")
    return "\n".join(lines)


def merge_covers(content: str, new_globs: List[str]) -> str:
    """Add `new_globs` to a doc's `covers:` front matter so future linkage finds it (step F).

    Preserves other front-matter keys and the body. Adds a front-matter block if absent.
    """
    if not new_globs:
        return content
    fm: Dict[str, Any] = {}
    body = content
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(content[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = content[end + 4:].lstrip("\n")
    if not isinstance(fm, dict):
        fm = {}
    existing = fm.get("covers", [])
    if isinstance(existing, str):
        existing = [existing]
    merged = list(dict.fromkeys([*existing, *new_globs]))  # dedup, preserve order
    if merged == existing:
        return content
    fm["covers"] = merged
    front = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{front}\n---\n{body}"


def resolve_target(root: Path, name: str, policy: Dict[str, Any]) -> str:
    """Resolve a user-supplied target to a repo-relative path.

    Exact allowed path wins. Otherwise fuzzy-match against writable docs; zero hits is
    target_not_found, more than one is ambiguous_target. A non-existent but allowed path
    is returned as-is (a create candidate).
    """
    name = name.strip()
    if is_allowed(name, policy) and (root / name).exists():
        return name
    docs = writable_docs(root, policy)
    needle = name.lower()
    matches = [d for d in docs if needle in d.lower() or needle in Path(d).name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousTarget(f"'{name}' matches: {', '.join(matches)}")
    if is_allowed(name, policy):
        return name  # allowed create candidate
    raise TargetNotFound(f"no writable doc matches '{name}'")
