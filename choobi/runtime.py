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


class Runtime:
    name = "base"

    def complete(
        self, prompt: str, system: str = "", timeout: int = 180,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        raise NotImplementedError


class ClaudeCliRuntime(Runtime):
    """Shells the authenticated `claude` CLI in print mode with a JSON envelope."""

    name = "claude"

    def complete(
        self, prompt: str, system: str = "", timeout: int = 180,
        schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        binary = shutil.which("claude")
        if not binary:
            raise RuntimeUnavailable("claude CLI not found on PATH")
        cmd = [binary, "-p", "--output-format", "json", "--tools", "",
               "--safe-mode", "--no-session-persistence"]
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

    def complete(
        self, prompt: str, system: str = "", timeout: int = 180,
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

    def __init__(self, response) -> None:
        self.response = list(response) if isinstance(response, list) else response
        self.last_prompt: Optional[str] = None

    def complete(
        self, prompt: str, system: str = "", timeout: int = 180,
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
