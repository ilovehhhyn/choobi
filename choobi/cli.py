"""Command dispatch. Thin: every command composes engine/status/help/hooks/pr and prints.

The `update` grammar splits the natural-language instruction after a standalone `--`
before argparse ever sees it, so options and instruction never collide.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import (
    agent_skill, auth, config, engine, gitio, help as help_mod, history, hooks, locking, pr,
    status, views,
)
from .errors import ChoobiError, InvalidScope, PendingDocsUpdate, SourceCommitRequired
from .runtime import get_runtime

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _range_for(root: Path, sha: str) -> str:
    try:
        gitio.resolve(root, sha + "^")
        return f"{sha}^..{sha}"
    except RuntimeError:
        return f"{EMPTY_TREE}..{sha}"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="choobi", add_help=False)
    sub = p.add_subparsers(dest="cmd")

    u = sub.add_parser("update", add_help=False)
    u.add_argument("targets", nargs="*")
    u.add_argument("--commit")
    u.add_argument("--range")
    u.add_argument("--pr", type=int)
    u.add_argument("--staged", action="store_true")
    u.add_argument("--working", action="store_true")
    u.add_argument("--chat", action="store_true")
    u.add_argument("--detached", action="store_true")
    u.add_argument("--trigger", default="manual")

    s = sub.add_parser("status", add_help=False)
    h = sub.add_parser("help", add_help=False)
    h.add_argument("topic", nargs="?", default="")

    sub.add_parser("docs", add_help=False)
    cl = sub.add_parser("changelog", add_help=False)
    cl.add_argument("-n", "--limit", type=int, default=30)
    cl.add_argument("--all", action="store_true")
    cl.add_argument("--status", choices=["committed", "no_op", "flagged", "failed"])
    sh = sub.add_parser("show", add_help=False)
    sh.add_argument("id", type=int)
    sub.add_parser("style", add_help=False)
    sub.add_parser("init", add_help=False)
    sub.add_parser("install", add_help=False)
    au = sub.add_parser("auth", add_help=False)
    au.add_argument("runtime", nargs="?", choices=sorted(auth.RUNTIMES))
    sub.add_parser("ui", add_help=False)
    prp = sub.add_parser("pr", add_help=False)
    prp.add_argument("pr_cmd", choices=["create"])
    return p


def _validate_update_args(args: argparse.Namespace) -> None:
    if len(args.targets) > 1:
        raise InvalidScope("Choobi v1 updates exactly one document per run")
    anchored_count = sum(value is not None for value in (args.commit, args.range, args.pr))
    anchored = anchored_count == 1
    if anchored_count > 1:
        raise InvalidScope("choose exactly one of --commit, --range, or --pr")
    if args.detached and anchored:
        raise InvalidScope("--detached cannot be combined with --commit, --range, or --pr")
    if args.staged and args.working:
        raise InvalidScope("choose --staged or --working, not both")
    if anchored and (args.staged or args.working):
        raise InvalidScope("commit-anchored scope cannot be combined with --staged or --working")
    if not anchored and not args.detached:
        raise SourceCommitRequired("uncommitted and chat updates require --detached")


def _cmd_update(args: argparse.Namespace, instruction: Optional[str]) -> int:
    _validate_update_args(args)
    root = gitio.repo_root(Path.cwd())
    cfg = config.Config.load()
    rt = get_runtime(cfg)

    source_commit = None
    rev_range = None
    if args.commit:
        source_commit = gitio.resolve(root, args.commit)
        rev_range = _range_for(root, source_commit)
    elif args.range:
        rev_range = args.range
        source_commit = gitio.resolve(root, args.range.split("..")[-1])
    elif args.pr:
        base = gitio.resolve(root, _gh_field(root, args.pr, "baseRefOid"))
        head = gitio.resolve(root, _gh_field(root, args.pr, "headRefOid"))
        rev_range, source_commit = f"{base}..{head}", head

    chat_context = None
    trigger = args.trigger
    if args.chat:
        chat_context = "" if sys.stdin.isatty() else sys.stdin.read()
        if trigger == "manual":
            trigger = "agent_chat"
    if args.detached and trigger == "manual":
        trigger = "detached"

    req = engine.UpdateRequest(
        targets=args.targets, source_commit=source_commit, rev_range=rev_range,
        use_staged=args.staged, use_working=args.working, detached=args.detached,
        instruction=instruction, chat_context=chat_context, trigger=trigger,
    )

    repo_id = config.checkout_id(gitio.common_dir(root))
    lock = locking.RepoLock(repo_id)
    if not lock.acquire(blocking=trigger == "post_commit"):
        raise PendingDocsUpdate("another documentation update is active for this repository")
    try:
        result = engine.run_update_guarded(root, req, cfg, rt)
    finally:
        lock.release()

    if result.status == "committed":
        print(result.completion_message)
    elif result.status == "flagged":
        print(result.completion_message)
    elif result.status == "gap":
        print("documentation_gap — a doc is warranted but no writable placement exists.")
    elif result.status == "no_op" and trigger != "post_commit":
        print(status.NOOP)
    return 0


def _cmd_changelog(args: argparse.Namespace) -> int:
    if args.all:
        records = history.recent(None, limit=args.limit if not args.status else 1000)
        label = "(all repos)"
    else:
        root = gitio.repo_root(Path.cwd())
        repo_id = config.checkout_id(gitio.common_dir(root))
        records = history.recent(repo_id, limit=args.limit if not args.status else 1000)
        label = "(this repo)"
    if args.status:
        records = [r for r in records if r["status"] == args.status]
    print(views.render_changelog(records[: args.limit], label))
    return 0


def _find_record(record_id: int) -> "Optional[dict]":
    return history.get(record_id)


def _gh_field(root: Path, number: int, field: str) -> str:
    out = pr._gh(root, "pr", "view", str(number), "--json", field, "-q", f".{field}")
    return out


def _cmd_ui(_args: argparse.Namespace) -> int:
    from .ui import server
    server.serve()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    instruction: Optional[str] = None
    if "--" in raw:
        i = raw.index("--")
        instruction = " ".join(raw[i + 1:]).strip() or None
        raw = raw[:i]

    args = _build_parser().parse_args(raw)

    try:
        if args.cmd is None or args.cmd == "ui":
            return _cmd_ui(args)
        if args.cmd == "update":
            return _cmd_update(args, instruction)
        if args.cmd == "status":
            print(status.render(gitio.repo_root(Path.cwd())))
            return 0
        if args.cmd == "help":
            print(help_mod.render(args.topic))
            return 0
        if args.cmd == "docs":
            print(views.render_docs(gitio.repo_root(Path.cwd())))
            return 0
        if args.cmd == "changelog":
            return _cmd_changelog(args)
        if args.cmd == "show":
            print(views.render_record(_find_record(args.id)))
            return 0
        if args.cmd == "style":
            print(views.render_style())
            return 0
        if args.cmd == "auth":
            if not args.runtime:
                print(auth.render_status())
                return 0
            selection = auth.select(args.runtime)
            for note in selection.notes:
                print(note)
            return 0 if selection.ready else 1
        if args.cmd == "init":
            root = gitio.repo_root(Path.cwd())
            for note in hooks.install(root):
                print("installed:", note)
            _print_auth_note(config.Config.load())
            return 0
        if args.cmd == "install":
            for note in agent_skill.install(scope="user"):
                print("installed:", note)
            print("say \"choobi update <doc> based on …\" inside Claude Code or Codex to use it.")
            if config.invocation() != "choobi":
                print("note: `choobi` is not on PATH; the skill calls it via python -m choobi. "
                      "run `pip install -e .` for a cleaner `choobi` command.")
            return 0
        if args.cmd == "pr":
            root = gitio.repo_root(Path.cwd())
            print(pr.create(root))
            return 0
    except ChoobiError as exc:
        print(f"{status.FAILED}   ({exc.reason}): {exc.message}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_auth_note(cfg: config.Config) -> None:
    agent = cfg.agent or "claude"
    if auth.is_logged_in(agent):
        print(f"runtime: {agent} is logged in — choobi is ready.")
    else:
        print("runtime not ready. run `choobi auth claude` or `choobi auth codex` "
              "to authenticate and select one runtime.")
