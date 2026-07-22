"""The command reference — one source rendered by both `choobi help` and the UI panel,
so the two can never drift (build-plan §4.1).
"""
from __future__ import annotations

from typing import List, TypedDict


class Command(TypedDict):
    command: str
    summary: str
    detail: str


COMMANDS: List[Command] = [
    {"command": "choobi",
     "summary": "open the local window",
     "detail": "Launches the choobi window — a native desktop window (alias: choobi ui)."},
    {"command": "choobi init",
     "summary": "install the post-commit hook + project skill",
     "detail": "Installs a git post-commit hook in the current repo so choobi runs "
               "automatically after each human commit, plus a project-scope agent skill."},
    {"command": "choobi install",
     "summary": "install the coding-agent skill (Claude Code + Codex)",
     "detail": "Writes the portable choobi skill into your user skills trees "
               "(~/.claude/skills, ~/.codex/skills, ~/.agents/skills) so you can say "
               "\"choobi update <doc> based on …\" inside Claude Code or Codex and it acts "
               "on the conversation."},
    {"command": "choobi auth [claude|codex]",
     "summary": "pick the runtime model and log it in",
     "detail": "With no argument, shows runtime status. With claude or codex, authenticates "
               "that CLI if needed and makes it Choobi's one active runtime. A failed switch "
               "leaves the previous runtime active. Choobi delegates auth and never stores "
               "your credentials."},
    {"command": "choobi update [DOC] SCOPE [--chat] [-- INSTRUCTION]",
     "summary": "run the documentation engine",
     "detail": "The one engine verb. DOC pins a target (path or fuzzy name); omit it to let "
               "choobi infer from the diff. SCOPE is --commit <sha>, --range <a..b>, "
               "--pr <number>, or --detached. Uncommitted input uses --detached --staged or "
               "--detached --working. --chat reads conversation evidence from stdin. Text after "
               "-- is an instruction, e.g. choobi update docs/api.md --detached -- \"clarify the backoff\"."},
    {"command": "choobi status",
     "summary": "show pending / failed / no-op jobs and the repo checkpoint",
     "detail": "A deterministic read of local state. No model call."},
    {"command": "choobi docs",
     "summary": "list the docs choobi can update in this repo",
     "detail": "Lists the writable documents (README, docs/**, *-plan.md) and the code each "
               "declares via its covers: front matter."},
    {"command": "choobi changelog [-n N] [--all] [--status S]",
     "summary": "browse choobi's activity log",
     "detail": "Newest-first list of runs for this repo. -n limits count, --all spans every "
               "repo, --status filters to committed / no_op / flagged / failed."},
    {"command": "choobi show <id>",
     "summary": "show one changelog entry in full",
     "detail": "Prints an activity record's commits, summary, reason, and the exact "
               "documentation patch."},
    {"command": "choobi style",
     "summary": "print the resolved style guide",
     "detail": "Shows the immutable baseline followed by any personal overrides."},
    {"command": "choobi pr create",
     "summary": "create a PR via gh and annotate it",
     "detail": "Refuses while a docs update is active, opens the PR with the authenticated gh CLI, and "
               "inserts the line 'choobi updated docs.' when a docs commit exists."},
    {"command": "choobi help [COMMAND]",
     "summary": "this command reference",
     "detail": "Shows all commands, or details for one."},
]


def render(command: str = "") -> str:
    if command:
        for c in COMMANDS:
            if c["command"].split()[1 if " " in c["command"] else 0] == command or \
               c["command"].startswith("choobi " + command):
                return f"{c['command']}\n  {c['summary']}\n\n  {c['detail']}"
        return f"no such command: {command}"
    width = max(len(c["command"]) for c in COMMANDS)
    lines = ["choobi — local-first documentation agent\n"]
    for c in COMMANDS:
        lines.append(f"  {c['command'].ljust(width)}   {c['summary']}")
    lines.append("\nrun `choobi help <command>` for detail.")
    return "\n".join(lines)
