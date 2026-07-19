"""Documentation surface: which files are writable, and which docs a diff touches.

v1 linkage is deliberately cheap and deterministic (build-plan §5.2): allowlist globs,
`covers:` front matter, README directory ownership, and literal path mentions. No
embeddings, no model call — that comes later in the engine.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import gitio
from .errors import AmbiguousTarget, Conflict, NotAllowedPath, TargetNotFound, VerificationFailed


def checked_path(root: Path, rel_path: str) -> Path:
    """Return a repository-contained path with no symlink in its relative path."""
    candidate = root / rel_path
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise NotAllowedPath(f"{rel_path} resolves outside the repository")
    cursor = root
    for part in Path(rel_path).parts:
        cursor /= part
        if cursor.is_symlink():
            raise NotAllowedPath(f"{rel_path} resolves through a symlink")
    return candidate


def read_snapshot(root: Path, rel_path: str) -> Tuple[str, str]:
    """Read text and its SHA-256 from one regular-file descriptor."""
    checked_path(root, rel_path)
    parts = Path(rel_path).parts
    if not parts:
        raise NotAllowedPath("documentation path is empty")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise VerificationFailed("platform cannot enforce symlink-safe repository reads")

    parent_fd = os.open(root.resolve(), os.O_RDONLY | directory | nofollow)
    file_fd = -1
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise NotAllowedPath(f"{rel_path} is not a regular repository file")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = -1
            data = stream.read()
            after = os.fstat(stream.fileno())
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise Conflict(f"{rel_path} changed while Choobi read it")
        return data.decode(errors="replace"), hashlib.sha256(data).hexdigest()
    except FileNotFoundError as exc:
        raise TargetNotFound(f"{rel_path} no longer exists") from exc
    except NotADirectoryError as exc:
        raise NotAllowedPath(f"{rel_path} resolves through a non-directory") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise NotAllowedPath(f"{rel_path} resolves through a symlink") from exc
        raise VerificationFailed(f"could not read repository file {rel_path}: {exc}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


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
    path = Path(rel_path)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != rel_path:
        return False
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


def _covers_globs(text: str, *, strict: bool = False) -> List[str]:
    fm = _front_matter(text)
    if text.startswith("---\n") and fm is None:
        if strict:
            raise VerificationFailed("documentation front matter must be a YAML mapping")
        return []
    if not fm or "covers" not in fm:
        return []
    covers = fm["covers"]
    if isinstance(covers, str):
        return [covers]
    if isinstance(covers, list) and all(isinstance(value, str) for value in covers):
        return covers
    if strict:
        raise VerificationFailed("covers must be a path string or list of path strings")
    return []


def candidate_docs(root: Path, changed: List[str], policy: Dict[str, Any]) -> List[str]:
    """Existing writable docs plausibly related to the changed files."""
    docs = writable_docs(root, policy)
    hits: List[str] = []
    for doc in docs:
        related = [path for path in changed if path != doc]
        if not related:
            continue  # a doc editing itself is not evidence about code
        text = checked_path(root, doc).read_text(errors="replace")
        matched = False
        # 1. covers: front matter
        for glob in _covers_globs(text):
            rx = _glob_to_re(glob)
            if any(rx.match(c) for c in related):
                matched = True
                break
        # 2. README owns the files DIRECTLY in its directory (non-recursive, so a root
        #    README does not become a candidate for every change in the tree).
        if not matched and Path(doc).name.lower() == "readme.md":
            owner_dir = str(Path(doc).parent)
            if any(str(Path(c).parent) == owner_dir for c in related):
                matched = True
        # 3. literal path mention in the body
        if not matched and any(c in text for c in related):
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
        text = checked_path(root, d).read_text(errors="replace")
        covers = _covers_globs(text)
        lines.append(f"- {d} | {first_heading(text)} | covers: {', '.join(covers) or 'none'}")
    return "\n".join(lines)


def merge_covers(
    original: str, content: str, new_globs: List[str], tracked_paths: List[str],
) -> str:
    """Add `new_globs` to a doc's `covers:` front matter so future linkage finds it (step F).

    Preserves other front-matter keys and the body. Adds a front-matter block if absent.
    """
    old_fm = _front_matter(original)
    new_fm = _front_matter(content)
    if original.startswith("---\n") and old_fm is None:
        raise VerificationFailed("existing documentation front matter is invalid")
    if content.startswith("---\n") and new_fm is None:
        raise VerificationFailed("documentation front matter must be a YAML mapping")
    old_meta = {key: value for key, value in (old_fm or {}).items() if key != "covers"}
    new_meta = {key: value for key, value in (new_fm or {}).items() if key != "covers"}
    if original and old_meta != new_meta:
        raise VerificationFailed("an update must preserve existing front matter metadata")

    old_covers = _covers_globs(original, strict=True)
    proposed = _covers_globs(content, strict=True)
    live_covers = [
        pattern for pattern in old_covers
        if any(_glob_to_re(pattern).match(path) for path in tracked_paths)
    ]
    if not set(live_covers) <= set(proposed):
        raise VerificationFailed("an update must preserve existing live covers entries")
    if not set(proposed) <= set(old_covers) | set(new_globs):
        raise VerificationFailed("model output added an unverified covers entry")
    merged = list(dict.fromkeys([*live_covers, *new_globs]))
    if proposed == merged:
        return content

    fm: Dict[str, Any] = dict(new_fm or {})
    body = content
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        body = content[end + 4:].lstrip("\n")
    if merged:
        fm["covers"] = merged
    else:
        fm.pop("covers", None)
    if not fm:
        return body
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
