# choobi — Build Plan

> Status: consolidated product and implementation contract. This document is the canonical
> description of choobi v1 and the v2 team direction. The v1 interaction model is intentionally
> small: a commit triggers choobi, an engineer can invoke choobi from an active coding-agent chat,
> and choobi records what it changed and knows about the repository in a local UI.

---

## 1. Product

**choobi** is a local-first documentation agent that keeps engineering documentation consistent
with the work an engineer is doing.

In v1, choobi has three visible behaviors:

1. After a human creates a git commit in an initialized, supported shell or coding-agent harness,
   choobi checks the code changed by that commit, updates the relevant documentation or creates a
   new document when the repository policy permits it, verifies the result, and creates a docs-only
   follow-up commit using the exact same commit message.
2. While an engineer is working or reviewing a PR in a supported coding-agent CLI, they can say
   **"choobi update"**. Choobi reads the active chat context and the relevant code diff, then
   updates documents such as a README, build plan, technical design, or PRD.
3. Running `choobi` opens a local UI where the user can view or edit their personal style guide,
   browse a history of documentation updates grouped by repository and sorted by date, and inspect
   Choobi's local map of the repository's code and documentation.

The user-facing completion message is deliberately short:

```text
choobi just updated the docs — documented the new retry behavior in docs/api.md.
```

If no documentation needs to change, choobi stays silent.

## 2. Product principles

### 2.1 Documentation must follow the work

The source of scope is always concrete work in front of the user:

- For the automatic path, scope is the committed code diff.
- For an explicit `choobi update`, scope is the active coding-agent conversation plus the current
  commit or PR diff.
- Choobi does not sweep unrelated repositories, crawl arbitrary historical chats, or rewrite the
  whole documentation tree.

### 2.2 Automatic means automatic

When choobi can safely verify a documentation update, it writes and commits the update without
requiring a second approval step. The resulting commit is reviewable through normal git history
and through choobi's local activity log.

Choobi fails loudly instead of partially applying an update. It never silently skips a requested
primary operation, commits an unverifiable claim, or overwrites a document that changed while the
background job was running.

### 2.3 Staying quiet is part of correctness

Most commits do not require documentation changes. A cheap deterministic filter runs before any
LLM call. Choobi only invokes the documentation agent when the changed code or chat context has a
credible relationship to an existing document or satisfies the strict new-document policy.

### 2.4 Policy, style, and knowledge are separate

- The **documentation policy** decides whether to create, update, or stay silent.
- The **style guide** decides how the resulting documentation should read.
- The **repository guide** declares repo-specific document types, locations, audiences, and owners.
- The **knowledge base** is Choobi's derived map of code, documents, relationships, and scan state.

The knowledge base is evidence, not policy. Inferred relationships never silently override an
explicit repository guide.

### 2.5 Personal first, team later

V1 is personal and local. The style guide, activity history, runtime credentials, and generated
patch metadata stay on the user's machine.

V2 adds repository-wide team style and shared history. V1 does not pretend that independent local
databases are a team consistency system.

## 3. V1 interaction model

### 3.1 Automatic post-commit update

Every human `git commit` fires the `post-commit` hook. The hook performs no inference itself. It
records a small event and starts the background runner, then returns immediately.

```text
human git commit
  -> post-commit hook enqueues source commit
  -> background runner reads the committed diff
  -> deterministic relevance filter finds candidate docs
  -> documentation agent produces a surgical patch
  -> verifier checks every changed claim it can check mechanically
  -> choobi creates a docs-only follow-up commit
  -> personal history records the result
  -> choobi sends one completion message
```

The background job examines the committed tree, not an uncommitted approximation of the user's
work. Its primary scope is `source_commit^..source_commit`.

#### Follow-up commit contract

The choobi commit:

- uses the source commit's exact subject and body;
- preserves the message byte-for-byte by using verbatim commit-message cleanup;
- is a separate commit immediately following the source work when the branch has not advanced;
- contains only verified documentation paths; all Choobi metadata stays in local storage;
- never amends or rewrites the human commit;
- never stages or commits unrelated user changes;
- preserves the user's existing git signing policy;
- records a durable `source_commit -> docs_commit` relationship in the local history database for
  idempotency and recovery rather than changing the commit message; and
- sets an inherited generated-commit marker so its own `post-commit` hook exits immediately. The
  marker and durable history mapping are both required; commit-message inspection is never used as
  a recursion guard.

If the configured signing operation, documentation write, verification, or commit cannot complete,
choobi creates no partial commit. The failure is recorded with a typed reason and shown in the UI.

#### Concurrency contract

The background runner serializes relevant source commits per repository in commit order. It may
batch deterministic no-op filtering, but it never coalesces two relevant source commits into one
docs commit: each successful event retains its own source mapping and exact commit message. If later
human commits already exist, Choobi verifies the earlier event against the current head and commits
it there without rewriting history; a conflict produces a typed failure.

Generation and verification run in an isolated temporary worktree and produce a prebuilt commit on
a Choobi-owned pending ref; they never mutate the user's checkout. At a supported shell or
coding-agent prompt boundary, Choobi acquires the repository lease, requires a clean index and clean
target paths, rechecks the branch head and target hashes, then attaches the prebuilt commit with one
guarded Git cherry-pick. Choobi never manually patches or stages the live checkout. Git's own
local-change checks are the final write guard, and any failure aborts the cherry-pick and leaves the
pending ref intact.

`choobi init` installs both the post-commit hook and a supported prompt integration. Automatic
attachment is guaranteed only in those initialized environments. Commits made through an
unsupported shell or Git GUI still enqueue analysis but remain visibly pending with
`safe_boundary_required`. `choobi pr create` must drain all relevant pending commits or fail with
`pending_docs_update`; it never opens a PR that falsely claims the docs were updated.

Before attaching a pending commit, choobi checks:

- the source commit is still an ancestor of the active branch;
- every existing target document still has the content hash used during generation;
- every proposed new-document path is still absent;
- no git merge, rebase, cherry-pick, or commit is currently mutating the repository; and
- every output path is inside the configured documentation allowlist.

If any invariant fails, choobi does not write or commit. It records the exact failure and allows a
later explicit `choobi update` to run against the new state.

Because a detached hook cannot safely print into a terminal after the shell prompt has returned,
completion is delivered through an OS notification, recorded in the Activity UI, and displayed by
the supported shell or coding-agent integration at its next safe prompt boundary. Choobi never
writes asynchronous output into a terminal where the user may be typing.

### 3.2 Coding-agent chat update

While using Codex, Claude Code, or another supported coding-agent CLI, the user can say:

> **choobi update**

The harness-specific choobi integration passes the active conversation, repository root, current
source commit, changed files, and PR metadata to the choobi CLI through a structured input contract.
The choobi binary does not scrape private session databases or guess which chat the user meant.

The chat is evidence about intent and decisions. It lets choobi update documents that cannot be
derived from source signatures alone, including:

- `README.md` setup and usage instructions;
- `build-plan.md` or another implementation plan;
- architecture and technical-design documents;
- product requirement documents; and
- debugging or operational notes.

The same linkage, surgical-edit, verification, commit, notification, and history pipeline is used.
The adapter supplies a source commit for the update, and the generated docs commit reuses that
commit's exact message. If there is no source commit, choobi returns a typed `source_commit_required`
error instead of inventing a commit association.

Only the active conversation is in scope. Before inference, choobi removes tool noise, excludes
secret and ignored-file content, and retains user decisions, implementation facts, files mentioned,
and unresolved questions. The original transcript is not persisted in the activity database.

### 3.3 Pull-request author flow

After choobi has created a docs commit and the user opens a PR through `choobi pr create` or a
supported coding-agent wrapper around that command, choobi inserts this exact line into the PR
description:

```text
choobi updated docs.
```

The line is intentionally not a report. The detailed record already exists in git and in the local
choobi activity history.

In v1, `choobi pr create` owns the supported PR-creation path and delegates the actual GitHub
operation to the authenticated GitHub CLI. A purely browser-created PR or a direct `gh pr create`
cannot wake the local process. Automatic coverage for every PR regardless of where it is opened
requires the v2 GitHub App.

Choobi inserts the line only when the branch contains a successful choobi docs commit associated
with the PR's source range. It never claims that docs were updated when they were not.

### 3.4 Pull-request reviewer flow

While reviewing another person's PR from a supported coding-agent CLI, the reviewer can say
**"choobi update"**. The adapter supplies the PR base, PR head, active conversation, and local
checkout. Choobi updates and commits the relevant docs locally using the PR head commit's exact
message.

V1 does not push to another person's branch or post a review comment automatically. The resulting
local commit is available for the reviewer to inspect, share, or apply through their normal git
workflow.

## 4. Invocation and integrations

The standalone `choobi` executable is the canonical implementation. Every other entry point — the
post-commit hook, a coding-agent chat, a UI button — is a thin wrapper that composes the same CLI
contract. It submits structured context; it never duplicates choobi's reasoning or editing logic.

### 4.1 Command surface

There is exactly one engine verb, `update`. Everything else is deterministic plumbing.

```text
choobi                     open the local window            (alias: choobi ui)
choobi init                install post-commit hook + project agent skill (per repo)
choobi install             install the coding-agent skill into Claude Code and Codex (user)
choobi auth [claude|codex] pick the runtime model and log it in (delegates to that CLI's login)
choobi update …            run the documentation engine     (see 4.2)
choobi status              show pending, failed, and no-op jobs and repo checkpoints
choobi docs                list the writable docs in this repo and what each covers
choobi changelog [-n N] [--all] [--status S]   browse the activity log
choobi show <id>           show one activity record in full, including the patch
choobi style               print the resolved style guide
choobi pr create           create the PR via the authenticated gh CLI and annotate it
choobi help [COMMAND]      command reference; the window's commands panel renders this source
```

Only `update` calls a model. Everything else is deterministic (the runtime `auth` login is
delegated to the runtime CLI), so the window can invoke them without a token budget.

### 4.2 The `update` grammar

`update` is polymorphic over three orthogonal inputs. Every caller — automatic, chat, PR, UI, or a
human typing at a shell — fills the same three slots; the caller differences are only *which slots
are provided*:

```text
choobi update  [TARGET…]  [SCOPE…]  [-- INSTRUCTION]

TARGET…       zero  → choobi infers the target docs from the diff (the automatic path)
              one+  → the user pins the docs (repo-relative path or fuzzy name, e.g. "the api doc")

SCOPE…        where the truth comes from; supply one or more:
                --commit <sha> | --range <a..b> | --pr <n>   (commit-anchored)
                --chat                                        (harness-supplied conversation)
                --staged | --working                         (uncommitted, explicit invocation only)
                --detached                                   (instruction is the only scope)

INSTRUCTION   free-text after `--`, the natural-language "based on …" that narrows the edit
```

Callers reduce to this single contract:

```text
post-commit         choobi update --commit <sha>                       (no target, no instruction)
chat "update the    choobi update docs/api.md --chat -- "document the retry change"
  api doc based on…"   (harness resolves the fuzzy target and supplies --chat)
UI doc button       choobi update docs/api.md  + instruction field + scope picker
targeted edit       choobi update docs/api.md --detached -- "clarify the retry backoff"
```

#### Commit-message authoring

- A **commit-anchored** scope (`--commit`, `--range`, `--pr`) keeps the §3.2 contract: the docs
  commit reuses that source commit's exact message.
- **`--detached`** is the only path that authors its own message. It exists for instruction-driven
  edits that correspond to no single commit (a UI doc button with a typed instruction, a
  documentation fix from knowledge rather than a diff). It produces a docs-only commit with a
  choobi-generated message and records `detached` as the trigger type in history. It is a distinct,
  named path — never a silent fallback when a source commit is merely absent.
- With neither a commit-anchored scope nor `--detached`, choobi returns typed
  `source_commit_required` rather than inventing a commit association.

Every scope still passes the same relevance gate, surgical-edit, verification, commit, notification,
and history pipeline. `--detached` changes only message authoring, not the write boundary.

Harness wrappers are responsible for obtaining the active conversation and PR context because the
harness owns that data. The first runtime adapter should target one non-interactive coding-agent
CLI. Additional runtime and harness adapters must implement the same versioned input and output
schemas.

### 4.3 `choobi status` output

`status` is a deterministic, no-model read of local state for the active repository. It surfaces the
pending/failed/no-op machinery from §3.1 and the checkpoints from §7.3. The state is exact; the copy
is deliberately warm. Each line is fixed, filled with the real record, and reused verbatim by the UI:

```text
pending      pending — choobi still working!
failed       failed — choobi is sorry :< try again pls!   (typed reason follows)
no-op        no-op, choobi decides to not write
checkpoint   checkpoint <sha>, choobi last worked on <subject>
idle         nothing running now!
```

The friendly wording is presentation only; the machine-readable typed reason (`documentation_gap`,
`source_commit_required`, `budget_exceeded`, verification failure, conflict) is still recorded and
still shown next to a failed line so a stuck job is diagnosable.

## 5. Documentation engine

All three update paths call the same engine.

### 5.1 Scope collection

Inputs are explicit and immutable:

- repository identity and root;
- source commit, base commit, and head commit;
- committed diff and enclosing symbols;
- active-chat context when explicitly invoked;
- candidate existing documents and allowed new-document placements;
- content hashes for every existing candidate;
- effective documentation policy, repository profile, style guide, and deterministic rules; and
- prompt, runtime, and schema versions.

### 5.2 Code-to-document linkage

Choobi discovers candidate documents using the cheapest available evidence:

1. optional `covers:` front matter;
2. README ownership of its immediate directory (non-recursive, so a root README is not a
   candidate for every change in the tree);
3. literal path mentions in the document; and
4. chat references to a named README, plan, design, or PRD.

`covers:` is an optimization and an editable declaration, not truth. The verified source tree and
the evidence attached to a specific update are authoritative.

**Recall backbone (as built).** The cheap linkage above only finds documents already tied to a
change, so it misses new or unlinked code. Three mechanisms close that gap:

- **New-surface detection.** Source files added in the commit, or drifted since a persisted
  per-repo snapshot of reconciled source, that no document owns are surfaced to the model as
  create candidates. The snapshot makes recall robust to commits the hook missed. The first run
  in a repo establishes the baseline snapshot without flooding the model.
- **Gated linkage pass.** When nothing is linked but source changed, choobi makes one bounded
  model call over a compact index of every document (path, first heading, `covers:`) to pick the
  owning document, or create, or none. This catches the semantic tail deterministic rules miss.
- **Self-reinforcing `covers:`.** After a successful write, choobi records the source files the
  document now covers in its front matter, so the next change links deterministically without the
  gated pass. Recall compounds and cost drops over time.

Symbol-level linkage and embeddings remain out of scope for v1.

The default writable surface is git-tracked Markdown or MDX explicitly classified as documentation,
including README, `docs/**`, build-plan, design, and PRD paths. A new file must also match a declared
document root and type from the local repository profile. Agent instruction files, source code, CI
configuration, and arbitrary tracked Markdown are not writable unless the user explicitly adds them
to the documentation allowlist.

### 5.3 Create, update, or stay silent

Choobi applies one disposition to each documentation need:

- **Update** an existing document when it already owns the changed behavior, workflow, decision,
  or operational responsibility.
- **Create** a document only when the change introduces a stable, independently discoverable
  concept with no existing owner. Examples include a new public API, CLI or configuration surface,
  user workflow, integration, service or architecture boundary, operational runbook, durable design
  decision, or audience that needs its own entry point.
- **Stay silent** when no documented behavior changed. This normally includes internal refactors,
  local renames that reference analysis proves do not alter a documented path or exported name,
  formatting, tests, generated artifacts, temporary experiments, bug fixes that restore
  already-documented behavior, and non-durable implementation details.

Automatic creation requires all of the following: a qualifying stable concept, a named audience and
owner, a deterministic document type and writable path from the repository profile, no existing
document that should own the content, and an explicit per-repository opt-in. If the change deserves
documentation but any placement invariant is missing, Choobi records a typed `documentation_gap`
and creates nothing. There is no guessed path or catch-all document.

### 5.4 Relevance gate

The deterministic gate rejects changes such as formatting, renamed locals, generated artifacts,
and internal refactors with no externally documented behavior change. If no existing document or
allowed new-document placement survives, choobi records a cheap no-op and makes no model call.

The LLM relevance decision receives only changed hunks, enclosing symbols, candidate document
sections or placement declarations, relevant chat decisions, and the applicable style rules. It
returns structured output.

### 5.5 Surgical edit

Choobi edits the smallest relevant section. It does not rewrite an entire document to update one
fact. Updating an existing owner document is preferred over creating a new file.

As built, the model returns the full updated content of one document, and choobi writes that
content. "Surgical" is enforced two ways: the prompt instructs the smallest edit, and the verifier
rejects an update that drops more than one existing section (measured by markdown headings), which
catches a wholesale rewrite while still allowing a single heading rename such as a changed
signature.

### 5.6 Verification

Verification is the write boundary. As built, before any write choobi checks that:

- the output path is inside the documentation allowlist;
- referenced relative links and paths resolve;
- proposed content contains no secret-shaped strings;
- an update still matches the content hash choobi read (no concurrent edit);
- a create target is still absent;
- an update drops no more than one existing section; and
- no merge, rebase, or cherry-pick is in progress.

The commit then contains only the changed document path, never unrelated working-tree changes. If
any check fails, choobi aborts the entire patch and commit and records the typed failure; it never
drops the bad part and commits the remainder.

Planned but not yet implemented: matching referenced symbols and signatures against the source
tree, and executing commands or examples through a repository-declared safe verifier.

## 6. Token and latency budget

Every commit triggers choobi, but every commit must not trigger an expensive agent run.

V1 uses these controls:

- hooks only enqueue small events;
- deterministic filtering of queued commits may be batched, while relevant commits remain distinct;
- deterministic relevance runs before the LLM;
- context contains only changed hunks, enclosing symbols, candidate doc sections, and relevant
  style, repository-profile, and chat excerpts;
- no-op results and completed analyses are cached by complete input fingerprint;
- superseded jobs are cancelled before another model call;
- the runtime adapter enforces a maximum call count and token budget per job;
- one global scheduler gives interactive `choobi update` calls priority over background work,
  limits background model concurrency across all repositories, and records `budget_exceeded`
  instead of exceeding the configured daily budget; and
- activity records retain input tokens, output tokens, duration, and outcome.

Product metrics include documentation-update precision, missed-update recall, successful docs
commits, failed verification rate, no-op rate, cache-hit rate, p50/p95 completion time, and p50/p95
tokens per human commit.

## 7. Documentation policy, style, and repository knowledge

Each choobi installation has a usable default policy and style without setup.

### 7.1 Built-in baseline

The application ships an immutable, versioned baseline:

- `baseline/style.md` contains prose guidance such as voice, tone, brevity, and structure.
- `baseline/policy.yaml` contains the general create, update, and stay-silent criteria.
- `baseline/rules.yaml` contains deterministic safety rules such as output allowlists, link checks,
  and commit behavior.

Installed baseline files are never edited in place, so application upgrades cannot overwrite a
user's preferences.

### 7.2 Personal override

The user edits a personal guide at:

```text
~/.choobi/style.md
```

The UI shows the resolved style and distinguishes inherited baseline values from personal
overrides. The personal guide may configure:

- voice, tone, and verbosity;
- preferred and banned terminology;
- README, build-plan, design, and PRD conventions; and
- the one-sentence detail after the fixed `choobi just updated the docs` prefix.

V1 style is personal. It cannot weaken safety rules or decide repository placement.

### 7.3 V1 per-repository SOP and knowledge base

Each initialized repository gets two Markdown files under `~/.choobi/repos/<checkout-id>/`, where
`<checkout-id>` hashes the Git common directory so linked worktrees share state while forks and
unrelated repositories stay distinct.

**SOP** (`sop.md`) is human-authored documentation preferences that choobi acts on. Its prose is fed
into the engine prompt alongside the style guide, and its `allow_create` front-matter flag gates
new-document creation. The default SOP allows creation, describes the document categories (public
features and user journeys, public CLI or SDK reference, internal plans, and internal feature
explanations), tells choobi to look for an existing owner before creating, and carries empty
sections for repo-specific style, terminology, audience, ownership, cadence, verification, and what
to never document. The user edits it in the window.

**Knowledge base** (`knowledge.md`) is choobi's generated map of the repo: the writable documents
grouped by category with their `covers:`, a top-level code map that flags directories no document
covers, and the last activity. It is built in one deterministic pass over the git-tracked tree, with
no model call. It regenerates on demand, and the user may also edit and save it.

A per-repo `snapshot.json` records the source files choobi last reconciled, so it can detect new
source that drifted in through commits the hook missed (see 5.2). Repository identity and activity
live in the shared `~/.choobi/choobi.db`: a repos registry populated by `choobi init` and by any
activity, per-repo checkpoints (last source commit and subject), and the full activity history.

Planned but not yet built: a richer profile with per-field `builtin`, `repo_metadata`,
`user_pinned`, and `inferred` provenance, and unresolved `documentation_gap` records surfaced in the
window.

### 7.4 V2 committed repository guide

V2 adds an explicit, versioned guide committed with the repository:

```text
choobi/
  guide.yaml
  style.md
```

`guide.yaml` is the authoritative structural policy for document types, roots, naming, audiences,
owners, writable paths, creation rules, approved verifier identifiers, and code-to-doc declarations.
It is untrusted input: Choobi validates its schema, pins its blob SHA for each job, enforces immutable
system path ceilings, and runs only registered sandboxed verifiers rather than arbitrary commands.
Choobi cannot edit `choobi/guide.yaml` or `choobi/style.md` itself.

`style.md` is the team's shared writing style. On personal clients, writing style resolves in
repository, then personal, then built-in order. Repository Choobi resolves repository, then an
optional organization default, then its immutable baseline. Changes to either file are reviewed
like code.

Both committed files are untrusted prompt inputs: immutable system rules remain authoritative,
secret-shaped content is rejected, and explicit size and token ceilings apply. Enabling team mode
makes `choobi/guide.yaml` outrank all V1 local structural pins. Conflicting pins remain visible as
suggestions for promotion or deletion but cannot affect team-mode writes.

## 8. Local UI

Running `choobi` with no subcommand launches the local choobi window. `choobi ui` is an explicit
alias. The UI is backed by the local CLI process and requires no hosted account.

Any local HTTP or webview bridge binds only to loopback and requires a random per-launch access
token.

As built, the window is a native desktop window rendered with pywebview (WKWebView on macOS),
backed by the loopback server above. There is no browser fallback: if pywebview is unavailable the
command fails loudly. The look is black-on-white monospace with a pixel-art mascot that wiggles on
save. The **choobi blob** is a hand-drawn mascot; v1 ships a placeholder pixel blob (a cheese block)
until the real art lands.

The window is configuration and inspection only. It does not trigger documentation updates; those
happen through commits, the CLI, and the agent skill.

### 8.0 Screens

**Onboarding.** On first launch, choobi shows one onboarding screen: the user's name, an optional API
key, and the runtime choice (claude or codex). It is the only screen shown until the user starts.

**Home.** Three tabs, plus two icons in the top right:

- **instructions** — the repositories that ran `choobi init`, each shown by name. Opening one shows
  its full path and two panels: the editable **SOP** (per-repo preferences, with save and
  return-to-default) and the **knowledge base** (choobi's generated repo map, editable, with a
  regenerate action). See §7.3.
- **style** — the resolved style guide, editable inline, with save and return-to-default.
- **changelog** — the repositories that ran choobi, each opening to its logs, each log opening to
  full detail (see §8.1).
- **book icon** — the command reference, rendered from the same source as `choobi help` so the
  panel and CLI cannot drift.
- **terminal icon** — hover to see the current runtime and model.

The window never exposes raw prompts or retains full chat transcripts.

### 8.1 Changelog navigation

The changelog is a drill-down: the repositories that ran choobi, ordered by when init ran; then a
repository's logs newest-first, each titled by its one-sentence summary; then a log's full detail.
Empty repositories read as `// empty`.

Each history record contains:

- repository identity and local path;
- trigger type: `post_commit`, `agent_chat`, or `pr_review`;
- source, base, head, and generated commit SHAs where applicable;
- timestamp and duration;
- documents changed;
- the one-sentence completion summary;
- the exact documentation patch;
- verification results and any typed failure;
- committed, failed, or no-op status;
- runtime and prompt versions; and
- input tokens, output tokens, and estimated cost when the runtime exposes it.

The history database does not store source-code snapshots, secrets, or full chat transcripts.

### 8.2 Personal storage

V1 stores preferences and activity locally (override the root with `CHOOBI_HOME`):

```text
~/.choobi/
  config.json            name, runtime agent, optional API key, per-repo create opt-in
  style.md               personal style override
  choobi.db              SQLite: activity records, per-repo checkpoints, and the repo registry
  repos/<checkout-id>/
    sop.md               per-repo documentation preferences (editable)
    knowledge.md         generated repo map (editable)
    snapshot.json        reconciled source files, for drift detection
    update.lock          per-repo advisory lock
  logs/hook.log          background hook output
```

SQLite is the personal activity source of truth. A committed Markdown changelog is not part of v1.

## 9. V1 architecture

```text
git post-commit ─┐
agent chat ──────┼─> event/context adapter ─> disposition engine ─> verifier ─> docs commit
PR review ───────┘             │                       │               │
                               │                       │               └─> notification
                               │                       └─> runtime adapter
                               ├─> repository knowledge <─────────────┤
                               └─> personal SQLite history <─────────┘
                                                  │
                                                  └─> local UI
```

Choobi v1 is one local modular application, not a set of network services.

Required components:

```text
standalone CLI
post-commit hook installer and shim
per-repository event queue, lock, and background runner
git snapshot and diff collector
code-to-document linkage and relevance gate
create, update, or stay-silent disposition engine
minimal context builder and token budgeter
global priority and token-budget scheduler
one coding-agent runtime adapter
documentation patch generator
deterministic verifier and secret scanner
docs-only commit writer with recursion guard
notification adapter
personal style resolver
local repository profiler and knowledge repository
SQLite activity repository
local UI
one supported coding-agent harness wrapper
supported shell prompt integration for safe commit attachment
PR-creation wrapper and description annotator
```

## 10. Security and correctness invariants

Repository content, commit messages, PR descriptions, and chat transcripts are untrusted input.

V1 must enforce:

- system rules cannot be overridden by repository or chat text;
- only configured documentation paths may be written;
- ignored files and secret-shaped content are excluded from prompts and outputs;
- the worker never commits unrelated staged or unstaged changes;
- the worker never rewrites a human commit;
- generated commits cannot recursively trigger choobi;
- document hashes are checked immediately before patching and committing;
- git signing policy is honored without an unsigned fallback;
- runtime unavailability produces a typed failure rather than selecting a different runtime; and
- failures and no-ops are recorded honestly in the personal activity history.

Every state-mutating invariant requires a test that fails without the implementation and passes
with it. Race tests must cover a new human commit, a document edit, and a rebase beginning while a
background update is in flight.

## 11. V1 scope

V1 includes:

- automatic post-commit documentation analysis;
- guarded follow-up-commit attachment through an initialized prompt integration;
- verified docs-only follow-up commits with the source commit's exact message;
- one-line completion notifications;
- explicit `choobi update` from supported coding-agent chats;
- README, build-plan, technical-design, and PRD updates when relevant;
- local PR description annotation for supported PR creation flows;
- explicit PR-review updates from a supported coding-agent CLI;
- built-in create, update, and stay-silent documentation policy;
- immutable baseline plus editable personal style guide;
- inferred local repository profiles and code-to-document knowledge bases;
- opt-in automatic document creation when type, path, audience, and owner are deterministic;
- local UI with style, changelog, and repository-knowledge buttons;
- personal changelog grouped by repository and sorted by date; and
- local token, latency, verification, and failure records.

V1 explicitly excludes:

- hosted inference;
- automatic interception of browser-created PRs;
- automatic follow-up attachment from unsupported shells or Git GUI clients;
- a GitHub App or server-side PR bot;
- team-wide style inheritance or activity history;
- automatic document creation in repositories that have not explicitly opted in;
- a committed shared repository guide or repository-wide reconciler;
- periodic remote scans of the repository's default branch;
- embeddings-based retrieval;
- repository-wide adoption rewrites;
- a committed central changelog or generated SSOT index; and
- automatic pushes to another person's branch.

## 12. V2 team direction

V2 adds a team mode scoped to a repository or GitHub App installation. Correctness does not depend
on every contributor installing personal Choobi: local agents remain the fast path, while one
repository-level reconciler is the shared safety net.

### 12.1 Shared repository policy and history

The shared experience includes:

- the committed `choobi/guide.yaml` repository policy and `choobi/style.md` team style;
- a generated repository knowledge map backed by shared scan checkpoints;
- a repository-wide activity timeline with actor identity for each update;
- explicit personal-client and Repository Choobi style precedence;
- filters by repository, package, document, contributor, PR, and date;
- automatic PR-open annotation regardless of whether the PR was created in a browser or CLI;
- GitHub checks or comments backed by the shared event record; and
- organization retention, access-control, and audit policies.

V2 does not synchronize personal SQLite databases. Opted-in clients publish a normalized,
versioned event to the team service. The server stores repository-scoped events and never receives
full local chat transcripts.

### 12.2 Repository Choobi

The V2 UI offers **Enable Master Choobi**. The implementation calls this Repository Choobi: one
repo-level reconciler, not a privileged personal agent.

The canonical event target is the current default-branch head SHA. Default-branch push and merged-PR
webhooks enqueue the same logical event; PR metadata only enriches it. The complete job fingerprint
includes repository, target head, incremental or full-audit mode, guide and style blob SHAs, and
policy, schema, prompt, and runtime versions. Equivalent active or successful jobs deduplicate, while
failed or budget-exceeded attempts remain retryable. A durable distributed lease ensures one active
reconciliation per repository.

The service tracks three checkpoints: `observed_head`, the newest default-branch head seen;
`reconciled_head`, the newest head fully analyzed and represented by either a no-op or the one open
docs PR; and `landed_head`, the newest head whose required docs are verified on the default branch.
`landed_head` advances after a no-op only when no earlier requirement remains open, or after a docs-PR
merge resolves every requirement through that head. Every job considers unresolved gaps in addition
to the new commit range. A rejected or closed docs PR marks its covered range blocked and leaves the
gap visible; later incremental jobs cannot strand or skip it, and the daily scan does not repeatedly
reopen a deliberately rejected PR without a manual retry or new relevant change.

A once-daily incremental delivery scan fetches the default head and recovers missed webhooks, direct
pushes, bypassed local hooks, and contributors who do not use Choobi. It does not repeatedly rescan
the whole repository. Full semantic audits run when Repository Choobi is enabled, when the guide or
schema changes, or when an administrator requests one; any later periodic full-audit schedule has a
separate explicit token budget.

The shared scheduler applies deterministic relevance before model calls and enforces per-repository
daily call and token caps. `budget_exceeded` leaves a visible gap and never advances
`reconciled_head`.

Repository Choobi runs in an isolated clone or worktree and never writes to a developer checkout or
directly commits to the default branch. Complete-fingerprint idempotency and the distributed lease
prevent duplicate jobs and duplicate token spend. The UI exposes all three checkpoints, next
delivery scan, open gaps, generated PRs, token usage, and typed failures.

At most one Repository Choobi docs PR is open per repository. Later merge ranges extend that PR
against the newest default-branch head and reverify its cumulative patch instead of creating PR
spam. If a human changes the same documentation, Choobi records a typed conflict and waits for
review; it never overwrites the human change or switches to a different target document.

When every teammate uses personal Choobi, most docs should already be current before merge. The
repository reconciler still verifies the merged truth once, so team-wide adoption improves latency
and precision but is not a correctness prerequisite.

## 13. Phased implementation plan

### Phase 1 — prove the update engine

Build a manual command for one repository and one runtime:

```text
choobi update --source-commit <sha>
```

Given a real commit, it must classify the need as create, update, or stay silent. In this phase it
executes existing-document updates, records would-create cases as `documentation_gap`, verifies each
change, and produces a docs-only commit using the source commit's exact message.

Exit criteria:

- the commit contains only allowed documentation paths;
- it never recursively invokes choobi;
- positive and negative historical diffs measure both precision and recall;
- deterministic verification rejects known-bad claims;
- path isolation, prompt-injection fixtures, secret scanning, recursion, and whole-patch failure
  tests pass before the command may write; and
- token and latency metrics are captured.

### Phase 2 — post-commit background operation

Add `choobi init`, the `post-commit` shim and prompt integration, per-repository serialization,
batched deterministic no-op filtering with relevant events kept distinct, guarded cherry-pick
attachment, and completion notification.

Exit criteria:

- the human commit returns without waiting for inference;
- a successful update creates exactly one docs commit;
- concurrent user commits or doc edits cannot be captured accidentally;
- an editor save after the final hash check and before attachment still causes Git to reject or
  abort the cherry-pick without capturing that content; and
- signing, rebase, merge, and runtime failures create no partial commit.

### Phase 3 — coding-agent chat integration

Ship one harness wrapper that passes the active conversation and source commit through the
versioned context schema. Validate README, build-plan, design, and PRD updates from real sessions.

### Phase 4 — personal UI and history

Ship the local SQLite store and the `choobi` window: onboarding, the blob, the document-centric home
with per-document update composers, and the **View changelog**, **View commands**, and **View style
guide** panels. The changelog must group by repository, sort by date, display patches and
verification evidence, and expose token usage without retaining chat transcripts. **View commands**
renders from the same source as `choobi help`.

### Phase 5 — local repository knowledge and document creation

Build the inferred repository profile, inspectable code-to-document map, typed documentation-gap
records, **View repository knowledge** UI, and per-repository opt-in. Automatic creation must fail
closed unless the type, writable path, audience, and owner are all deterministic and the new
document passes the same verification boundary as an update.

### Phase 6 — PR author and reviewer integration

Add local PR description annotation and the explicit reviewer-side `choobi update` path. Confirm
that choobi never claims success without an associated docs commit and never pushes another
person's branch automatically.

### Phase 7 — v1 hardening

Expand adversarial and race coverage, fixture-based relevance evaluation, installer packaging,
upgrade behavior, and recovery tooling for failed local jobs. Write-boundary safety is already an
exit criterion of the phase that first mutates documentation.

### Phase 8 — V2 shared guide and Repository Choobi

Add the committed repository guide, shared knowledge and event service, GitHub App, merge-triggered
incremental reconciliation, daily checkpoint backstop, isolated docs-PR writer, and team UI. Prove
idempotency across duplicate or missing webhooks before enabling automatic repository jobs.

## 14. Implementation decisions

Resolved (as built):

- **Stack**: Python for the CLI and engine; a native desktop window via pywebview (WKWebView on
  macOS) with a plain HTML/CSS/JS front end served on loopback with a per-launch token. Dependencies
  are PyYAML and pywebview. This is the "embedded local web UI" option from §8, in a native window.
- **Runtime and harness adapters**: both Claude Code (`claude -p`) and Codex (`codex exec`, run
  read-only) as runtimes, chosen with `choobi auth`; and one portable agent skill installed into both
  Claude Code and Codex for the chat path.

Still open:

- Cross-platform notification implementation.
- Exact documentation allowlist configuration format.
- Exact `choobi/guide.yaml` schema and migration policy.
- Which inferred V1 repository-profile corrections can be promoted into the V2 committed guide.
- Minimum evidence and confidence required before offering automatic new-document creation.
- Local history retention and patch-size limits.
- Repository Choobi schedule, stale-PR rollover policy, and token-budget defaults.
- How the local PR integration safely preserves an existing PR description while inserting the
  single choobi line.

These choices may change implementation details but must not change the v1 interaction contract.
