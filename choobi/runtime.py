"""Runtime adapter — the only component that calls a model.

The first adapter targets a non-interactive coding-agent CLI (build-plan §4). `complete`
takes a fully-built prompt and returns the model's raw text; the engine builds the prompt
and parses the result. If the configured runtime is unavailable we raise
RuntimeUnavailable — never a silent switch to a different runtime.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

from . import config
from .errors import RuntimeUnavailable


class Runtime:
    name = "base"

    def complete(self, prompt: str, system: str = "", timeout: int = 180) -> str:
        raise NotImplementedError


class ClaudeCliRuntime(Runtime):
    """Shells the authenticated `claude` CLI in print mode with a JSON envelope."""

    name = "claude"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def complete(self, prompt: str, system: str = "", timeout: int = 180) -> str:
        binary = shutil.which("claude")
        if not binary:
            raise RuntimeUnavailable("claude CLI not found on PATH")
        cmd = [binary, "-p", prompt, "--output-format", "json"]
        if system:
            cmd += ["--append-system-prompt", system]
        env = dict(os.environ)
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  env=env, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeUnavailable(f"claude CLI failed: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeUnavailable(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeUnavailable(f"claude CLI returned non-JSON envelope: {exc}") from exc
        return str(envelope.get("result", ""))


class CodexCliRuntime(Runtime):
    """Shells the `codex exec` non-interactive path."""

    name = "codex"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def complete(self, prompt: str, system: str = "", timeout: int = 180) -> str:
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeUnavailable("codex CLI not found on PATH")
        full = (system + "\n\n" + prompt) if system else prompt
        env = dict(os.environ)
        if self.api_key:
            env["OPENAI_API_KEY"] = self.api_key
        try:
            # read-only sandbox: codex is choobi's brain, not an actor. It returns the
            # disposition JSON; choobi does every file write. codex must not touch the repo.
            proc = subprocess.run(
                [binary, "exec", "--sandbox", "read-only", full],
                capture_output=True, text=True, timeout=timeout,
                env=env, stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeUnavailable(f"codex CLI failed: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeUnavailable(f"codex CLI exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout


class FakeRuntime(Runtime):
    """Returns canned responses. `response` may be a string (same every call), a list
    (one per call, in order), or a callable(prompt) -> str. Used by tests and CHOOBI_RUNTIME=fake.
    """

    name = "fake"

    def __init__(self, response) -> None:
        self.response = list(response) if isinstance(response, list) else response
        self.last_prompt: Optional[str] = None

    def complete(self, prompt: str, system: str = "", timeout: int = 180) -> str:
        self.last_prompt = prompt
        if callable(self.response):
            return self.response(prompt)
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


def get_runtime(cfg: config.Config) -> Runtime:
    """Select the runtime by config. CHOOBI_RUNTIME=fake overrides for deterministic tests."""
    if os.environ.get("CHOOBI_RUNTIME") == "fake":
        return FakeRuntime(os.environ.get("CHOOBI_FAKE_RESPONSE", ""))
    if cfg.agent == "codex":
        return CodexCliRuntime(cfg.api_key)
    return ClaudeCliRuntime(cfg.api_key)
