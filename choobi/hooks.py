"""`choobi init` — install the post-commit hook and the project-scope agent skill.

The hook does no inference. It returns immediately and starts the engine in the
background (build-plan §3.1). The recursion guard is the inherited CHOOBI_GENERATING
marker, checked in the very first line.
"""
from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path
from typing import List

from . import agent_skill, config, gitio, history
from .errors import HookConflict

_MANAGED_MARKER = "# managed by choobi"
_PERSISTED_ENV = ("CHOOBI_HOME",)


def _exports() -> str:
    """Bake only the storage root needed by the detached process."""
    return "".join(
        f"export {key}={shlex.quote(os.environ[key])}\n"
        for key in _PERSISTED_ENV if key in os.environ
    )


def install(root: Path) -> List[str]:
    """Install hook + project-scope skill. Returns notes about what was written."""
    notes: List[str] = []
    hooks_dir = Path(gitio._run(root, "rev-parse", "--git-path", "hooks").strip())
    if not hooks_dir.is_absolute():
        hooks_dir = root / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    if hook.exists():
        existing = hook.read_text(errors="replace")
        if existing.strip() and _MANAGED_MARKER not in existing \
                and "# choobi post-commit hook" not in existing:
            raise HookConflict(f"{hook} already exists; Choobi will not overwrite it")

    log = config.logs_dir()
    log.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        f"{_MANAGED_MARKER}\n"
        "# choobi post-commit hook — returns immediately, runs the engine in the background.\n"
        'if [ -n "$CHOOBI_GENERATING" ]; then exit 0; fi\n'
        f"{_exports()}"
        "SHA=$(git rev-parse HEAD)\n"
        f'( {config.invocation()} update --commit "$SHA" --trigger post_commit '
        f'>> {shlex.quote(str(log / "hook.log"))} 2>&1 & ) >/dev/null 2>&1\n'
        "exit 0\n"
    )
    hook.write_text(script)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    notes.append(f"post-commit hook -> {hook}")

    notes += agent_skill.install(scope="project", root=root)
    history.register_repo(config.checkout_id(gitio.common_dir(root)), str(root), initialized=True)
    return notes
