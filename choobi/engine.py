"""The one engine verb: `update`. Every entry point composes this contract.

The flow is linear and synchronous (build-plan §3.1): collect scope -> deterministic
relevance gate -> build context -> one model call -> verify -> commit -> record. No
background threads, no batching, no cross-commit coalescing.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import baseline, commitwriter, config, docs, gitio, history, repos, verify
from .errors import (
    ChoobiError,
    DocumentationGap,
    RuntimeOutputInvalid,
    SourceCommitRequired,
)
from .runtime import Runtime

SYSTEM_PROMPT = (
    "You are choobi, a documentation agent. Keep engineering docs consistent with the work.\n"
    "- If the change alters behavior, an API, a CLI, config, or a workflow that a candidate "
    "document covers, UPDATE that document. Make the smallest edit that captures what changed; "
    "never rewrite a document to change one fact, and never drop existing sections.\n"
    "- If the change adds new code that exposes a public feature, API, function, class, CLI, "
    "or workflow with no owning document, and creating is allowed, CREATE one document for it "
    "in the right category. Prefer creating over silence when the new surface is user-facing.\n"
    "- Stay silent ONLY for changes with no documented-behavior impact: internal refactors, "
    "local renames, formatting, tests, generated files, and trivial internal plumbing.\n"
    "Respond with ONE JSON object and nothing else."
)

LINKAGE_SYSTEM = (
    "You are choobi's linkage step. Given a code change and a list of documents, decide which "
    "ONE document should own the change, or whether a new one should be created, or neither. "
    "Respond with ONE JSON object and nothing else."
)


@dataclass
class UpdateRequest:
    targets: List[str] = field(default_factory=list)
    source_commit: Optional[str] = None
    rev_range: Optional[str] = None
    use_staged: bool = False
    use_working: bool = False
    detached: bool = False
    instruction: Optional[str] = None
    chat_context: Optional[str] = None
    trigger: str = "manual"


@dataclass
class UpdateResult:
    status: str                       # committed | no_op | gap
    summary: str = ""
    completion_message: str = ""
    docs_commit: Optional[str] = None
    docs_changed: List[str] = field(default_factory=list)
    reason: str = ""


def _repo_identity(root: Path) -> "tuple[str, str]":
    return config.checkout_id(gitio.common_dir(root)), str(root)


def _collect_diff(root: Path, req: UpdateRequest) -> "tuple[str, List[str]]":
    if req.rev_range:
        return gitio.diff(root, req.rev_range), gitio.changed_files(root, req.rev_range)
    if req.use_staged:
        return gitio.working_diff(root, True), gitio.working_changed(root, True)
    if req.use_working:
        return gitio.working_diff(root, False), gitio.working_changed(root, False)
    return "", []


def _authoring_message(root: Path, req: UpdateRequest, summary: str) -> str:
    if req.source_commit:
        return gitio.commit_message(root, req.source_commit)
    if req.detached:
        return f"docs: {summary}" if summary else "docs: update"
    raise SourceCommitRequired("supply --commit/--range/--pr, or use --detached")


def _build_prompt(
    req: UpdateRequest, diff_text: str, candidates: Dict[str, str], policy: Dict,
    sop_body: str = "", surface: "List[str]" = None,
) -> str:
    parts: List[str] = []
    parts.append("## Task\nDecide whether any candidate document needs to change, and if so, "
                 "produce its full updated content.\n")
    if req.instruction:
        parts.append(f"## Explicit instruction\n{req.instruction}\n")
    parts.append("## Style guide\n" + baseline.resolved_style() + "\n")
    if sop_body:
        parts.append("## Repository SOP (this repo's documentation preferences)\n" + sop_body + "\n")
    if diff_text.strip():
        parts.append("## Code diff\n```diff\n" + diff_text[:20000] + "\n```\n")
    if req.chat_context:
        parts.append("## Conversation context\n" + req.chat_context[:8000] + "\n")
    parts.append("## Candidate documents (you may change AT MOST ONE)\n")
    for path, content in candidates.items():
        parts.append(f"### {path}\n```markdown\n{content}\n```\n")
    if not candidates:
        parts.append("(No existing document is linked to this change.)\n")
    if surface:
        parts.append(
            "## New code with no owning document\n"
            "These source files are new and no existing document covers them. If any exposes a "
            "public feature, API, function, class, or CLI, CREATE one new document for it in the "
            "right category (public features, public reference, internal plans, internal "
            "features) at a path under docs/. Only stay silent if the new code is internal "
            "plumbing, trivial, or not user-facing.\n"
            + "\n".join(f"- {f}" for f in surface) + "\n"
        )
    parts.append(
        "## Response format\n"
        "Return ONE JSON object:\n"
        '{"disposition":"update|create|silent",'
        '"target":"<repo-relative path of the one doc>",'
        '"summary":"<one sentence, e.g. documented the new retry behavior in docs/api.md>",'
        '"content":"<the FULL updated file content>"}\n'
        "Use \"silent\" (omit target/content) when no documented behavior changed. "
        "Only choose a target from the candidates listed above."
    )
    return "\n".join(parts)


def _extract_json(raw: str) -> Dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeOutputInvalid(f"could not parse JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _parse_disposition(raw: str) -> Dict:
    data = _extract_json(raw)
    if data.get("disposition") not in {"update", "create", "silent"}:
        raise RuntimeOutputInvalid("disposition missing or invalid")
    return data


def _llm_linkage(root: Path, diff_text: str, doc_paths: List[str], runtime: Runtime):
    """Step 2: nothing linked the cheap way, so ask the model once which doc should own the
    change. Returns ("doc", path) | ("create", None) | ("none", None)."""
    prompt = (
        "A code change was made but no document is obviously linked to it.\n\n"
        f"## Code diff\n```diff\n{diff_text[:12000]}\n```\n\n"
        f"## Documents\n{docs.doc_index(root, doc_paths)}\n\n"
        "## Response format\nReturn ONE JSON object: {\"doc\":\"<repo-relative path>\"} to "
        "update that document, {\"create\":true} to create a new one, or {\"none\":true} if "
        "nothing needs documenting."
    )
    data = _extract_json(runtime.complete(prompt, LINKAGE_SYSTEM))
    doc = str(data.get("doc", "")).strip()
    if doc in doc_paths:
        return ("doc", doc)
    if data.get("create"):
        return ("create", None)
    return ("none", None)


def _unified(old: str, new: str, target: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{target}",
            tofile=f"b/{target}",
        )
    )


def run_update(root: Path, req: UpdateRequest, cfg: config.Config, runtime: Runtime) -> UpdateResult:
    started = time.monotonic()
    repo_id, repo_path = _repo_identity(root)
    head = gitio.resolve(root, "HEAD")

    # Idempotency: an automatic run for an already-handled source commit is a no-op.
    if req.trigger == "post_commit" and req.source_commit:
        prior = history.find_by_source(repo_id, req.source_commit)
        if prior:
            return UpdateResult(status=prior["status"], summary=prior["summary"],
                                docs_commit=prior["docs_commit"],
                                docs_changed=json.loads(prior["docs_changed"]))

    policy = baseline.policy()
    diff_text, changed = _collect_diff(root, req)
    creation_allowed = (repo_id in cfg.create_enabled_repos
                        or repos.sop_allows_create(repo_id, repo_path))

    # Resolve the documents in scope, and (in inference mode) find new source that no doc owns.
    surface: List[str] = []
    if req.targets:
        resolved = [docs.resolve_target(root, t, policy) for t in req.targets]
    else:
        resolved = docs.candidate_docs(root, changed, policy)
        # Recall backbone: new source files, added in this commit or drifted since the last
        # snapshot, that no doc owns. The snapshot makes this robust to commits we missed.
        prior = repos.load_snapshot(repo_id)
        current_source = [f for f in gitio.tracked_files(root) if docs.is_source(f)]
        added = gitio.added_files(root, req.rev_range) if req.rev_range else []
        drift = (set(current_source) - prior) if prior is not None else set()
        if creation_allowed:
            surface = docs.documentable_surface(root, set(added) | drift, policy)
        repos.save_snapshot(repo_id, current_source, head)  # advance baseline; surface captured

        # Step 2: semantic linkage fallback. Nothing found the cheap way but source changed,
        # so ask the model once which existing doc (if any) should own it.
        if not resolved and not surface:
            changed_src = [f for f in changed if docs.is_source(f)]
            all_docs = docs.writable_docs(root, policy)
            if changed_src and all_docs:
                kind, doc = _llm_linkage(root, diff_text, all_docs, runtime)
                if kind == "doc":
                    resolved = [doc]
                elif kind == "create" and creation_allowed:
                    surface = changed_src

    # Deterministic relevance gate: nothing linked and nothing new, no model call (§5.4).
    if not resolved and not surface and not req.instruction:
        history.add_record(repo_id, repo_path, req.trigger, "no_op",
                           source_commit=req.source_commit, head_commit=head,
                           summary="", reason="no_candidate_docs")
        _advance_checkpoint(root, req, repo_id, repo_path)
        return UpdateResult(status="no_op", reason="no_candidate_docs")
    if not resolved and not surface:
        raise DocumentationGap("instruction given but no target or candidate document")

    # Snapshot current content + hashes for the concurrent-edit guard.
    contents = {p: (root / p).read_text(errors="replace") if (root / p).exists() else ""
                for p in resolved}
    hashes = {p: gitio.file_hash(root, p) for p in resolved}

    sop_body = repos.sop_prompt_body(repo_id, repo_path)
    prompt = _build_prompt(req, diff_text, contents, policy, sop_body, surface)
    disp = _parse_disposition(runtime.complete(prompt, SYSTEM_PROMPT))

    if disp["disposition"] == "silent":
        history.add_record(repo_id, repo_path, req.trigger, "no_op",
                           source_commit=req.source_commit, head_commit=head,
                           summary="", reason="model_silent")
        _advance_checkpoint(root, req, repo_id, repo_path)
        return UpdateResult(status="no_op", reason="model_silent")

    target = str(disp.get("target", "")).strip()
    content = str(disp.get("content", ""))
    summary = str(disp.get("summary", "")).strip()
    if not target or not content:
        raise RuntimeOutputInvalid("update/create requires target and content")

    is_create = disp["disposition"] == "create"
    if is_create:
        allowed = repo_id in cfg.create_enabled_repos or repos.sop_allows_create(repo_id, repo_path)
        if not allowed:
            history.add_record(repo_id, repo_path, req.trigger, "failed",
                               source_commit=req.source_commit, head_commit=head,
                               summary=summary, reason="documentation_gap")
            return UpdateResult(status="gap", summary=summary, reason="documentation_gap")
        expected_hash = None
    else:
        if target not in resolved:
            raise RuntimeOutputInvalid(f"model chose off-scope target: {target}")
        expected_hash = hashes.get(target)

    # Step 3: record the code this doc now covers, so future linkage finds it deterministically
    # (and the expensive linkage pass isn't needed next time).
    relevant_src = sorted({f for f in changed if docs.is_source(f)} | set(surface))
    content = docs.merge_covers(content, relevant_src)

    # Write boundary. Any failure aborts the whole patch before any write.
    verify.check_write(root, target, content, is_create=is_create,
                       expected_hash=expected_hash, policy=policy)

    message = _authoring_message(root, req, summary)
    docs_commit = commitwriter.write_and_commit(root, {target: content}, message)

    patch = _unified(contents.get(target, ""), content, target)
    duration_ms = int((time.monotonic() - started) * 1000)
    history.add_record(repo_id, repo_path, req.trigger, "committed",
                       source_commit=req.source_commit, head_commit=head,
                       docs_commit=docs_commit, duration_ms=duration_ms,
                       docs_changed=[target], summary=summary, patch=patch)
    _advance_checkpoint(root, req, repo_id, repo_path)

    completion = f"choobi just updated the docs — {summary.rstrip('.')}." if summary else \
        "choobi just updated the docs."
    return UpdateResult(status="committed", summary=summary, completion_message=completion,
                        docs_commit=docs_commit, docs_changed=[target])


def _advance_checkpoint(root: Path, req: UpdateRequest, repo_id: str, repo_path: str) -> None:
    sha = req.source_commit or gitio.resolve(root, "HEAD")
    try:
        subject = gitio.commit_subject(root, sha)
    except RuntimeError:
        subject = ""
    history.set_checkpoint(repo_id, repo_path, sha, subject)


def run_update_guarded(root: Path, req: UpdateRequest, cfg: config.Config, runtime: Runtime) -> UpdateResult:
    """run_update, but a typed failure is recorded and re-raised (single place to log)."""
    try:
        return run_update(root, req, cfg, runtime)
    except ChoobiError as exc:
        repo_id, repo_path = _repo_identity(root)
        history.add_record(repo_id, repo_path, req.trigger, "failed",
                           source_commit=req.source_commit, summary="", reason=exc.reason)
        raise
