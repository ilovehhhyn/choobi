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


def _exports() -> str:
    """Bake any CHOOBI_* env so the detached process resolves the same home/runtime."""
    return "".join(
        f"export {k}={shlex.quote(v)}\n" for k, v in os.environ.items() if k.startswith("CHOOBI_")
    )


def install(root: Path) -> List[str]:
    """Install hook + project-scope skill. Returns notes about what was written."""
    notes: List[str] = []
    hooks_dir = Path(gitio._run(root, "rev-parse", "--git-path", "hooks").strip())
    if not hooks_dir.is_absolute():
        hooks_dir = root / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"

    log = config.logs_dir()
    log.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# choobi post-commit hook — returns immediately, runs the engine in the background.\n"
        'if [ -n "$CHOOBI_GENERATING" ]; then exit 0; fi\n'
        f"{_exports()}"
        "SHA=$(git rev-parse HEAD)\n"
        f'( {config.invocation()} update --commit "$SHA" --trigger post_commit '
        f'>> "{log / "hook.log"}" 2>&1 & ) >/dev/null 2>&1\n'
        "exit 0\n"
    )
    hook.write_text(script)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    notes.append(f"post-commit hook -> {hook}")

    notes += agent_skill.install(scope="project", root=root)
    history.register_repo(config.checkout_id(gitio.common_dir(root)), str(root), initialized=True)
    return notes
