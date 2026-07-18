# choobi

A local-first documentation agent that keeps engineering docs consistent with the work.
When you commit code, or ask it to from your coding agent, choobi finds the affected
document, edits the smallest relevant part, verifies it, and commits a docs-only change that
reuses your exact commit message. If nothing documented changed, it stays quiet.

choobi does **not** ship a model. It drives one, called the *runtime*: the authenticated
`claude` or `codex` CLI already on your machine. See [Runtime vs. harness](#runtime-vs-harness).

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [The window](#the-window)
- [Runtime vs. harness](#runtime-vs-harness)
- [How it works](#how-it-works)
- [Configuration and storage](#configuration-and-storage)
- [Development](#development)

## Requirements

- Python 3.9+ and `git`
- One runtime CLI installed: `claude` (default) or `codex`
- `gh` (optional, only for `choobi pr create`)

`pip install` pulls the two Python dependencies (PyYAML, and pywebview for the native
window). You do not need an API key: run `choobi auth claude` (or `choobi auth codex`) to
pick your runtime and log in. choobi delegates to that CLI's own login, which opens the
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
choobi auth claude        # once: pick your runtime model and log in (or: choobi auth codex)
choobi install            # once: install the "talk to choobi" skill into Claude Code + Codex
cd ~/code/my-project
choobi init               # per repo: install the post-commit hook + project skill
```

Then use it any of four ways. All compose the same `update` engine:

1. **Automatic.** Commit normally. choobi runs in the background and makes a docs-only
   follow-up commit when a doc needs it.
2. **From your coding agent.** In Claude Code or Codex say "choobi update the api doc based
   on what we decided", and the agent hands choobi the conversation context.
3. **Manual CLI.** `choobi update docs/api.md --detached -- "clarify the retry backoff"`
4. **The window.** `choobi` opens the local window (see [The window](#the-window)).

## Commands

```text
choobi                                        open the local window (alias: choobi ui)
choobi init                                   install post-commit hook + project skill
choobi install                                install the coding-agent skill (Claude Code + Codex)
choobi auth [claude|codex]                    pick the runtime model and log it in
choobi update [DOC...] [SCOPE] [-- TEXT]      run the documentation engine (the one verb)
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
choobi update  [TARGET...]  [SCOPE...]  [-- INSTRUCTION]

TARGET      zero: choobi infers the doc from the diff; one or more: you pin it (path or fuzzy name)
SCOPE       --commit <sha> | --range <a..b> | --pr <n>   (commit-anchored; reuses that message)
            --chat        (conversation context piped on stdin, supplied by the agent skill)
            --staged | --working | --detached  (--detached authors its own commit message)
INSTRUCTION free text after --, the natural-language "based on ..." that narrows the edit
```

With no commit-anchored scope and no `--detached`, choobi fails with a typed
`source_commit_required` rather than inventing a commit association.

## The window

`choobi` opens a native desktop window (pywebview / WKWebView), backed by a loopback server
with a per-launch token. It is configuration and inspection only; updates happen through
commits, the CLI, or the agent skill. Three tabs, plus two icons in the top right:

- **instructions**: the repos that ran `choobi init`. Open one to edit its **SOP** (per-repo
  documentation preferences that choobi acts on) or view and edit its **knowledge base**
  (choobi's generated map of the repo).
- **style**: the resolved style guide, editable, with save and return-to-default.
- **changelog**: repos, then their logs (titled by summary), then a log's full detail.
- **book icon**: the command reference.
- **terminal icon**: hover to see the current runtime and model.

## Runtime vs. harness

Two independent choices, easy to conflate:

- **Runtime**: the model choobi calls to do its own doc reasoning. Choose and log in with
  `choobi auth claude` or `choobi auth codex`. choobi delegates to that CLI's native login
  and records the choice in `~/.choobi/config.json`. It never stores credentials and never
  reads your repo's `.env`.
- **Harness**: the coding agent you are chatting in when you say "choobi update". The skill
  lets Claude Code or Codex call choobi. It does not share credentials with the runtime.

You can code in Codex while choobi uses Claude as its runtime, or any mix.

## How it works

```text
git post-commit ─┐
agent chat ──────┼─> scope → relevance gate → model → verify → docs commit → history
manual / CLI ────┘
```

- **Relevance gate** (deterministic, no model call): if nothing is linked to the change and
  no new source appeared, choobi records a cheap no-op and stops.
- **Linkage** finds the docs a change concerns, cheapest first: `covers:` front matter,
  README directory ownership, and literal path mentions.
- **Recall backbone** catches what linkage misses. New source files (added in the commit, or
  drifted since the last snapshot) that no doc owns are surfaced to the model as create
  candidates. If nothing is linked but source changed, one bounded model pass picks the
  owning doc, or create, or none. After writing, choobi records the code a doc now covers in
  its `covers:` front matter, so next time linkage finds it without a model pass.
- **Write boundary** (the verifier): the output path must be in the allowlist, links and
  referenced paths must resolve, content is secret-scanned, the target's hash must be
  unchanged since choobi read it, an update may not drop more than one section, and the
  commit contains only the doc. Any failure aborts the whole patch. Nothing partial commits.
- **Recursion guard**: the docs commit is created with a `CHOOBI_GENERATING` marker so its
  own post-commit hook exits immediately.

## Configuration and storage

Everything lives under `~/.choobi` (override with `CHOOBI_HOME`):

```text
~/.choobi/
  config.json            name, runtime agent, optional API key, per-repo create opt-in
  style.md               personal style override (falls back to the built-in baseline)
  choobi.db              SQLite: activity records, checkpoints, and the repo registry
  repos/<checkout-id>/
    sop.md               per-repo documentation preferences (editable; choobi acts on it)
    knowledge.md         choobi's generated map of the repo (editable)
    snapshot.json        the source files choobi last reconciled (drift detection)
    update.lock          per-repo lock
  logs/hook.log          background hook output
```

The writable documentation surface (allowlist) defaults to `README.md`, `docs/**/*.md`, and
`*-plan.md`. Add `covers: path/glob` front matter to a doc to link it to the code it
documents. A repo's SOP can allow choobi to create new docs and describes where they go.

## Development

```bash
python3 -m unittest discover -s tests    # unit tests, no tokens (FakeRuntime)
python3 -m choobi.evaluate               # live precision/recall/silence over fixtures
```

`CHOOBI_RUNTIME=fake` with `CHOOBI_FAKE_RESPONSE='{...}'` drives deterministic runs without a
model. See `build-plan.md` for the full product and implementation contract.
