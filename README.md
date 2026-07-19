# choobi - docs that follow your work 

Choobi is a doc writing agent that keeps engineering docs consistent with the code.
When you commit code or summon choobi in your claude or codex CLI, choobi finds the affected
document, edits the smallest relevant part, checks the write boundary, and commits a docs-only
change that reuses your exact commit message. If nothing documented changed, it stays quiet.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [The window](#the-window)
- [Runtime vs. harness](#runtime-vs-harness)
- [How it works](#how-it-works)
- [Configuration and storage](#configuration-and-storage)
- [Current limits](#current-limits)
- [Development](#development)

## Requirements

- Python 3.9+ and `git`
- The `claude` CLI installed as Choobi's tool-free runtime
- `gh` (optional, only for `choobi pr create`)

`pip install` pulls the two Python dependencies (PyYAML, and pywebview for the native
window). You do not need an API key: run `choobi auth claude` to
log in. choobi delegates to that CLI's own login, which opens the
browser, and never stores your credentials. If the CLI is already logged in, choobi uses it.

## Install

```bash
git clone <repo> && cd choobi
python3 -m pip install -e .
```

If pip warns that the `choobi` script is not on PATH, add its bin directory (macOS example),
then open a new terminal:

```bash
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc
```

Verify with `choobi help`.

## Quickstart

```bash
choobi auth claude        # once: select the tool-free runtime and log in
choobi install            # once: install the "talk to choobi" skill into Claude Code + Codex
cd ~/code/my-project
choobi init               # per repo: install the post-commit hook + project skill
```

Then use it in any of three ways. All compose the same `update` engine:

1. **Automatic.** Commit normally. choobi runs in the background and makes a docs-only
   follow-up commit when a doc needs it.
2. **From your coding agent.** In Claude Code or Codex say "choobi update the api doc based
   on what we decided", and the agent hands choobi the conversation context.
3. **Manual CLI.** `choobi update docs/api.md --detached -- "clarify the retry backoff"`

## Commands

```text
choobi                                        open the local window (alias: choobi ui)
choobi init                                   install post-commit hook + project skill
choobi install                                install the coding-agent skill (Claude Code + Codex)
choobi auth [claude]                          select the runtime and log it in
choobi update [DOC] SCOPE [--chat] [-- TEXT]  run the documentation engine (the one verb)
choobi status                                 pending / failed / no-op jobs + repo checkpoint
choobi docs                                   list the docs choobi can update in this repo
choobi changelog [-n N] [--all] [--status S]  browse the activity log
choobi show <id>                              show one changelog entry in full (with the patch)
choobi style                                  print the resolved style guide
choobi pr create                              open a PR via gh and annotate it
choobi help [COMMAND]                         command reference
```

### The `update` grammar

`update` is polymorphic over three inputs. Every caller fills the same slots:

```text
choobi update  [TARGET]  [SCOPE]  [-- INSTRUCTION]

TARGET      zero: choobi infers the doc from the diff; one: you pin it (path or fuzzy name)
SCOPE       exactly one commit anchor: --commit <sha> | --range <a..b> | --pr <n>
            or --detached, optionally with --staged or --working
CONTEXT     --chat reads conversation evidence from stdin and may accompany either scope
INSTRUCTION free text after --, the natural-language "based on ..." that narrows the edit
```

With no commit-anchored scope and no `--detached`, choobi fails with a typed
`source_commit_required` rather than inventing a commit association.

## The window

`choobi` opens a native desktop window (pywebview / WKWebView), backed by a loopback server
with a per-launch token. It is configuration and inspection only; updates happen through
commits, the CLI, or the agent skill. Three tabs, plus two icons in the top right:

- **instructions**: the repos that ran `choobi init`. Open one to edit its **SOP** (per-repo
  documentation preferences that choobi acts on) or view and regenerate its read-only
  **knowledge base** (choobi's derived map of the repo).
- **style**: the complete default `style.md`, editable as a personal copy and resettable at any time.
- **changelog**: repos, then their logs (titled by summary), then a log's full detail.
- **book icon**: the command reference.
- **terminal icon and footer**: show whether the Claude runtime is installed and authenticated.

## Runtime vs. harness

Two independent choices, easy to conflate:

- **Runtime**: the model choobi calls to do its own doc reasoning. V1 uses Claude through a
  native system prompt and JSON schema with all tools, customizations, and session persistence
  disabled. `choobi auth claude` delegates login to the CLI. Choobi never stores credentials
  or reads your repo's `.env`.
- **Harness**: the coding agent you are chatting in when you say "choobi update". The skill
  lets Claude Code or Codex call choobi. It does not share credentials with the runtime.

You can code in Codex while Choobi uses Claude as its runtime. Codex's current non-interactive
CLI cannot provide Choobi's required tool-free system boundary, so it is not a V1 runtime.

## How it works

```text
git post-commit ─┐
agent chat ──────┼─> scope → full-doc ownership → one-doc edit → verify → docs commit → history
manual / CLI ────┘
```

- **Full-context ownership review** runs for every nontrivial implementation, UI, configuration,
  or other non-document commit. The model receives the complete diff, current changed-file
  snapshots, repository SOP, and every Git-tracked Markdown/MDX document in full. `covers:`, README
  directory ownership, and literal path mentions are inexpensive hints; they never bypass review.
- **Repo-specific areas** are inferred from each repository rather than imposed globally. Choobi
  groups the change conceptually into areas such as backend, frontend UI, operations, or whatever
  fits that repo, and marks feature-wide changes as cross-cutting so they can select a broader
  owner.
- **Complete-document batching** preserves context when everything does not fit one call. Choobi
  splits whole documents into bounded batches, asks each batch for possible owners, and makes a
  final selection over the shortlisted documents in full. It never truncates or splits a document.
- **Read and write boundaries stay distinct**: every tracked Markdown/MDX file participates in
  ownership review, but only allowlisted documents are writable. A selected read-only or generated
  true owner becomes a visible `documentation_gap`; Choobi does not silently substitute a weaker
  README. Tests, docs-only changes, lockfiles, and recognized generated output retain a narrow
  deterministic skip.
- **Recall backbone** also surfaces new source files (added in the commit, or drifted since the
  last snapshot) that no document owns. After writing, Choobi records the changed inputs a doc now
  covers in front matter as a useful hint for later reviews.
- **Write boundary** (the verifier): the output path must be in the allowlist, inline relative
  links must resolve inside the repository, `covers:` entries must match tracked files, existing
  front matter and live coverage must survive an update, prompt inputs and output are
  secret-scanned, the target must be clean and unchanged, and an update may not drop more than one
  section. A created code block must exist verbatim in validated source content or conversation
  evidence. These checks do not execute examples or prove arbitrary prose claims. Any failure
  aborts the whole patch.
- **Write isolation**: every update builds a docs commit in a temporary
  worktree, rechecks the live branch and target, then attaches it with one guarded cherry-pick.
- **Recursion guard**: the docs commit is created with a `CHOOBI_GENERATING` marker so its
  own post-commit hook exits immediately.

## Configuration and storage

Everything lives under `~/.choobi` (override with `CHOOBI_HOME`):

```text
~/.choobi/
  config.json            name, runtime, mode, and onboarding state
  style.md               complete personal style guide (defaults to the bundled copy)
  choobi.db              SQLite: activity records, checkpoints, and the repo registry
  repos/<checkout-id>/
    sop.md               per-repo documentation preferences (editable; choobi acts on it)
    knowledge.md         choobi's generated, read-only map of the repo
    snapshot.json        the source files choobi last reconciled (drift detection)
    update.lock          per-repo lock
  logs/hook.log          background hook output
```

The writable documentation surface (allowlist) defaults to `README.md`, `docs/**/*.md`, and
`*-plan.md`. Add `covers: path/glob` front matter to a doc to link it to the code it
documents. New-document creation is off by default. A repo's SOP must explicitly set
`allow_create: true` and declare a non-empty `create_roots` list.

## Current limits

Choobi V1 intentionally updates one canonical document per run. Background processes serialize on
a per-repository lock, but there is not yet a durable event queue, crash recovery, OS completion
notification, multi-document reconciliation, or token and cost accounting. A failed or interrupted
automatic run remains visible in history when it reached the engine; rerun `choobi update` for work
that never reached it. These are release blockers for calling Choobi a production-grade autonomous
documentation system, not hidden alternate modes. Prompts over 100,000 UTF-8 bytes fail with
`context_too_large`; Choobi does not truncate evidence and then pretend it reconciled the change.

## Development

```bash
python3 -m unittest discover -s tests    # unit tests, no tokens (FakeRuntime)
python3 -m choobi.evaluate               # live decisions, fact/preservation recall, forbidden probes
```

`CHOOBI_RUNTIME=fake` with `CHOOBI_FAKE_RESPONSE='{...}'` drives deterministic runs without a
model. See `build-plan.md` for the full product and implementation contract.
