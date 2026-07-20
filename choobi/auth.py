"""`choobi auth` — pick the runtime and make sure it's logged in.

choobi does not host its own login. Each runtime CLI owns its own auth, so choobi
*delegates*: it checks the requested runtime's login status, launches that runtime's native
browser login when needed, and records the choice only after authentication succeeds. Choobi
is the driver, not the identity provider.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import config
from .errors import RuntimeUnavailable

RUNTIMES: Dict[str, Dict[str, List[str]]] = {
    "claude": {"status": ["claude", "auth", "status"], "login": ["claude", "auth", "login"]},
    "codex": {"status": ["codex", "login", "status"], "login": ["codex", "login"]},
}
_NEGATIVE = ("not logged in", "logged out", "not authenticated", "no credentials")


@dataclass(frozen=True)
class AuthSelection:
    """Result of authenticating and selecting exactly one active Choobi runtime."""

    requested: str
    active: str
    ready: bool
    switched: bool
    notes: List[str]


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


def _state_label(state: Optional[bool], active: bool) -> str:
    if active:
        return {True: "✓ ready", False: "✕ sign-in required", None: "– not installed"}[state]
    return {True: "✓ available", False: "– sign-in required", None: "– not installed"}[state]


def render_status() -> str:
    cfg = config.Config.load()
    lines = [f"choobi runtime: {cfg.agent} (active)"]
    if cfg.agent not in RUNTIMES:
        lines.append(f"  {cfg.agent.ljust(7)} ✕ unsupported")
    for rt in RUNTIMES:
        active = rt == cfg.agent
        mark = "   (active)" if active else ""
        lines.append(f"  {rt.ljust(7)} {_state_label(is_logged_in(rt), active)}{mark}")
    lines.append("\nonly one runtime is active. run `choobi auth claude` or "
                 "`choobi auth codex` to authenticate and select it.")
    return "\n".join(lines)


def select(runtime: str) -> AuthSelection:
    """Authenticate `runtime`, then make it active without risking the prior selection.

    The requested runtime is persisted only after its CLI reports a valid login. A failed
    install check, cancelled browser flow, or failed login therefore leaves the previous
    runtime active.
    """
    if runtime not in RUNTIMES:
        raise RuntimeUnavailable(
            f"unsupported runtime '{runtime}' (choose claude or codex)"
        )
    cfg = config.Config.load()
    previous = cfg.agent
    if shutil.which(runtime) is None:
        return AuthSelection(
            requested=runtime, active=previous, ready=False, switched=False,
            notes=[f"the {runtime} CLI is not on PATH — install it, then run "
                   f"`choobi auth {runtime}` again."],
        )

    state = is_logged_in(runtime)
    already_ready = state is True
    if not already_ready:
        try:
            # Inherit stdio so the runtime's own interactive browser login can complete.
            subprocess.run(RUNTIMES[runtime]["login"])
        except OSError as exc:
            return AuthSelection(
                requested=runtime, active=previous, ready=False, switched=False,
                notes=[f"could not launch {runtime} login: {exc}"],
            )
        state = is_logged_in(runtime)

    if state is not True:
        suffix = (
            f"; still using {previous}." if previous and previous != runtime
            else "; no authenticated runtime was selected."
        )
        return AuthSelection(
            requested=runtime, active=previous, ready=False, switched=False,
            notes=[f"{runtime} login did not complete{suffix}"],
        )

    cfg = config.Config.load()
    prior_active = cfg.agent
    cfg.agent = runtime
    cfg.save()
    switched = prior_active != runtime
    notes = [f"runtime set to {runtime}."]
    if already_ready:
        notes.append(f"✓ {runtime} is already logged in — choobi is ready.")
    else:
        notes.append(f"✓ {runtime} login complete — choobi is ready.")
    return AuthSelection(
        requested=runtime, active=runtime, ready=True, switched=switched, notes=notes,
    )
