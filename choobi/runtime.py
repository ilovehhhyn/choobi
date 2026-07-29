"""Runtime adapters — the only components that call models.

Each adapter targets an authenticated non-interactive CLI (build-plan §4). `complete` takes
a fully-built prompt and returns the model's raw text; the engine builds the prompt and parses
the result. If the configured runtime is unavailable we raise RuntimeUnavailable — never a
silent switch to a different runtime.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from . import config
from .errors import RuntimeUnavailable

# One call reads up to the engine's whole in-scope corpus and, for an edit, writes a complete
# document back. On a reasoning model with a multi-megabyte prompt that is minutes of work, not
# seconds, and print mode returns nothing until the reply is complete. 180s was sized for the old
# 100 KB prompt ceiling and silently turned a slow-but-correct run into a runtime failure.
COMPLETION_TIMEOUT_SECONDS = 900

# Documentation and source tokenize at roughly 3.5-4 bytes per token. The prompt ceiling uses the
# low figure, so a corpus that passes the check cannot overflow the window on denser-than-expected
# text. This is a conversion factor, not a tuning knob: the runtimes authenticate as CLIs, so
# `count_tokens` is unavailable and bytes are the only thing choobi can measure.
BYTES_PER_TOKEN = 3.5

# Share of the context window the prompt may claim. The rest carries the system contract, the
# output schema, thinking, and the reply — which for an edit is a complete document.
PROMPT_SHARE_OF_WINDOW = 0.8


class Runtime:
    """One model call, plus the two facts the engine needs to size a prompt for it.

    `context_window_tokens` is the honest reason the model is pinned per adapter. A byte ceiling
    is a claim about a context window, so an adapter that inherited the operator's CLI default
    would be making that claim about a window nobody knows. Declaring both together means the
    pair cannot drift, and `prompt_budget_bytes` is derived rather than restated.
    """

    name = "base"
    model = ""
    context_window_tokens = 0

    @property
    def prompt_budget_bytes(self) -> int:
        return int(self.context_window_tokens * PROMPT_SHARE_OF_WINDOW * BYTES_PER_TOKEN)

    def complete(
        self, prompt: str, system: str = "", timeout: int = COMPLETION_TIMEOUT_SECONDS,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        raise NotImplementedError


class ClaudeCliRuntime(Runtime):
    """Shells the authenticated `claude` CLI in print mode with a JSON envelope."""

    name = "claude"
    model = "claude-opus-5"
    context_window_tokens = 1_000_000  # Claude Opus 5: 1M is both the default and the maximum.

    def complete(
        self, prompt: str, system: str = "", timeout: int = COMPLETION_TIMEOUT_SECONDS,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        binary = shutil.which("claude")
        if not binary:
            raise RuntimeUnavailable("claude CLI not found on PATH")
        cmd = [binary, "-p", "--output-format", "json", "--tools", "",
               "--safe-mode", "--no-session-persistence", "--model", self.model]
        if system:
            cmd += ["--system-prompt", system]
        if schema:
            cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  env=dict(os.environ), input=prompt)
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
    """Run Codex ephemerally in an empty read-only workspace with schema output.

    Codex CLI does not expose a separate system-prompt flag, so the Choobi contract and
    evidence are sent together as one explicitly delimited input. User config and exec rules
    are ignored, no session is persisted, and the working directory contains only the schema
    and final-output files created for this call.
    """

    name = "codex"

    # `--ignore-user-config` means choobi gets the Codex CLI's own default model, not the
    # operator's configured one, and choobi cannot verify that model's context window from here.
    # So this window is a declared floor rather than a measurement. It is deliberately the safe
    # direction to be wrong in: an under-sized budget fails early with `context_too_large`, which
    # names the remedy, where an over-sized one fails late as an opaque CLI rejection. Raise it
    # only against a verified window for a model this adapter also pins.
    context_window_tokens = 200_000

    def complete(
        self, prompt: str, system: str = "", timeout: int = COMPLETION_TIMEOUT_SECONDS,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeUnavailable("codex CLI not found on PATH")

        with tempfile.TemporaryDirectory(prefix="choobi-codex-") as tmp:
            root = Path(tmp)
            output_path = root / "final.txt"
            cmd = [
                binary, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "-c", 'approval_policy="never"',
                "-c", 'shell_environment_policy.inherit="none"',
                "--skip-git-repo-check", "--color", "never", "-C", str(root),
                "--output-last-message", str(output_path),
            ]
            if schema:
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema, separators=(",", ":")))
                cmd += ["--output-schema", str(schema_path)]
            cmd.append("-")

            runtime_input = (
                "You are Choobi's isolated reasoning runtime. Do not run commands, inspect "
                "files, browse, call tools, or modify anything. Reason only from the supplied "
                "contract and evidence.\n\n"
                "----- BEGIN CHOOBI CONTRACT -----\n"
                f"{system}\n"
                "----- END CHOOBI CONTRACT -----\n\n"
                "----- BEGIN CHOOBI EVIDENCE -----\n"
                f"{prompt}\n"
                "----- END CHOOBI EVIDENCE -----\n"
            )
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    env=dict(os.environ), input=runtime_input, cwd=str(root),
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise RuntimeUnavailable(f"codex CLI failed: {exc}") from exc
            if proc.returncode != 0:
                detail = proc.stderr.strip() or proc.stdout.strip()
                raise RuntimeUnavailable(f"codex CLI exited {proc.returncode}: {detail}")
            if output_path.exists():
                return output_path.read_text()
            if proc.stdout.strip():
                return proc.stdout
            raise RuntimeUnavailable("codex CLI completed without a final response")


class FakeRuntime(Runtime):
    """Returns canned responses. `response` may be a string (same every call), a list
    (one per call, in order), or a callable(prompt) -> str. Used by tests and CHOOBI_RUNTIME=fake.
    """

    name = "fake"
    model = "fake"
    context_window_tokens = 1_000_000

    def __init__(self, response, context_window_tokens: Optional[int] = None) -> None:
        self.response = list(response) if isinstance(response, list) else response
        self.last_prompt: Optional[str] = None
        # Tests that exercise the byte ceiling shrink the window here rather than patching a
        # module constant, so they assert against the same derivation production uses.
        if context_window_tokens is not None:
            self.context_window_tokens = context_window_tokens

    def complete(
        self, prompt: str, system: str = "", timeout: int = COMPLETION_TIMEOUT_SECONDS,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
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
    if cfg.agent == "claude":
        return ClaudeCliRuntime()
    if cfg.agent == "codex":
        return CodexCliRuntime()
    raise RuntimeUnavailable(
        f"unsupported runtime {cfg.agent!r}; run `choobi auth claude` or `choobi auth codex`"
    )
