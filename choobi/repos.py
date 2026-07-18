"""Per-repo instructions: the editable SOP and the generated knowledge base.

Both are Markdown files under ~/.choobi/repos/<checkout-id>/. The SOP is human-authored
preferences that choobi *acts on* (its prose feeds the engine; its `allow_create` frontmatter
gates new-document creation). The knowledge base is choobi's own derived map of the repo; it
is generated but the user can edit and save it.

The knowledge traversal is one deterministic pass, no model call:

    tracked = git ls-files                      # git already flattened the tree for us
    docs, code = partition(tracked, is_allowed) # docs = inside the writable allowlist
    for each doc: category(path) + covers(front-matter)
    coverage = code files matched by any doc's covers glob

That is the whole of it: split, categorize, and match. No recursion (git listed every
tracked file), no heuristics beyond path category and the explicit `covers:` linkage.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from . import baseline, config, docs, gitio, history

# Doc categories, in display order. Each is (label, path-predicate). First match wins.
_CATEGORIES: List[Tuple[str, Any]] = [
    ("README", lambda p: Path(p).name.lower() == "readme.md"),
    ("public: features and user journeys", lambda p: p.startswith("docs/public/features/")),
    ("public: CLI / SDK reference", lambda p: p.startswith("docs/public/reference/")),
    ("internal: plans", lambda p: p.startswith("docs/internal/plans/")
        or Path(p).name == "build-plan.md" or p.endswith("-plan.md")),
    ("internal: feature explanations", lambda p: p.startswith("docs/internal/features/")),
    ("other docs", lambda p: True),
]


def _category(path: str) -> str:
    for label, match in _CATEGORIES:
        if match(path):
            return label
    return "other docs"


def _top_dir(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def sop_path(repo_id: str) -> Path:
    return config.repo_dir(repo_id) / "sop.md"


def knowledge_path(repo_id: str) -> Path:
    return config.repo_dir(repo_id) / "knowledge.md"


def snapshot_path(repo_id: str) -> Path:
    return config.repo_dir(repo_id) / "snapshot.json"


def load_snapshot(repo_id: str) -> Optional[Set[str]]:
    """The set of source files choobi last reconciled, or None if it never has (baseline)."""
    p = snapshot_path(repo_id)
    if not p.exists():
        return None
    try:
        return set(json.loads(p.read_text()).get("code", []))
    except (json.JSONDecodeError, OSError):
        return None


def save_snapshot(repo_id: str, code_files: List[str], head: str) -> None:
    p = snapshot_path(repo_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reconciled_head": head, "code": sorted(code_files)}, indent=2))


_SOP_TEMPLATE = """\
---
# choobi may CREATE new documents in this repo, not only update existing ones.
allow_create: true
---
# choobi SOP: {repo}

How choobi documents this repository. The global style guide still applies on top of this.
Fill in the sections below; any section you leave empty falls back to choobi's defaults.

## Creating vs updating

When a change introduces a new feature choobi identifies, or a big shift, choobi:

1. First looks for an existing doc that already owns this, across these categories:
   - public: features and user journeys (how a feature works, how users move through it)
   - public: CLI / SDK reference, the single source of truth for commands and APIs
   - internal: plans, such as build and implementation plans
   - internal: feature explanations for humans (how and why something works)
2. If a relevant doc exists, it updates that doc.
3. If none exists, it creates a new doc in the right category.

## Suggested layout (choobi may write into these)

- docs/public/features/     feature and user-journey docs
- docs/public/reference/    CLI / SDK single source of truth
- docs/internal/plans/      build and implementation plans
- docs/internal/features/   feature explanations for humans

## Repo-specific style
<!-- Voice, terminology, and formatting particular to this repo. Overrides the global style
     guide where they conflict. Leave empty to use the global style guide as-is. -->

## Terminology and naming
<!-- Preferred terms, banned terms, product and brand capitalization, acronyms to expand. -->

## Audience
<!-- Who reads these docs: external users, internal engineers, operators, or a mix. Say it
     per doc category if it differs. -->

## Ownership and review
<!-- Who owns which docs and where proposed changes should be routed. Keep in sync with
     CODEOWNERS where possible. -->

## Cadence and noise
<!-- How often choobi should act and how chatty to be. For example: document on every
     relevant commit, stay silent on internal-only changes, keep completion messages to one
     line, and batch trivial changes. -->

## Verification
<!-- Repo-specific checks: safe commands choobi may run to confirm examples, links or paths
     that must always resolve, and generated files it must never edit. -->

## Never document
<!-- Anything that must never appear in docs: secrets, internal-only endpoints, customer
     data, or unreleased plans. -->

## Notes
<!-- Anything else choobi should know about documenting this repo. -->

## Leave undocumented
- internal refactors, local renames, formatting, tests, generated files
"""


def default_sop(repo_path: str) -> str:
    return _SOP_TEMPLATE.format(repo=repo_path or "this repository")


def read_sop(repo_id: str, repo_path: str) -> Tuple[str, bool]:
    """Return (content, is_default). is_default means no personal SOP saved yet."""
    p = sop_path(repo_id)
    if p.exists() and p.read_text().strip():
        return p.read_text(), False
    return default_sop(repo_path), True


def save_sop(repo_id: str, content: str) -> None:
    p = sop_path(repo_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def reset_sop(repo_id: str) -> None:
    sop_path(repo_id).unlink(missing_ok=True)


def _split_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return (frontmatter dict, body) for a doc that may start with a --- block."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 4:].lstrip("\n")
            return (fm if isinstance(fm, dict) else {}), body
    return {}, text


def sop_allows_create(repo_id: str, repo_path: str) -> bool:
    content, _ = read_sop(repo_id, repo_path)
    fm, _ = _split_front_matter(content)
    return bool(fm.get("allow_create", False))


def sop_prompt_body(repo_id: str, repo_path: str) -> str:
    """The SOP prose (frontmatter stripped) for injection into the engine prompt."""
    content, _ = read_sop(repo_id, repo_path)
    _, body = _split_front_matter(content)
    return body.strip()


def generate_knowledge(repo_id: str, repo_path: str) -> str:
    """Build choobi's map of the repo in one pass over the git-tracked tree.

    Split tracked files into documents (inside the writable allowlist) and code, group each
    by category and location, and match every doc's `covers:` globs against the code so we
    can show what is documented and what is not. Deterministic, no model call.
    """
    root = Path(repo_path)
    when = datetime.now(timezone.utc).isoformat()[:16].replace("T", " ")
    if not root.exists():
        return f"# choobi knowledge: {repo_path}\n\n_repo path not found on disk; nothing to scan._\n"

    policy = baseline.policy()
    tracked = gitio.tracked_files(root)
    doc_paths = [f for f in tracked if docs.is_allowed(f, policy)]
    code_paths = [f for f in tracked if f not in set(doc_paths)]

    # Group docs by category, collecting each doc's covers globs.
    by_category: Dict[str, List[Tuple[str, List[str]]]] = defaultdict(list)
    all_covers: List[str] = []
    for d in doc_paths:
        covers = docs._covers_globs((root / d).read_text(errors="replace"))
        all_covers += covers
        by_category[_category(d)].append((d, covers))

    # A top-level code dir is "covered" if any covers glob matches a file under it.
    matchers = [docs._glob_to_re(g) for g in all_covers]
    covered_dirs = {_top_dir(f) for f in code_paths if any(m.match(f) for m in matchers)}
    dir_counts = Counter(_top_dir(f) for f in code_paths)

    out: List[str] = [
        f"# choobi knowledge: {repo_path}",
        f"_generated {when} UTC. {len(doc_paths)} docs, {len(code_paths)} code files._",
        "",
        "choobi built this map in one pass over the git-tracked tree: it split tracked files",
        "into documents and code, grouped each by category and location, and matched every",
        "doc's `covers:` globs against the code.",
        "",
        "## Documents",
    ]
    if not doc_paths:
        out.append("- (none matched the documentation allowlist yet)")
    for label, _pred in _CATEGORIES:
        items = by_category.get(label)
        if not items:
            continue
        out.append(f"### {label}")
        for d, covers in sorted(items):
            out.append(f"- `{d}`" + (f"  covers: {', '.join(covers)}" if covers else ""))
        out.append("")

    out.append("## Code map")
    for d, n in sorted(dir_counts.items()):
        gap = "" if (d in covered_dirs or d == "(root)") else "   [no doc covers this yet]"
        out.append(f"- `{d}/` {n} file{'s' if n != 1 else ''}{gap}")

    cp = history.get_checkpoint(repo_id)
    if cp and cp.get("last_source_commit"):
        out += ["", "## Last activity",
                f"- {cp['last_subject']} (`{cp['last_source_commit'][:7]}`)"]

    text = "\n".join(out) + "\n"
    p = knowledge_path(repo_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return text


def read_knowledge(repo_id: str, repo_path: str) -> str:
    p = knowledge_path(repo_id)
    if p.exists():
        return p.read_text()
    return generate_knowledge(repo_id, repo_path)


def save_knowledge(repo_id: str, content: str) -> None:
    """Persist a user-edited knowledge base (build-plan §7.3 user-pinned corrections)."""
    p = knowledge_path(repo_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
