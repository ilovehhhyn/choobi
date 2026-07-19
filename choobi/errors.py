"""Typed failures. choobi fails loudly with a named reason; it never degrades silently.

Every reason here is a machine-readable code recorded in history and shown in the UI.
The prose stays warm (see status.py); the `reason` stays exact.
"""
from __future__ import annotations


class ChoobiError(Exception):
    """Base for every choobi failure. `reason` is the stable, typed code."""

    reason = "error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.reason)
        self.message = message or self.reason


class SourceCommitRequired(ChoobiError):
    reason = "source_commit_required"


class InvalidScope(ChoobiError):
    reason = "invalid_scope"


class RuntimeUnavailable(ChoobiError):
    """The configured runtime could not be reached. We never select a different one."""

    reason = "runtime_unavailable"


class RuntimeOutputInvalid(ChoobiError):
    """The runtime returned something that is not a valid disposition."""

    reason = "runtime_output_invalid"


class VerificationFailed(ChoobiError):
    reason = "verification_failed"


class DocumentationGap(ChoobiError):
    """A change deserves a doc but no writable placement exists; nothing is created."""

    reason = "documentation_gap"


class NotAllowedPath(ChoobiError):
    reason = "path_not_allowed"


class AmbiguousTarget(ChoobiError):
    reason = "ambiguous_target"


class TargetNotFound(ChoobiError):
    reason = "target_not_found"


class Conflict(ChoobiError):
    """The target changed under us since scope collection (hash mismatch)."""

    reason = "conflict"


class CommitFailed(ChoobiError):
    reason = "commit_failed"


class HookConflict(ChoobiError):
    reason = "hook_conflict"


class InvalidSop(ChoobiError):
    reason = "invalid_sop"


class InvalidRepository(ChoobiError):
    reason = "invalid_repository"


class InvalidSnapshot(ChoobiError):
    reason = "invalid_snapshot"


class ContextTooLarge(ChoobiError):
    reason = "context_too_large"


class PendingDocsUpdate(ChoobiError):
    reason = "pending_docs_update"
