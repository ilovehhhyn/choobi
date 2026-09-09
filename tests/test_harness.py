"""Background-harness tests: branch identity, parking, coalescing, auto-push, retries, audit.

Every test builds a throwaway git repo (some with a bare `origin`) and points CHOOBI_HOME at a
temp dir. Models are FakeRuntime; nothing here spends tokens or touches ~/.choobi.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from choobi import baseline, commitwriter, docs, gitio, verify
from choobi.errors import Conflict, NotAllowedPath, Parked, VerificationFailed


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(root), check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


def make_repo(root: Path) -> str:
    """docs/api.md covers src/api.py; two commits on `main`. Returns HEAD."""
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "README.md").write_text("# demo\n")
    (root / "docs" / "api.md").write_text("---\ncovers: src/api.py\n---\n# API\n\nRetries once.\n")
    (root / "src" / "api.py").write_text("def retry(): pass\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.co")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    (root / "src" / "api.py").write_text("def retry(n=3): return n\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add configurable retry backoff")
    return gitio.resolve(root, "HEAD")


def add_remote(root: Path, remote_dir: Path) -> None:
    _git(remote_dir, "init", "-q", "--bare", "-b", "main")
    _git(root, "remote", "add", "origin", str(remote_dir))


class HarnessCase(unittest.TestCase):
    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory(prefix="choobi-home-")
        self._repo = tempfile.TemporaryDirectory(prefix="choobi-repo-")
        self._remote = tempfile.TemporaryDirectory(prefix="choobi-remote-")
        os.environ["CHOOBI_HOME"] = self._home.name
        os.environ.pop("CHOOBI_RUNTIME", None)
        os.environ.pop("CHOOBI_TOOLS", None)
        self.root = Path(self._repo.name)
        self.remote = Path(self._remote.name)
        self.head = make_repo(self.root)

    def tearDown(self) -> None:
        for tmp in (self._home, self._repo, self._remote):
            tmp.cleanup()


class GitPlumbingTest(HarnessCase):
    def test_current_branch_is_none_when_detached(self) -> None:
        self.assertEqual(gitio.current_branch(self.root), "main")
        _git(self.root, "checkout", "-q", "--detach")
        self.assertIsNone(gitio.current_branch(self.root))

    def test_ls_tree_reports_modes_including_symlinks(self) -> None:
        os.symlink("docs/api.md", self.root / "link.md")
        _git(self.root, "add", "link.md")
        _git(self.root, "commit", "-qm", "symlink")
        modes = gitio.ls_tree(self.root, "HEAD")
        self.assertEqual(modes["docs/api.md"], "100644")
        self.assertEqual(modes["link.md"], "120000")
        self.assertNotIn("missing.md", modes)

    def test_show_blob_reads_committed_bytes_not_the_dirty_working_copy(self) -> None:
        (self.root / "docs/api.md").write_text("# dirty\n")
        self.assertIn(b"Retries once.", gitio.show_blob(self.root, "HEAD", "docs/api.md"))
        with self.assertRaises(RuntimeError):
            gitio.show_blob(self.root, "HEAD", "docs/nope.md")

    def test_commits_between_is_oldest_first(self) -> None:
        first = gitio.resolve(self.root, "HEAD^")
        (self.root / "a.txt").write_text("a")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "a")
        a = gitio.resolve(self.root, "HEAD")
        (self.root / "b.txt").write_text("b")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "b")
        b = gitio.resolve(self.root, "HEAD")
        self.assertEqual(gitio.commits_between(self.root, first, b), [self.head, a, b])
        self.assertEqual(gitio.commits_between(self.root, b, b), [])

    def test_upstream_and_fast_forward_push(self) -> None:
        self.assertIsNone(gitio.upstream(self.root))
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        self.assertEqual(gitio.upstream(self.root), ("origin", "main"))

        (self.root / "docs/api.md").write_text("# API\n\nRetries three times.\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "docs")
        docs_commit = gitio.resolve(self.root, "HEAD")
        gitio.push_fast_forward(self.root, "origin", "main", docs_commit, {"CHOOBI_GENERATING": "1"})
        self.assertEqual(_git(self.remote, "rev-parse", "main"), docs_commit)

        # A remote that moved ahead rejects the push; nothing is forced.
        other = Path(tempfile.mkdtemp(prefix="choobi-other-"))
        _git(other, "clone", "-q", str(self.remote), "clone")
        clone = other / "clone"
        _git(clone, "config", "user.email", "o@o.co"); _git(clone, "config", "user.name", "o")
        (clone / "other.txt").write_text("x")
        _git(clone, "add", "-A"); _git(clone, "commit", "-qm", "other"); _git(clone, "push", "-q")
        remote_tip = _git(self.remote, "rev-parse", "main")
        (self.root / "z.txt").write_text("z")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "local")
        with self.assertRaises(RuntimeError):
            gitio.push_fast_forward(self.root, "origin", "main", gitio.resolve(self.root, "HEAD"),
                                    {"CHOOBI_GENERATING": "1"})
        self.assertEqual(_git(self.remote, "rev-parse", "main"), remote_tip)

    def test_pending_refs_lists_choobi_refs_only(self) -> None:
        self.assertEqual(gitio.pending_refs(self.root), {})
        _git(self.root, "update-ref", f"refs/choobi/pending/{self.head}", self.head)
        _git(self.root, "update-ref", "refs/other/x", self.head)
        self.assertEqual(gitio.pending_refs(self.root), {self.head: self.head})


class TreeTest(HarnessCase):
    def test_pinned_tree_ignores_dirty_working_copy(self) -> None:
        (self.root / "docs/api.md").write_text("# dirty\n")
        working = docs.Tree.working(self.root)
        pinned = docs.Tree.at(self.root, "HEAD")
        self.assertIn("dirty", working.read("docs/api.md")[0])
        text, digest = pinned.read("docs/api.md")
        self.assertIn("Retries once.", text)
        self.assertEqual(len(digest), 64)
        self.assertTrue(pinned.exists("docs/api.md"))
        self.assertTrue(pinned.exists("docs"))
        self.assertFalse(pinned.exists("docs/nope.md"))
        self.assertIn("src/api.py", pinned.files())

    def test_pinned_tree_rejects_symlink_blobs(self) -> None:
        os.symlink("docs/api.md", self.root / "link.md")
        _git(self.root, "add", "link.md")
        _git(self.root, "commit", "-qm", "symlink")
        with self.assertRaises(NotAllowedPath):
            docs.Tree.at(self.root, "HEAD").read("link.md")

    def test_tracked_documents_can_read_a_pinned_tree(self) -> None:
        (self.root / "docs/api.md").write_text("# dirty\n")
        records = {r.path: r for r in docs.tracked_documents(
            self.root, baseline.policy(), tree=docs.Tree.at(self.root, "HEAD"))}
        self.assertIn("Retries once.", records["docs/api.md"].content)
        self.assertTrue(records["docs/api.md"].writable)


class VerifySplitTest(HarnessCase):
    def test_content_checks_resolve_links_against_the_pinned_tree(self) -> None:
        (self.root / "docs/guide.md").write_text("# Guide\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "guide")
        (self.root / "docs/guide.md").unlink()          # deleted in the working tree only
        content = "---\ncovers: src/api.py\n---\n# API\n\nSee [guide](guide.md).\n"
        old, _ = docs.Tree.at(self.root, "HEAD").read("docs/api.md")
        verify.check_content(
            self.root, "docs/api.md", content, is_create=False, old_content=old,
            policy=baseline.policy(), tree=docs.Tree.at(self.root, "HEAD"),
        )
        with self.assertRaises(VerificationFailed):
            verify.check_content(
                self.root, "docs/api.md", content, is_create=False, old_content=old,
                policy=baseline.policy(), tree=docs.Tree.working(self.root),
            )

    def test_tree_state_checks_are_separate(self) -> None:
        expected = gitio.file_hash(self.root, "docs/api.md")
        verify.check_tree_state(self.root, "docs/api.md", is_create=False, expected_hash=expected)
        (self.root / "docs/api.md").write_text("# dirty\n")
        with self.assertRaises(Conflict):
            verify.check_tree_state(self.root, "docs/api.md", is_create=False,
                                    expected_hash=expected)


NEW_DOC = "---\ncovers: src/api.py\n---\n# API\n\nRetries up to n times (default 3).\n"


class CommitWriterTest(HarnessCase):
    def _write(self, **overrides):
        kwargs = dict(
            source_commit=self.head,
            expected_hashes={"docs/api.md": gitio.file_hash(self.root, "docs/api.md")},
            source_branch="main",
        )
        kwargs.update(overrides)
        return commitwriter.write_and_commit(
            self.root, {"docs/api.md": NEW_DOC}, "add configurable retry backoff", **kwargs)

    def test_happy_path_appends_one_commit_and_clears_the_ref(self) -> None:
        new_head = self._write()
        self.assertEqual(new_head, gitio.resolve(self.root, "HEAD"))
        self.assertEqual(gitio.resolve(self.root, "HEAD^"), self.head)
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)
        self.assertEqual(_git(self.root, "status", "--porcelain"), "")
        self.assertEqual(gitio.pending_refs(self.root), {})

    def test_switched_branch_parks_and_touches_neither_branch(self) -> None:
        expected = gitio.file_hash(self.root, "docs/api.md")
        _git(self.root, "checkout", "-q", "-b", "other")
        (self.root / "other.txt").write_text("x")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "other work")
        other_head = gitio.resolve(self.root, "HEAD")
        with self.assertRaises(Parked) as caught:
            self._write(expected_hashes={"docs/api.md": expected})
        pending = caught.exception.pending
        self.assertIn("other", caught.exception.why)
        self.assertEqual(gitio.resolve(self.root, "HEAD"), other_head)
        self.assertEqual(gitio.resolve(self.root, "main"), self.head)
        self.assertEqual(gitio.pending_refs(self.root), {self.head: pending})
        # The parked commit was built on main's tip, not on the other branch.
        self.assertEqual(gitio.resolve(self.root, f"{pending}^"), self.head)
        self.assertIn("default 3", gitio.show_blob(self.root, pending, "docs/api.md").decode())

    def test_dirty_target_parks_and_keeps_the_users_edit(self) -> None:
        expected = gitio.file_hash(self.root, "docs/api.md")
        (self.root / "docs/api.md").write_text("# my in-progress edit\n")
        with self.assertRaises(Parked):
            self._write(expected_hashes={"docs/api.md": expected})
        self.assertEqual((self.root / "docs/api.md").read_text(), "# my in-progress edit\n")
        self.assertEqual(gitio.resolve(self.root, "HEAD"), self.head)
        self.assertEqual(len(gitio.pending_refs(self.root)), 1)

    def test_operation_in_progress_parks(self) -> None:
        gitdir = Path(_git(self.root, "rev-parse", "--absolute-git-dir"))
        (gitdir / "MERGE_HEAD").write_text(self.head + "\n")
        with self.assertRaises(Parked) as caught:
            self._write()
        self.assertIn("in progress", caught.exception.why)
        (gitdir / "MERGE_HEAD").unlink()
        self.assertEqual(gitio.resolve(self.root, "HEAD"), self.head)

    def test_stale_draft_is_a_conflict_and_builds_nothing(self) -> None:
        expected = gitio.file_hash(self.root, "docs/api.md")
        (self.root / "docs/api.md").write_text("# human committed later\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "human docs edit")
        with self.assertRaises(Conflict):
            self._write(expected_hashes={"docs/api.md": expected})
        self.assertEqual(gitio.pending_refs(self.root), {})
        self.assertEqual((self.root / "docs/api.md").read_text(), "# human committed later\n")

    def test_branch_advanced_by_unrelated_commit_still_attaches(self) -> None:
        (self.root / "src/other.py").write_text("x = 1\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "unrelated")
        tip = gitio.resolve(self.root, "HEAD")
        new_head = self._write()
        self.assertEqual(gitio.resolve(self.root, f"{new_head}^"), tip)
        self.assertEqual(gitio.pending_refs(self.root), {})

    def test_rewritten_branch_is_a_conflict(self) -> None:
        _git(self.root, "commit", "-q", "--amend", "-m", "amended")
        with self.assertRaises(Conflict):
            self._write()

    def test_detached_head_builds_off_head(self) -> None:
        _git(self.root, "checkout", "-q", "--detach")
        new_head = self._write(source_branch=None)
        self.assertEqual(gitio.resolve(self.root, f"{new_head}^"), self.head)

    def test_attach_pending_lands_a_parked_commit_and_is_idempotent(self) -> None:
        expected = gitio.file_hash(self.root, "docs/api.md")
        _git(self.root, "checkout", "-q", "-b", "other")
        with self.assertRaises(Parked) as caught:
            self._write(expected_hashes={"docs/api.md": expected})
        _git(self.root, "checkout", "-q", "main")
        pending = caught.exception.pending
        landed = commitwriter.attach_pending(self.root, pending, paths=["docs/api.md"])
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)
        self.assertEqual(gitio.resolve(self.root, "HEAD"), landed)
        # Re-attaching the same content is a no-op, never a duplicate commit.
        self.assertEqual(commitwriter.attach_pending(self.root, pending, paths=["docs/api.md"]),
                         landed)


if __name__ == "__main__":
    unittest.main()
