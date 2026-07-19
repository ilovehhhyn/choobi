"""`choobi auth` — pick the runtime and make sure it's logged in.

choobi does not host its own login. Each runtime CLI owns its own auth, so choobi
*delegates*: it records your choice, checks the runtime's login status, and if you're not
logged in it launches that runtime's native login (which opens the browser). This is the
idiomatic pattern for a tool that drives another agent — choobi is the driver, not the
identity provider.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, Optional

from . import config

RUNTIMES: Dict[str, Dict[str, List[str]]] = {
    "claude": {"status": ["claude", "auth", "status"], "login": ["claude", "auth", "login"]},
}
_NEGATIVE = ("not logged in", "logged out", "not authenticated", "no credentials")


def is_logged_in(runtime: str) -> Optional[bool]:
    """True/False if the runtime is authenticated, or None if its CLI isn't installed."""
    if runtime not in RUNTIMES:
        return None
    if shutil.which(runtime) is None:
        return None
    try:
        proc = subprocess.run(
            RUNTIMES[runtime]["status"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout + proc.stderr).lower()
    if any(neg in out for neg in _NEGATIVE):
        return False
    return proc.returncode == 0


def _state_label(state: Optional[bool]) -> str:
    return {True: "✓ logged in", False: "✕ not logged in", None: "– not installed"}[state]


def render_status() -> str:
    cfg = config.Config.load()
    lines = [f"choobi runtime: {cfg.agent} (default)"]
    if cfg.agent not in RUNTIMES:
        lines.append(f"  {cfg.agent.ljust(7)} ✕ unsupported: runtime must be tool-free")
    for rt in RUNTIMES:
        mark = "   (default)" if rt == cfg.agent else ""
        lines.append(f"  {rt.ljust(7)} {_state_label(is_logged_in(rt))}{mark}")
    lines.append("\nrun `choobi auth claude` to select the supported runtime and log in.")
    return "\n".join(lines)


def ensure(runtime: str) -> List[str]:
    """Set `runtime` as the default and make sure it's logged in. Returns notes."""
    if runtime not in RUNTIMES:
        raise ValueError(f"unsupported runtime '{runtime}' (choose claude)")
    cfg = config.Config.load()
    cfg.agent = runtime
    cfg.save()
    notes = [f"runtime set to {runtime}."]
    if shutil.which(runtime) is None:
        notes.append(f"but the {runtime} CLI is not on PATH — install it, then run "
                     f"`choobi auth {runtime}` again.")
        return notes
    if is_logged_in(runtime):
        notes.append(f"✓ {runtime} is already logged in — choobi is ready.")
        return notes
    notes.append(f"launching {runtime} login…")
    print("\n".join(notes))
    # Inherit stdio so the runtime's own interactive/browser login works.
    subprocess.run(RUNTIMES[runtime]["login"])
    if is_logged_in(runtime):
        return ["✓ logged in — choobi is ready."]
    return ["login did not complete; run `choobi auth " + runtime + "` to try again."]
