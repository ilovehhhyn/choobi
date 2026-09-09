# Background Docs Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Choobi a background docs updater that appends a docs commit to the same branch and pushes it to the same PR, without ever racing, blocking, or surprising the developer.

**Architecture:** The post-commit hook stays. The engine reads all anchored evidence from git objects at a pinned revision (never the working tree), builds the docs commit in an isolated worktree off the *source branch tip*, and attaches it only when it cannot collide (same branch name, clean targets, no git operation in progress). Anything else is **parked** on `refs/choobi/pending/<source>` and landed later by `choobi apply`. Bursts of commits coalesce into one job. Model-fixable verification failures are fed back to the model and retried; runtime outages back off and retry; nothing is dropped silently. After a commit lands, Choobi pushes it only to a remote branch that already contains the source commit (fast-forward only). `choobi audit` gives a read-only stale-claim report for existing repositories.

**Tech Stack:** Python 3.9 stdlib + PyYAML, git CLI, stdlib `unittest` (run with `python -m pytest -q`).

**Spec:** the conversation with Helen on 2026-09-08 plus Astra's critique. Decisions locked:

1. Docs ride the **same branch and PR**. Never a separate branch/PR.
2. Background Choobi **only appends**, and only when it cannot collide. Otherwise it parks.
3. **Branch identity** check (symbolic ref name), not ancestor check.
4. Anchored evidence is read from **git objects**, never the working tree.
5. **Coalesce** bursts: N rapid commits → one docs commit covering the whole range.
6. **Retry with feedback** (3 drafts), runtime backoff, one re-run on stale conflict. After that: recorded failure with the last draft, never a silent drop.
7. **Auto-push** only where the developer already pushed (source commit reachable from `@{u}`), fast-forward only, never force, never a new branch.
8. `choobi pr create` **never blocks** on a running update.
9. Every silent outcome carries a **structured reason**.
10. `choobi audit` is **read-only** and summon-only.

## Global Constraints

- No new dependencies. Python >= 3.9.
- `MAX_PROMPT_BYTES = 100_000` stays the prompt ceiling.
- Writable allowlist in `choobi/baseline/policy.yaml` stays immutable; nothing here widens it.
- The docs commit carries `CHOOBI_GENERATING=1` so its own hook exits (unchanged).
- All 107 existing tests keep passing, adjusted only where the contract deliberately changed (documented per task).
- Every new behavior has a test against a throwaway git repo (`tempfile` + `git init`), FakeRuntime only.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `choobi/gitio.py` (modify) | New plumbing: `current_branch`, `ls_tree`, `show_blob`, `commits_between`, `upstream`, `push_fast_forward`, `pending_refs`. |
| `choobi/docs.py` (modify) | `Tree` abstraction: uniform read/exists/files over the working tree or a pinned revision. `tracked_documents(root, policy, tree=None)`. |
| `choobi/verify.py` (modify) | Split into `check_content` (pure, tree-aware) and tree-state checks; `check_write` keeps its signature as the composition. |
| `choobi/commitwriter.py` (modify) | `write_and_commit(..., source_branch=...)`; builds off the source branch tip; raises `Parked` instead of `Conflict` on attach-time collisions; `attach_pending` shared with apply. |
| `choobi/apply.py` (create) | `choobi apply`: land parked refs onto the current branch. |
| `choobi/pushing.py` (create) | Auto-push policy: push only where the developer already pushed. |
| `choobi/engine.py` (modify) | Tree-based anchored reads, branch capture, parked outcome, retry-with-feedback, runtime backoff, silent assessment, findings handoff, creation reviewer sees existing docs, chat-only linkage. |
| `choobi/cli.py` (modify) | Coalescing + unreachable-source skip for `post_commit`, range widening, `apply` and `audit` commands. |
| `choobi/pr.py` (modify) | No lock; report a running update instead. |
| `choobi/status.py`, `choobi/views.py` (modify) | Parked section with `choobi apply` hint; new status glyphs. |
| `choobi/history.py` (modify) | `parked`/`coalesced`/`audit` statuses; `coalesced_sources`, `docs_commits` lookups. |
| `choobi/errors.py` (modify) | `Parked`, `PushRejected`. |
| `choobi/config.py` (modify) | `auto_push: bool = True`. |
| `choobi/audit.py` (create) | Read-only stale-claim report. |
| `choobi/help.py`, `README.md`, `HOW_CHOOBI_WORKS.md`, `build-plan.md` (modify) | Document the new contract. |
| `tests/test_harness.py` (create) | All new scenario tests (fake repos, bare remotes, bursts, branch switches). |

---

### Task 1: git plumbing

**Files:** Modify `choobi/gitio.py`. Test `tests/test_harness.py`.

**Produces:**
```python
def current_branch(root) -> Optional[str]            # "main" or None when detached
def ls_tree(root, rev) -> Dict[str, str]             # path -> mode ("100644", "120000", ...)
def show_blob(root, rev, path) -> bytes              # raises RuntimeError when absent
def commits_between(root, base, tip) -> List[str]    # rev-list base..tip, oldest first
def upstream(root) -> Optional[Tuple[str, str]]      # (remote, remote_branch) for @{u}
def push_fast_forward(root, remote, branch, sha, env) -> None  # raises RuntimeError on rejection
def pending_refs(root) -> Dict[str, str]             # source_sha -> pending_sha
```

- [ ] Write tests: detached HEAD returns None; ls_tree flags a symlink mode; show_blob reads committed bytes even when the working tree is dirty; upstream is None with no remote and `("origin","main")` after `push -u`; push_fast_forward rejects non-FF.
- [ ] Implement. Run `python -m pytest tests/test_harness.py -q`. Commit.

### Task 2: `docs.Tree`

**Files:** Modify `choobi/docs.py`. Tests.

**Produces:**
```python
class Tree:
    root: Path; rev: Optional[str]
    @classmethod working(cls, root) -> Tree
    @classmethod at(cls, root, rev) -> Tree
    def files(self) -> List[str]
    def exists(self, path) -> bool          # file or directory prefix
    def read(self, path) -> Tuple[str, str] # (text, sha256); rejects symlink/non-regular
def tracked_documents(root, policy, tree: Optional[Tree] = None)
```
`Tree.working().read` delegates to `docs.read_snapshot` (keeps the symlink-safe descriptor read and the existing test mocks). `Tree.at().read` uses `ls_tree` mode + `show_blob`.

- [ ] Tests: `Tree.at(HEAD).read` ignores a dirty working copy; symlink blob raises `NotAllowedPath`; `tracked_documents(tree=Tree.at(HEAD))` returns committed content.
- [ ] Implement, run, commit.

### Task 3: verify split

**Files:** Modify `choobi/verify.py`. Tests.

**Produces:**
```python
def check_content(root, target, content, *, is_create, old_content, policy, evidence="", tree: Tree) -> None
def check_tree_state(root, target, *, is_create, expected_hash) -> None   # clean, exists/absent, op in progress
def check_write(...)  # unchanged signature; = check_content(working tree) + check_tree_state
```
`_check_links` and `_check_covers` take `tree` and use `tree.exists` / `tree.files`.

- [ ] Tests: link check against a committed tree passes when the linked file exists only in HEAD (deleted in working tree); existing `check_write` tests unchanged.
- [ ] Implement, run full suite, commit.

### Task 4: commitwriter parks instead of racing

**Files:** Modify `choobi/commitwriter.py`, `choobi/errors.py`. Tests.

**Produces:**
```python
class Parked(ChoobiError): reason = "parked"; pending: str; why: str
class PushRejected(ChoobiError): reason = "push_rejected"

def write_and_commit(root, writes, message, *, source_commit, expected_hashes, source_branch: Optional[str]) -> str
def attach_pending(root, pending: str, *, paths: List[str]) -> str   # cherry-pick with guards; raises Conflict
```
Algorithm:
1. `base` = `refs/heads/<source_branch>` tip, or HEAD when `source_branch is None`.
2. `source_commit` must be an ancestor of `base`, else `Conflict("source commit no longer on branch")`.
3. Every `expected_hashes[path]` must equal sha256 of `base:path` blob (None ⇔ absent), else `Conflict` (stale draft; engine re-runs once).
4. Build the pending commit in a detached worktree at `base`; store at `refs/choobi/pending/<source_commit>`.
5. Attach guards (any failure → `Parked`, ref kept): `current_branch(root) == source_branch`; `is_ancestor(base, HEAD)`; no operation in progress; `working_tree_clean(paths)`.
6. Cherry-pick; on git failure abort and raise `Parked` with the git error.

- [ ] Tests: branch switched before attach → `Parked`, pending ref exists, other branch untouched; dirty target in working tree → `Parked`; human committed the target after verification → `Conflict` (stale), nothing written; happy path unchanged; `source_branch=None` detached path works.
- [ ] Update the three existing commitwriter tests that pass no `source_branch` (add `source_branch=gitio.current_branch(root)`).
- [ ] Implement, run, commit.

### Task 5: `choobi apply`

**Files:** Create `choobi/apply.py`. Modify `choobi/cli.py`, `choobi/help.py`. Tests.

**Produces:**
```python
@dataclass
class ApplyOutcome: source: str; pending: str; status: str; detail: str   # landed | skipped
def apply_pending(root, cfg) -> List[ApplyOutcome]
```
For each pending ref oldest-first: source must be an ancestor of HEAD (else skipped: "source commit is not on this branch"); `attach_pending`; on success record history `committed` (trigger `apply`), delete ref, auto-push (Task 6). On `Conflict` keep the ref and report.

- [ ] Tests: parked → switch back → `apply` lands it and deletes the ref; pending for another branch is skipped with the reason; `choobi apply` CLI prints one line per ref.
- [ ] Implement, run, commit.

### Task 6: auto-push

**Files:** Create `choobi/pushing.py`. Modify `choobi/config.py`. Tests with a bare remote.

**Produces:**
```python
def maybe_push(root, cfg, *, source_commit: str, docs_commit: str) -> str  # "pushed" | "no_upstream" | "not_pushed_by_user" | "disabled" | raises PushRejected
```
Rule: push iff `cfg.auto_push`, `upstream()` exists, `is_ancestor(source_commit, "<remote>/<branch>")`, `is_ancestor("<remote>/<branch>", docs_commit)`. Command: `git push <remote> <docs_commit>:refs/heads/<branch>` with `CHOOBI_GENERATING=1`. Never `--force`.

- [ ] Tests: user pushed → Choobi's commit appears on the bare remote; user did not push → nothing pushed, local commit exists; remote moved ahead → `PushRejected`, local commit stays; `auto_push=False` → "disabled".
- [ ] Implement, run, commit.

### Task 7: engine

**Files:** Modify `choobi/engine.py`, `choobi/history.py`. Tests.

Changes, each with a test:
1. **Anchored reads from git objects.** For `req.source_commit` runs, `tree = Tree.at(root, head)`; all doc, changed-input, surface, and verify reads go through `tree`. Detached runs use `Tree.working`.
2. **Branch capture.** `source_branch = gitio.current_branch(root)` at start; passed to `write_and_commit`.
3. **Parked outcome.** Catch `Parked` → `history.add_record(status="parked", docs_commit=pending, patch=..., reason=why)`; `UpdateResult(status="parked")`; checkpoint and snapshot advance (the work is done, only landing is deferred). `find_by_source` treats `parked` and `coalesced` as handled.
4. **Retry with feedback.** `MAX_DRAFT_ATTEMPTS = 3`. Editor call + parse + covers merge + `check_content` in a loop; `RuntimeOutputInvalid | VerificationFailed | NotAllowedPath` appends a `## Previous attempt was rejected` block to the prompt and retries. After the last attempt re-raise; `run_update_guarded` stores the last draft's unified diff in `patch`.
5. **Runtime backoff.** `_complete` retries `RuntimeUnavailable` with `RUNTIME_BACKOFF = (5, 20)` seconds via `time.sleep` (patched in tests).
6. **Stale re-run.** `run_update_guarded` catches `Conflict` once and re-runs from scratch.
7. **Silent assessment.** `_parse_disposition` allows a non-empty `summary` on `silent`; prompt asks for one sentence naming what was checked; recorded as the no-op summary.
8. **Findings handoff.** `LINKAGE_SCHEMA` gains optional `findings: string`; `LinkageDecision.findings`; editor prompt shows `## Ownership findings`.
9. **Creation reviewer sees existing docs.** `_build_creation_review_prompt(..., existing_docs=doc index text)`.
10. **Chat-only linkage.** When linkage would skip but `req.chat_context` is present and no target was pinned, run semantic linkage with a `## Conversation context` block.
11. **Auto-push** after commit: `pushing.maybe_push`; `PushRejected` recorded as `reason="push_rejected"` on the committed record; `UpdateResult.push` carries the outcome string.

### Task 8: CLI coalescing and skips

**Files:** Modify `choobi/cli.py`, `choobi/history.py`. Tests via `cli._cmd_update` with mocks (pattern from `test_cli_lock_contention_is_a_typed_failure`).

After acquiring the lock for `trigger == "post_commit"`:
- If `source_commit` is on no branch (`git branch --contains` empty) → record `no_op` reason `source_commit_unreachable`, exit 0.
- If a **human** commit exists in `source_commit..HEAD` (i.e. `commits_between` minus known docs commits and pending refs) → record `coalesced`, exit 0. The newer commit's own queued job covers the range.
- Otherwise widen: `start = source^`; while `history.find_by_source(start)` has status `coalesced`, `start = start^`. `rev_range = f"{start}..{source}"`.

- [ ] Tests: burst A,B,C — jobs for A and B record `coalesced`; C's job sees `A^..C` as its range (assert on the FakeRuntime prompt containing both diffs); a source moved by Choobi's own docs commit is **not** coalesced; an amended-away commit is skipped as unreachable.

### Task 9: `pr create` never blocks

**Files:** Modify `choobi/pr.py`, test `test_pr_holds_the_update_lock_while_creating` → `test_pr_does_not_wait_for_a_running_update`.

- [ ] No lock. If `locking.is_running(repo_id)`, append `"\nchoobi: a docs update is still running and will push to this branch when it lands."` to the returned text.

### Task 10: status and views

**Files:** Modify `choobi/status.py`, `choobi/views.py`, `choobi/ui/static/app.js` (glyph map only).

- [ ] `status.report` gains `parked: List[{source, pending}]` from `gitio.pending_refs`; `render` prints `PARKED = "parked — docs commit waiting; run `choobi apply` to land it"` per ref; last committed record shows `pushed`/`push_rejected`.
- [ ] `_GLYPH` adds `parked: "⧗"`, `coalesced: "·"`, `audit: "≡"`.

### Task 11: `choobi audit`

**Files:** Create `choobi/audit.py`. Modify `choobi/cli.py`, `choobi/help.py`. Tests.

**Produces:**
```python
AUDIT_SYSTEM, AUDIT_SCHEMA
@dataclass class Finding: doc: str; claim: str; status: str  # contradicted | unverified
                          evidence: str
def run_audit(root, cfg, runtime) -> Tuple[List[Finding], List[str]]   # findings, notes (docs skipped and why)
def render_report(findings, notes) -> str
```
One model call per writable document. Evidence = the document + complete contents of every tracked file its `covers:` globs match (read from `Tree.at(HEAD)`), truncated never — a document whose evidence exceeds the prompt ceiling is skipped with a note. Documents without `covers:` are skipped with a note unless tools are enabled, in which case the model may read tracked files. Writes nothing to the repo; saves the report to `~/.choobi/repos/<id>/audit.md`; records history status `audit`.

- [ ] Tests: contradicted claim reported with evidence; doc without covers produces a note; report file written; no repo file changes.

### Task 12: documentation

- [ ] `help.py`: `choobi apply`, `choobi audit`, `pr create` detail, `status` detail.
- [ ] `README.md`: commands table; "What happens when you commit" section listing the outcomes (landed, landed + pushed, parked, silent with reason, coalesced, failed after retries).
- [ ] `HOW_CHOOBI_WORKS.md` "Git and concurrency model" and "Current limits": rewrite for branch identity, parking, coalescing, auto-push, retries.
- [ ] `build-plan.md` §3.1 concurrency contract: same.

### Task 13: end-to-end scenarios (fake repos)

`tests/test_harness.py::EndToEndTest`, each a real `git init` repo, some with a bare `origin`:

- [ ] commit → update lands as the next commit, message reused, working tree clean.
- [ ] commit, `git push -u`, update → docs commit present on the bare remote.
- [ ] commit, switch branch mid-job (simulate by switching before `write_and_commit`) → parked; switch back; `apply` lands; remote updated.
- [ ] commit with a dirty doc in the working tree → parked, user's edit untouched.
- [ ] burst of three commits through `cli._cmd_update` sequentially → one docs commit, two coalesced records.
- [ ] model returns a draft with a broken link, then a good one → committed on attempt 2, one record.
- [ ] runtime unavailable twice, then fine → committed; `time.sleep` called with the backoff schedule.
- [ ] `pr create` while the lock is held → returns the URL, does not raise.

## Self-review

- Spec coverage: decisions 1–10 map to Tasks 4/6 (1,2,7), 4 (3), 2/3/7 (4), 8 (5), 7 (6), 6 (7), 9 (8), 7/10 (9), 11 (10).
- Type consistency: `Tree` defined in Task 2, consumed by 3, 7, 11. `Parked` defined in Task 4, consumed by 5, 7, 10. `maybe_push` defined in Task 6, consumed by 5, 7.
