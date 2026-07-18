"""The coding-agent skill — how a user "talks to choobi" inside Claude Code or Codex.

Skills are a portable open standard: one SKILL.md works in Claude Code (~/.claude/skills),
Codex (~/.codex/skills), and any agent that reads the shared ~/.agents/skills tree. The
skill is instructions, not a script: it tells the agent to distill the relevant
conversation context and run `choobi update … --chat`, which is exactly the harness-wrapper
contract in build-plan §4 (the harness owns the conversation; choobi owns the reasoning).

`{CHOOBI}` is substituted at install time with the working invocation so the skill runs
whether or not `choobi` is on PATH.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from . import config

SKILL_NAME = "choobi"

_TEMPLATE = """\
---
name: choobi
description: >-
  Update repository documentation from the current conversation using the choobi CLI.
  Use when the user says "choobi update", or asks to update / sync a document (README,
  API docs, build plan, design doc, PRD) based on what was decided or what changed in
  this chat, or wants the docs kept consistent with recent work.
allowed-tools: Bash(choobi *), Bash(printf *), Bash(PYTHONPATH=* *)
---

# choobi — update docs from this conversation

choobi is a local documentation agent. Given a code diff and/or conversation context it
edits the smallest relevant part of a document, verifies it (links resolve, no secrets,
referenced paths exist), and commits a docs-only change. **You do not edit the document
yourself — choobi does the editing and committing.** Your job is to hand choobi the right
target and the right context, then report its result.

## Steps

1. **Target.** Decide which document the user means.
   - If they named one, pass it as a repo-relative path, or a short fuzzy name choobi will
     resolve (e.g. `api` for `docs/api.md`).
   - If they did not name one, omit the target and let choobi infer it from the diff.

2. **Context.** In a few lines, distill the RELEVANT decisions from this conversation:
   the behavior that changed, the decision made, files involved, unresolved questions.
   Leave out tool noise, secrets, and unrelated chatter — this is the evidence choobi
   reasons over.

3. **Scope.** Pick exactly one:
   - a specific commit is the source → `--commit <sha>` (choobi reuses that commit's message);
   - the change is only in the working tree → `--working`;
   - the update is driven purely by this conversation, with no single commit → `--detached`
     (choobi authors its own commit message).

4. **Run choobi**, piping the distilled context on stdin with `--chat`:

   ```bash
   printf '%s' 'DECISION: retries now default to 3 attempts with backoff
   CHANGED: src/api.py retry()' | {CHOOBI} update docs/api.md --chat --detached -- 'document the new retry default'
   ```

   Substitute the real target, scope flag, piped context, and the instruction after `--`.

5. **Report** choobi's output to the user verbatim — one line such as
   `choobi just updated the docs — …`, or its typed failure reason.

## Rules
- Never invent a target. If choobi returns `ambiguous_target` or `target_not_found`, ask
  the user which document they mean.
- One document per run. Run choobi again for a second document.
- Do not paste secrets into the context; choobi also scans outputs and refuses
  secret-shaped content.
"""


def _skill_body() -> str:
    return _TEMPLATE.replace("{CHOOBI}", config.invocation())


def _user_dirs() -> List[Path]:
    home = Path.home()
    # ~/.agents/skills is the portable, harness-agnostic location; the others are the
    # per-harness trees. Writing all three keeps the same skill valid across versions.
    return [home / ".claude" / "skills", home / ".codex" / "skills", home / ".agents" / "skills"]


def _project_dirs(root: Path) -> List[Path]:
    return [root / ".claude" / "skills", root / ".codex" / "skills", root / ".agents" / "skills"]


def install(scope: str = "user", root: Optional[Path] = None) -> List[str]:
    """Write the skill into every skills tree for the scope. Returns notes per file."""
    if scope == "project":
        if root is None:
            raise ValueError("project scope requires a repo root")
        bases = _project_dirs(root)
    else:
        bases = _user_dirs()
    body = _skill_body()
    notes: List[str] = []
    for base in bases:
        target = base / SKILL_NAME / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        notes.append(f"skill -> {target}")
    return notes
