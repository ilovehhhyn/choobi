"""Personal config and local storage paths, all under ~/.choobi.

Set CHOOBI_HOME to relocate the whole tree (tests use this so they never touch the
real ~/.choobi). Layout mirrors build-plan §8.2.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import InvalidRepository


def home() -> Path:
    root = os.environ.get("CHOOBI_HOME")
    return Path(root) if root else Path.home() / ".choobi"


def config_path() -> Path:
    return home() / "config.json"


def personal_style_path() -> Path:
    return home() / "style.md"


def db_path() -> Path:
    return home() / "choobi.db"


def repo_dir(checkout_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{16}", checkout_id) is None:
        raise InvalidRepository("repository id must be 16 lowercase hexadecimal characters")
    return home() / "repos" / checkout_id


def logs_dir() -> Path:
    return home() / "logs"


@dataclass
class Config:
    name: str = ""
    # Which coding-agent CLI backs the runtime adapter.
    agent: str = "claude"
    # Completion-message verbosity toggle, mirrored in the UI.
    mode: str = "curt"
    onboarded: bool = False
    @classmethod
    def load(cls) -> "Config":
        p = config_path()
        if not p.exists():
            return cls()
        data = json.loads(p.read_text())
        known = {k: data[k] for k in data if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))


def invocation() -> str:
    """How to invoke choobi from a generated script/skill, install-agnostic.

    Prefers a `choobi` on PATH; otherwise falls back to `python -m choobi` with the
    package's PYTHONPATH baked in so it works from a source checkout too.
    """
    binary = shutil.which("choobi")
    if binary:
        return shlex.quote(binary)
    pkg_parent = Path(__file__).resolve().parent.parent
    return f"PYTHONPATH={shlex.quote(str(pkg_parent))} {shlex.quote(sys.executable)} -m choobi"


def checkout_id(git_common_dir: str) -> str:
    """Stable id for a checkout, hashing the git common dir so linked worktrees share it."""
    return hashlib.sha256(os.path.abspath(git_common_dir).encode()).hexdigest()[:16]
