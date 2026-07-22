# How Choobi works

This document describes Choobi's execution model, model boundary, document-selection pipeline,
safety checks, storage, current limits, and contributor workflow. For installation and everyday
usage, start with the [README](README.md).

## System overview

Every entry point uses the same update engine:

```text
git post-commit ─┐
agent chat ──────┼─> scope → full-doc ownership → one-doc edit or flag → verify → history
manual CLI ──────┘
```

The post-commit hook supplies the commit that just landed. The coding-agent skill can add
conversation evidence. A manual command supplies a commit, range, pull request, or explicitly
detached working-tree scope.

Choobi's update grammar is:

```text
choobi update  [TARGET]  [SCOPE]  [-- INSTRUCTION]

TARGET      zero: Choobi infers the doc from the diff; one: pin it by path or fuzzy name
SCOPE       --commit <sha> | --range <a..b> | --pr <n>
            or --detached, optionally with --staged or --working
CONTEXT     --chat reads conversation evidence from stdin and can accompany either scope
INSTRUCTION free text after -- that narrows the requested edit
```

Without a commit-based scope or `--detached`, Choobi returns `source_commit_required` instead of
inventing a commit association.

## Runtime vs. harness

The runtime and harness are independent choices:

- **Runtime** is the model Choobi calls for documentation reasoning. V1 supports the authenticated
  Claude and Codex CLIs. `choobi auth claude` and `choobi auth codex` delegate authentication to
  the selected CLI; Choobi does not store its credentials.
- **Harness** is the coding agent in which a user says “choobi update.” The installed skill lets
  Claude Code or Codex invoke the Choobi CLI and provide relevant conversation evidence. Harness
  credentials are not shared with the runtime.

Choobi persists exactly one active runtime in `config.json`. When a user selects another runtime,
Choobi first checks that CLI's existing login and otherwise launches its browser flow. Only a
successful status check changes the active runtime. A missing CLI, cancelled browser flow, or
failed login preserves the previous selection. Choobi does not log out or modify the other CLI's
own account session; “one active runtime” refers to the one runtime Choobi will invoke.

The adapters apply the strongest isolation their CLIs expose:

- **Claude** runs with a native system prompt and JSON schema, with tools, runtime customizations,
  and session persistence disabled.
- **Codex** runs through `codex exec` in an empty temporary working directory with a read-only
  sandbox, `approval_policy="never"`, schema-constrained output, ephemeral sessions, and user
  config and exec rules ignored. Tool subprocesses inherit no environment variables. Codex does
  not expose a separate system-prompt flag, so Choobi sends its contract and evidence together in
  one explicitly delimited input and instructs the runtime not to call tools.

Either harness can invoke either runtime. For example, you can code in Codex while Choobi uses
Claude, or code in Claude Code while Choobi uses Codex.

Local-first describes control and storage, not on-device inference. Repository evidence included
in a prompt is passed to the selected runtime and is handled under that runtime's service terms.
Choobi never reads a repository's `.env`, and it secret-scans prompt inputs and proposed output.

## Selecting the document owner

Choobi gives every nontrivial implementation, UI, configuration, or other non-document commit a
full-context ownership review. Only a narrow deterministic gate can skip model judgment: test-only
changes, docs-only changes, lockfiles, and recognized generated output.

The ownership call receives:

- the complete cross-file diff;
- current snapshots of the changed files;
- the repository SOP; and
- every Git-tracked Markdown and MDX document in full.

Cheap signals such as `covers:` front matter, README directory ownership, and literal path
mentions are hints, not final gates. They do not bypass the model review.

Roadmaps, proposals, plans, and statements marked future or not yet implemented are intent, not
evidence that the behavior exists. Missing planned behavior is not a conflict. If changed code
appears to make a product or architecture decision that conflicts with future intent, the ownership
pass selects that document so the editing review can request owner judgment instead of silently
rewriting the plan.

### Repository-specific areas

Choobi infers the repository's own conceptual areas instead of imposing a universal taxonomy. A
repository might be divided into backend, frontend UI, operations, integrations, or another shape
that better reflects its code and documentation. A feature-wide change is marked cross-cutting so
it can select a broader owner.

Priority behaviors—including retention, deletion, privacy, authentication, permissions,
credentials, telemetry, security, and user-visible configuration—are called out in the default SOP
and ownership prompt. They still follow the same full-context review.

### Complete-document batching

If the diff, SOP, changed-file evidence, and all documents fit, Choobi sends them in one ownership
call. If they do not fit, Choobi:

1. splits the corpus into bounded batches of complete documents;
2. asks each batch for possible owners;
3. makes a final selection over the shortlisted documents in full; and
4. passes the selected document in full to the editing call.

Choobi never truncates or splits an individual document. A prompt that cannot fit without dropping
required evidence fails with `context_too_large`.

### Read and write boundaries

Every tracked Markdown or MDX file can participate in ownership review, but only allowlisted files
are writable. A generated or read-only true owner receives a flag-or-silent editing review because
neither outcome writes the file. If the model instead requests an update, Choobi records a visible
`documentation_gap`; it does not silently update a weaker substitute such as the root README.

The recall backbone also surfaces new source files—either added in the current commit or detected
since the last snapshot—that no document owns. After a successful edit, `covers:` front matter can
record the changed inputs that the document now owns as a useful signal for later reviews.

## Editing and verification

Once an owner is selected, the editing call receives the complete diff, the selected document in
full, changed-file evidence, relevant chat decisions, placement policy, the repository SOP, and the
resolved style guide. It is instructed to make the smallest edit that brings the document back into
agreement with the evidence.

The editing call may instead return `flag` with an LLM-written owner-review message. A valid flag
names the selected future-intent document and the concrete conflict, but contains no replacement
content or source paths. Choobi records `future_direction_conflict`, advances the checkpoint, and
writes no document or commit. If code implements rather than conflicts with the planned direction,
Choobi may update the document's implementation status normally.

A flag takes precedence if the same code change also makes current-state prose stale. Choobi does
not partially update that prose while leaving a product-direction conflict unresolved.

Verification is the write boundary. Before Choobi writes anything, it checks that:

- the output path is in the documentation allowlist, resolves inside the repository, and is not a
  symlink;
- inline relative Markdown links stay inside the repository and resolve;
- every `covers:` entry matches a Git-tracked path;
- an update preserves existing front matter and every still-valid coverage entry;
- prompt inputs and proposed content contain no secret-shaped strings;
- a new document is permitted by the SOP and lies under one of its `create_roots`;
- every code block in a newly created document appears verbatim in source or conversation evidence;
- the target is clean and still has the content Choobi originally read;
- a new target path is still absent;
- the edit does not drop more than one existing Markdown section; and
- no merge, rebase, cherry-pick, or other conflicting Git operation is in progress.

These checks do not execute examples or prove arbitrary prose claims. Planned verification work
includes matching referenced symbols and signatures against source and running repository-declared
safe checks.

Any verification failure aborts the complete patch. Choobi never removes the failing part and
commits the remainder.

## Git and concurrency model

Choobi creates each documentation commit in an isolated temporary worktree. It rechecks the live
branch and target, then attaches the prebuilt commit with one guarded cherry-pick. It never stages
unrelated changes in the user's working tree.

Before attaching the commit, Choobi verifies that:

- the source commit is still an ancestor of the active branch;
- every existing target still has the content hash used during generation;
- every proposed new-document path is still absent;
- the repository is not in the middle of another Git mutation; and
- every output path remains inside the documentation allowlist.

A conflict aborts the cherry-pick and leaves the pending reference available for diagnosis. The
docs commit carries a `CHOOBI_GENERATING` marker so its own post-commit hook exits immediately.

Background work serializes through one advisory lock per repository. `choobi init` refuses to
overwrite an unmanaged post-commit hook, and `choobi pr create` refuses to run while an update owns
the repository lock.

## Coding-agent context

The portable skill passes distilled context from the active coding-agent conversation on standard
input with `--chat`. The context can contain user decisions, implementation facts, mentioned files,
and unresolved questions that source code alone may not reveal.

Choobi does not scrape private agent session databases or guess which chat the user intended. It
does not persist the original transcript in activity history. A conversation-only update must be
explicitly `--detached`; an anchored update can associate the docs commit with a source commit and
reuse that commit's exact message.

## Configuration and local storage

Choobi stores local state under `~/.choobi`. Set `CHOOBI_HOME` to relocate the entire tree.

```text
~/.choobi/
  config.json            name, runtime, mode, and onboarding state
  style.md               complete personal style guide, when customized
  choobi.db              activity records, checkpoints, and repository registry
  repos/<checkout-id>/
    sop.md               per-repository documentation preferences
    knowledge.md         generated, read-only repository map
    snapshot.json        source files last reconciled
    update.lock          per-repository advisory lock
  logs/hook.log          background hook output
```

The checkout ID hashes Git's common directory. Linked worktrees share repository state, while
forks and unrelated repositories remain distinct.

### Style

Choobi ships an immutable baseline style guide. The UI initially displays that document in full.
Saving edits creates a complete personal `~/.choobi/style.md`; returning to default deletes the
personal copy and reloads the bundled guide. Style preferences can influence voice, terminology,
structure, and verbosity, but cannot weaken safety rules or decide repository placement.

### Repository SOP and knowledge base

The SOP is human-authored guidance that Choobi acts on. It can describe documentation priorities,
placement, and repository-specific expectations. New-document creation is off by default and
requires both `allow_create: true` and a non-empty `create_roots` list.

The knowledge base is a deterministic, read-only map of writable documents, their `covers:`
entries, top-level code areas without documentation coverage, and recent activity. Regenerating it
does not call a model.

The default writable surface is `README.md`, `HOW_CHOOBI_WORKS.md`, `docs/**/*.md`, and
`*-plan.md`.

## Activity and pull requests

Choobi records committed updates, no-ops, future-direction flags, documentation gaps, and failures
in its local SQLite history. The CLI and desktop changelog expose the decision, source and docs
commits, summary, reason, duration, changed documents, and exact patch when available.

`choobi pr create` delegates pull-request creation to the authenticated GitHub CLI. When the branch
contains a successful Choobi docs commit associated with the source range, Choobi adds this line to
the pull-request description:

```text
choobi updated docs.
```

A pull request created directly in a browser or with `gh pr create` cannot wake the local process.
Automatically covering every pull-request creation path requires a future hosted integration.

## Current limits

Choobi V1 intentionally updates one canonical document per run. It does not yet provide:

- a durable event queue or crash recovery;
- coalescing and ordered replay of background commit events;
- operating-system completion notifications;
- routing owner-review flags to a named CODEOWNER, chat user, or hosted service;
- acknowledging or resolving an owner-review flag as active state rather than historical activity;
- multi-document reconciliation;
- result caching, cancellation, or global scheduling; or
- token, cost, call-budget, or daily-budget accounting.

A failed or interrupted automatic run remains visible in history if it reached the engine. Rerun
`choobi update` for work that never reached it. The current hook writes background output to
`~/.choobi/logs/hook.log` because a detached process cannot safely print after the terminal prompt
has returned.

The full prompt has a 100,000-byte UTF-8 ceiling. Choobi fails with `context_too_large` rather than
truncating a diff, SOP, changed file, document, or editing target and pretending the repository was
fully reconciled.

## Development and evaluation

Run the unit suite with the deterministic fake runtime:

```bash
python3 -m unittest discover -s tests
```

Run the fixture evaluation against the configured live runtime:

```bash
python3 -m choobi.evaluate
```

The evaluation builds isolated synthetic Git repositories and tests changes that should update a
specific document, remain silent, or create a document when policy permits. It reports decision
accuracy, write precision, recall, correct silence, required-fact recall, forbidden-claim rate,
preservation, and per-fixture changed-line ceilings. Scenarios cover semantic linkage, retention
configuration, environment requirements, UI workflows, public API changes, generated docs,
feature gates, and removal of documented behavior.

For deterministic engine runs outside the test suite, set `CHOOBI_RUNTIME=fake` and provide a
schema-shaped response in `CHOOBI_FAKE_RESPONSE`.

See [build-plan.md](build-plan.md) for the complete product and implementation contract.
