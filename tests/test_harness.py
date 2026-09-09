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

from choobi import baseline, docs, gitio
from choobi.errors import NotAllowedPath


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


if __name__ == "__main__":
    unittest.main()
