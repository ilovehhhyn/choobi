# choobi

**Docs that follow your work.**

Choobi is a documentation agent for Git repositories. After you commit a code
change, Choobi decides whether the change belongs in your docs and, when it does, creates a
small docs-only follow-up commit. If the docs are already accurate, Choobi stays quiet.

You can also talk to Choobi from Claude Code or Codex or run commands directly from the CLI; you can inspect every decision in a native desktop window.

<img width="381" height="564" alt="Screenshot 2026-07-23 at 8 11 44 PM" src="https://github.com/user-attachments/assets/85cd8d57-a5a0-4fd9-be3f-dcab71b1645b" />


## Highlights

- **Works with your Git workflow.** Initialize choobi in a repository once and you are good to go! work and commit normally.
- **Finds the right document.** Choobi considers the complete change and the repository's
  Markdown documentation before choosing an owner.
- **Keeps edits focused.** It updates the smallest relevant part of one document and rejects
  changes that cross its write boundary.
- **Protects future direction.** It treats plans as intent, not shipped behavior, and leaves them
  unchanged while surfacing an LLM-written owner-review message when code contradicts them.
- **Fits your coding setup.** Use the automatic hook, a coding-agent command, or the CLI.
- **Leaves an audit trail.** Every update, flag, no-op, and failure is available in the CLI and UI
  changelog.

## Prerequisites

- Python 3.9 or newer
- Git
- At least one supported runtime CLI installed and available on `PATH`: `claude` or `codex`
- Optional: the GitHub CLI (`gh`) for `choobi pr create`

Choobi delegates authentication to the selected runtime CLI and does not store runtime
credentials. Both `choobi auth claude` and `choobi auth codex` open that CLI's browser login when
needed.

## Install

Choobi is currently installed from source:

```bash
git clone https://github.com/ilovehhhyn/choobi.git
cd choobi
python3 -m pip install -e .
choobi help
```

If your shell cannot find `choobi` after installation, add Python's user scripts directory to
your `PATH`. From the source checkout, you can also run commands as `python3 -m choobi`.

## Quickstart

Set up Choobi once:

```bash
choobi auth claude        # or: choobi auth codex
choobi install
```

Choobi uses exactly one active runtime. Authenticating another runtime switches to it only after
the new login succeeds; a cancelled or failed login leaves the previous runtime active. Having one
authenticated runtime is enough.

Then initialize each repository you want Choobi to follow:

```bash
cd ~/code/my-project
choobi init
```

Now commit code as usual. The installed post-commit hook runs Choobi in the background. When a
documentation update is warranted, Choobi creates a docs-only follow-up commit with the same
commit message. Otherwise, it records a no-op and leaves the repository unchanged. If code
contradicts a documented future direction, Choobi records a flag for owner review and makes no doc
change.

**There are three ways to ask Choobi to work:**

1. **Commit normally.** The post-commit hook reviews the change automatically.
2. **Ask your coding agent.** In Claude Code or Codex, say something like: “choobi update the
   API docs based on what we decided.”
3. **Run the CLI.** For example, `choobi update --commit HEAD` reviews the latest commit.

## Commands

| Command | What it does |
| --- | --- |
| `choobi` | Open the native desktop window. Alias: `choobi ui`. |
| `choobi init` | Install the post-commit hook and project skill in the current repository. |
| `choobi install` | Install the Choobi skill for Claude Code and Codex. |
| `choobi auth [claude\|codex]` | Show runtime status, or authenticate and select one active runtime. |
| `choobi update [DOC] SCOPE [--chat] [-- TEXT]` | Run a documentation review, optionally pinned to one document. |
| `choobi status` | Show pending, flagged, failed, and no-op work plus the repository checkpoint. |
| `choobi docs` | List the documents Choobi can update in the current repository. |
| `choobi changelog [-n N] [--all] [--status S]` | Browse recent Choobi activity. |
| `choobi show <id>` | Show one activity record and its exact patch. |
| `choobi style` | Print the resolved documentation style guide. |
| `choobi pr create` | Create a pull request with `gh` and annotate it when Choobi updated docs. |
| `choobi help [COMMAND]` | Show the full command reference or help for one command. |

### Manual update examples

Let Choobi choose the document for the latest commit:

```bash
choobi update --commit HEAD
```

Pin the review to a document or review a larger scope:

```bash
choobi update docs/api.md --commit HEAD
choobi update --range main..HEAD
choobi update --pr 42
```

Review staged or working-tree changes without associating them with a commit:

```bash
choobi update --detached --staged
choobi update --detached --working
choobi update docs/api.md --detached -- "clarify the retry backoff"
```

A manual update must have one commit-based scope (`--commit`, `--range`, or `--pr`) or use
`--detached`. Run `choobi help update` for the complete grammar.

## Use the desktop window

Launch the window from any terminal:

```bash
choobi
```

On first launch, enter your name, choose Claude or Codex, and select the sign-in button. Choobi
opens the chosen runtime's browser login when needed and returns to the app when authentication
succeeds.

The window is for configuration and inspection:

- **instructions** — choose a repository that has run `choobi init`, edit its SOP, or view and
  regenerate Choobi's read-only knowledge base for that repository.
- **style** — edit the complete default `style.md` as your personal style guide, or use
  **return to default** to discard your copy and reload the bundled version.
- **changelog** — watch Choobi's work, open individual runs, and inspect summaries, reasons,
  commit hashes, and patches.
- **book icon** — open the command reference.
- **terminal icon** — hover to see the current runtime and readiness. Click it to authenticate and
  switch to Claude or Codex. The current runtime changes only after the new runtime is ready.

Updates themselves run through commits, the CLI, or the coding-agent skill; the window does not
start an update.

## Choose what Choobi can edit

By default, Choobi can update `README.md`, `HOW_CHOOBI_WORKS.md`, Markdown files under `docs/`,
and `*-plan.md` files. Use the repository SOP in the **instructions** tab to describe what should
be documented and where. New-document creation is disabled until the SOP explicitly enables it
and declares the allowed destination directories.

Use the **style** tab for preferences that should apply across repositories, such as voice,
terminology, structure, and verbosity.

## Learn more

- [How Choobi works](HOW_CHOOBI_WORKS.md) — runtime vs. harness, document ownership, safety
  boundaries, local storage, current limits, and development.
- [Build plan](build-plan.md) — the full product and implementation contract.
