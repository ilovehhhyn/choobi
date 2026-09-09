"""Background-harness tests: branch identity, parking, coalescing, auto-push, retries, audit.

Every test builds a throwaway git repo (some with a bare `origin`) and points CHOOBI_HOME at a
temp dir. Models are FakeRuntime; nothing here spends tokens or touches ~/.choobi.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from choobi import apply as apply_mod, audit, baseline, cli, coalesce, commitwriter, config, docs, engine, gitio, history, hooks, pushing, status, verify
from choobi.engine import UpdateRequest
from choobi.errors import Conflict, NotAllowedPath, Parked, PushRejected, RuntimeUnavailable, VerificationFailed
from choobi.runtime import FakeRuntime


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


def _park(test: HarnessCase) -> str:
    """Park a docs commit for test.head by switching branches first. Returns the pending sha."""
    expected = gitio.file_hash(test.root, "docs/api.md")
    _git(test.root, "checkout", "-q", "-b", "other", "HEAD^")   # other does not contain head
    with test.assertRaises(Parked) as caught:
        commitwriter.write_and_commit(
            test.root, {"docs/api.md": NEW_DOC}, "add configurable retry backoff",
            source_commit=test.head, expected_hashes={"docs/api.md": expected},
            source_branch="main",
        )
    return caught.exception.pending


class PushingTest(HarnessCase):
    def _docs_commit(self) -> str:
        (self.root / "docs/api.md").write_text(NEW_DOC)
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "docs")
        return gitio.resolve(self.root, "HEAD")

    def test_pushes_only_where_the_developer_already_pushed(self) -> None:
        cfg = config.Config()
        self.assertEqual(pushing.maybe_push(self.root, cfg, source_commit=self.head,
                                            docs_commit=self.head), pushing.NO_UPSTREAM)
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        docs_commit = self._docs_commit()
        self.assertEqual(pushing.maybe_push(self.root, cfg, source_commit=self.head,
                                            docs_commit=docs_commit), pushing.PUSHED)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), docs_commit)

    def test_does_not_push_a_branch_the_developer_has_not_pushed(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        (self.root / "src/api.py").write_text("def retry(n=5): return n\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "local only")
        source = gitio.resolve(self.root, "HEAD")
        docs_commit = self._docs_commit()
        self.assertEqual(pushing.maybe_push(self.root, config.Config(), source_commit=source,
                                            docs_commit=docs_commit), pushing.NOT_PUBLISHED)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), self.head)

    def test_disabled_and_rejected(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        docs_commit = self._docs_commit()
        self.assertEqual(pushing.maybe_push(self.root, config.Config(auto_push=False),
                                            source_commit=self.head, docs_commit=docs_commit),
                         pushing.DISABLED)
        with mock.patch("choobi.pushing.gitio.push_fast_forward",
                        side_effect=RuntimeError("rejected")):
            with self.assertRaises(PushRejected):
                pushing.maybe_push(self.root, config.Config(), source_commit=self.head,
                                   docs_commit=docs_commit)
        self.assertEqual(gitio.resolve(self.root, "HEAD"), docs_commit)


class ApplyTest(HarnessCase):
    def test_apply_lands_parked_commit_on_its_branch_and_pushes(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        pending = _park(self)
        # Still on `other`, which does not contain the source commit: not landed here.
        skipped = apply_mod.apply_pending(self.root, config.Config())
        self.assertEqual([(o.status, o.pending) for o in skipped], [("skipped", pending)])
        self.assertEqual(gitio.pending_refs(self.root), {self.head: pending})

        _git(self.root, "checkout", "-q", "main")
        landed = apply_mod.apply_pending(self.root, config.Config())
        self.assertEqual([o.status for o in landed], ["landed"])
        self.assertEqual(landed[0].detail, pushing.PUSHED)
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)
        self.assertEqual(gitio.pending_refs(self.root), {})
        self.assertEqual(_git(self.remote, "rev-parse", "main"), gitio.resolve(self.root, "HEAD"))
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        record = history.recent(repo_id, limit=1)[0]
        self.assertEqual((record["status"], record["trigger"], record["push"]),
                         ("committed", "apply", pushing.PUSHED))
        self.assertEqual(apply_mod.apply_pending(self.root, config.Config()), [])

    def test_apply_keeps_the_ref_when_the_target_is_dirty(self) -> None:
        pending = _park(self)
        _git(self.root, "checkout", "-q", "main")
        (self.root / "docs/api.md").write_text("# editing\n")
        outcome = apply_mod.apply_pending(self.root, config.Config())[0]
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("uncommitted", outcome.detail)
        self.assertEqual(gitio.pending_refs(self.root), {self.head: pending})
        self.assertEqual((self.root / "docs/api.md").read_text(), "# editing\n")

    def test_cli_apply_prints_one_line_per_ref(self) -> None:
        _park(self)
        _git(self.root, "checkout", "-q", "main")
        with mock.patch("choobi.cli.gitio.repo_root", return_value=self.root):
            with mock.patch("builtins.print") as printed:
                self.assertEqual(cli.main(["apply"]), 0)
        text = printed.call_args[0][0]
        self.assertIn("landed", text)
        self.assertIn(self.head[:7], text)

    def test_history_migration_adds_push_column_to_an_old_database(self) -> None:
        import sqlite3
        config.db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.db_path()))
        conn.executescript(history._SCHEMA.replace(",\n    push          TEXT NOT NULL DEFAULT ''", ""))
        conn.close()
        rid = history.add_record("0123456789abcdef", "/x", "manual", "committed", push="pushed")
        self.assertEqual(history.get(rid)["push"], "pushed")


def _link(doc: str = "docs/api.md", findings: str = "") -> str:
    data = {"action": "doc", "doc": doc, "area": "backend", "scope": "area"}
    if findings:
        data["findings"] = findings
    return json.dumps(data)


def _upd(content: str = NEW_DOC, summary: str = "documented the retry default in docs/api.md",
         target: str = "docs/api.md") -> str:
    return json.dumps({"disposition": "update", "target": target, "summary": summary,
                       "content": content, "source_paths": []})


def _silent(summary: str = "") -> str:
    return json.dumps({"disposition": "silent", "target": "", "summary": summary,
                       "content": "", "source_paths": []})


class EngineTest(HarnessCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = config.Config(name="t", onboarded=True)
        self.repo_id = config.checkout_id(gitio.common_dir(self.root))

    def _anchored(self, sha: "str | None" = None, trigger: str = "post_commit") -> UpdateRequest:
        sha = sha or self.head
        return UpdateRequest(source_commit=sha, rev_range=f"{sha}^..{sha}", trigger=trigger)

    def test_anchored_run_reads_evidence_from_git_objects_not_the_working_tree(self) -> None:
        (self.root / "src/api.py").write_text("def retry(n=99): return 'UNCOMMITTED_SENTINEL'\n")
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return _link() if "## Final response" in prompt else _upd()

        result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertNotIn("UNCOMMITTED_SENTINEL", "\n".join(prompts))
        self.assertIn("def retry(n=3)", prompts[0])
        # The user's in-progress source edit is untouched and the docs commit landed cleanly.
        self.assertIn("UNCOMMITTED_SENTINEL", (self.root / "src/api.py").read_text())
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)

    def test_branch_switch_during_the_run_parks_and_apply_lands_later(self) -> None:
        def answer(prompt: str) -> str:
            if "## Final response" in prompt:
                return _link()
            _git(self.root, "checkout", "-q", "-b", "other", "HEAD^")   # user wandered off
            return _upd()

        result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "parked")
        self.assertIn("choobi apply", result.completion_message)
        record = history.recent(self.repo_id, limit=1)[0]
        self.assertEqual(record["status"], "parked")
        self.assertEqual(record["docs_commit"], result.docs_commit)
        self.assertIn("+Retries up to n times", record["patch"])
        self.assertEqual(gitio.resolve(self.root, "main"), self.head)
        # Idempotent: the hook re-firing for the same commit does nothing new.
        again = engine.run_update(self.root, self._anchored(), self.cfg,
                                  FakeRuntime("MUST NOT BE CALLED"))
        self.assertEqual(again.status, "parked")

        _git(self.root, "checkout", "-q", "main")
        outcomes = apply_mod.apply_pending(self.root, self.cfg)
        self.assertEqual(outcomes[0].status, "landed")
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)

    def test_rejected_draft_is_fed_back_and_retried(self) -> None:
        prompts = []
        broken = NEW_DOC + "\nSee [missing](nope.md).\n"

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            if "## Final response" in prompt:
                return _link()
            editor_calls = sum("Candidate documents" in p for p in prompts)
            return _upd(broken) if editor_calls == 1 else _upd()

        result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertEqual(len(prompts), 3)
        self.assertIn("Previous answer was rejected", prompts[2])
        self.assertIn("broken link", prompts[2])
        self.assertNotIn("Previous answer was rejected", prompts[1])
        self.assertEqual([r["status"] for r in history.recent(self.repo_id)], ["committed"])

    def test_three_rejected_drafts_fail_loudly_with_the_last_reason(self) -> None:
        broken = NEW_DOC + "\nSee [missing](nope.md).\n"
        calls = []

        def answer(prompt: str) -> str:
            calls.append(prompt)
            return _link() if "## Final response" in prompt else _upd(broken)

        with self.assertRaises(VerificationFailed):
            engine.run_update_guarded(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual(len(calls), 1 + engine.MAX_DRAFT_ATTEMPTS)
        record = history.recent(self.repo_id, limit=1)[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["reason"], "verification_failed")
        self.assertIn("broken link", record["summary"])
        self.assertEqual(gitio.resolve(self.root, "HEAD"), self.head)

    def test_unavailable_runtime_backs_off_and_retries(self) -> None:
        failures = {"left": 2}

        def answer(prompt: str) -> str:
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeUnavailable("claude CLI exited 1")
            return _link() if "## Final response" in prompt else _upd()

        with mock.patch("choobi.engine.time.sleep") as sleep:
            result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(engine.RUNTIME_BACKOFF))

    def test_runtime_that_stays_down_fails_with_runtime_unavailable(self) -> None:
        def answer(prompt: str) -> str:
            raise RuntimeUnavailable("down")

        with mock.patch("choobi.engine.time.sleep"):
            with self.assertRaises(RuntimeUnavailable):
                engine.run_update_guarded(self.root, self._anchored(), self.cfg,
                                          FakeRuntime(answer))
        self.assertEqual(history.recent(self.repo_id, limit=1)[0]["reason"],
                         "runtime_unavailable")

    def test_stale_draft_triggers_one_fresh_run(self) -> None:
        runs = {"n": 0}

        def answer(prompt: str) -> str:
            if "## Final response" in prompt:
                runs["n"] += 1
                if runs["n"] == 1:
                    # A human commits to the target while Choobi is thinking.
                    (self.root / "docs/api.md").write_text(
                        "---\ncovers: src/api.py\n---\n# API\n\nHuman edit.\n")
                    _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "human docs")
                return _link()
            return _upd("---\ncovers: src/api.py\n---\n# API\n\nHuman edit. Default 3.\n")

        result = engine.run_update_guarded(self.root, self._anchored(), self.cfg,
                                           FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertEqual(runs["n"], 2)
        self.assertIn("Human edit. Default 3.", (self.root / "docs/api.md").read_text())
        self.assertEqual([r["status"] for r in history.recent(self.repo_id)], ["committed"])

    def test_silent_carries_an_assessment(self) -> None:
        def answer(prompt: str) -> str:
            if "## Final response" in prompt:
                return _link()
            self.assertIn("one-sentence", prompt)
            return _silent("checked docs/api.md: the retry default is already documented")

        result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual((result.status, result.reason), ("no_op", "model_silent"))
        self.assertIn("already documented", history.recent(self.repo_id, limit=1)[0]["summary"])

    def test_ownership_findings_reach_the_editor(self) -> None:
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            if "## Final response" in prompt:
                return _link(findings="retry() gained n=3 default in src/api.py:1")
            return _upd()

        engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertIn("findings from the ownership review: retry() gained n=3", prompts[1])

    def test_creation_review_sees_existing_documents(self) -> None:
        repos_mod = __import__("choobi.repos", fromlist=["repos"])
        repos_mod.save_sop(self.repo_id,
                           "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n")
        (self.root / "src/export.py").write_text('"""Public export API."""\ndef export(): pass\n')
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "add export")
        head = gitio.resolve(self.root, "HEAD")
        seen = {}

        def answer(prompt: str) -> str:
            if "proposes CREATING" in prompt:
                seen["review"] = prompt
                return json.dumps({"approve": True, "reason": "new surface"})
            if "## Final response" in prompt:
                return json.dumps({"action": "create", "doc": "", "area": "export",
                                   "scope": "area"})
            return json.dumps({"disposition": "create", "target": "docs/internal/features/export.md",
                               "summary": "documented export", "content": "# Export\n\nExports.\n",
                               "source_paths": ["src/export.py"]})

        result = engine.run_update(self.root, self._anchored(head), self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertIn("Existing documents", seen["review"])
        self.assertIn("docs/api.md | title: API", seen["review"])

    def test_chat_only_detached_run_reviews_ownership_with_the_conversation(self) -> None:
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return _link() if "## Final response" in prompt else _upd()

        req = UpdateRequest(detached=True, chat_context="DECISION: retries default to 3",
                            trigger="agent_chat")
        result = engine.run_update(self.root, req, self.cfg, FakeRuntime(answer))
        self.assertEqual(result.status, "committed")
        self.assertIn("## Conversation context", prompts[0])
        self.assertIn("retries default to 3", prompts[0])

    def test_committed_docs_are_pushed_where_the_developer_already_pushed(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")

        def answer(prompt: str) -> str:
            return _link() if "## Final response" in prompt else _upd()

        result = engine.run_update(self.root, self._anchored(), self.cfg, FakeRuntime(answer))
        self.assertEqual((result.status, result.push), ("committed", pushing.PUSHED))
        self.assertIn("Pushed to your branch", result.completion_message)
        self.assertEqual(_git(self.remote, "rev-parse", "main"), result.docs_commit)
        self.assertEqual(history.recent(self.repo_id, limit=1)[0]["push"], pushing.PUSHED)

    def test_unpushed_branch_is_never_pushed_by_choobi(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        (self.root / "src/api.py").write_text("def retry(n=4): return n\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "not pushed yet")
        head = gitio.resolve(self.root, "HEAD")

        def answer(prompt: str) -> str:
            return _link() if "## Final response" in prompt else _upd()

        result = engine.run_update(self.root, self._anchored(head), self.cfg, FakeRuntime(answer))
        self.assertEqual((result.status, result.push), ("committed", pushing.NOT_PUBLISHED))
        self.assertEqual(_git(self.remote, "rev-parse", "main"), self.head)


def _commit(root: Path, path: str, content: str, msg: str) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)
    _git(root, "add", "-A"); _git(root, "commit", "-qm", msg)
    return gitio.resolve(root, "HEAD")


class CoalesceTest(HarnessCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo_id = config.checkout_id(gitio.common_dir(self.root))

    def test_burst_folds_older_jobs_into_the_newest_and_widens_its_range(self) -> None:
        a = self.head
        b = _commit(self.root, "src/b.py", "b = 1\n", "b")
        c = _commit(self.root, "src/c.py", "c = 1\n", "c")
        da = coalesce.decide(self.root, self.repo_id, a, cli.EMPTY_TREE)
        self.assertEqual((da.action, da.into), (coalesce.COALESCED, c))
        history.add_record(self.repo_id, str(self.root), "post_commit", "coalesced",
                           source_commit=a, reason="coalesced_into_newer_commit")
        db = coalesce.decide(self.root, self.repo_id, b, cli.EMPTY_TREE)
        self.assertEqual(db.action, coalesce.COALESCED)
        history.add_record(self.repo_id, str(self.root), "post_commit", "coalesced",
                           source_commit=b, reason="coalesced_into_newer_commit")
        dc = coalesce.decide(self.root, self.repo_id, c, cli.EMPTY_TREE)
        self.assertEqual(dc.action, coalesce.RUN)
        self.assertEqual(dc.rev_range, f"{gitio.resolve(self.root, a + '^')}..{c}")
        self.assertEqual(set(gitio.changed_files(self.root, dc.rev_range)),
                         {"src/api.py", "src/b.py", "src/c.py"})

    def test_choobi_own_docs_commit_does_not_coalesce_the_job(self) -> None:
        docs_commit = _commit(self.root, "docs/api.md", NEW_DOC, "add configurable retry backoff")
        history.add_record(self.repo_id, str(self.root), "post_commit", "committed",
                           source_commit=self.head, docs_commit=docs_commit)
        (self.root / "src/api.py").write_text("def retry(n=4): return n\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "later human commit")
        later = gitio.resolve(self.root, "HEAD")
        # A job whose only "newer" commit is Choobi's own docs commit still runs.
        _git(self.root, "reset", "-q", "--hard", docs_commit)
        d = coalesce.decide(self.root, self.repo_id, self.head, cli.EMPTY_TREE)
        self.assertEqual((d.action, d.rev_range),
                         (coalesce.RUN, f"{gitio.resolve(self.root, self.head + '^')}..{self.head}"))
        _git(self.root, "reset", "-q", "--hard", later)
        self.assertEqual(coalesce.decide(self.root, self.repo_id, self.head,
                                         cli.EMPTY_TREE).action, coalesce.COALESCED)

    def test_amended_away_commit_is_unreachable_and_switched_branch_still_runs(self) -> None:
        _git(self.root, "commit", "-q", "--amend", "-m", "amended")
        self.assertEqual(coalesce.decide(self.root, self.repo_id, self.head,
                                         cli.EMPTY_TREE).action, coalesce.UNREACHABLE)
        # Back on a branch that contains it, but checked out elsewhere: run (the engine parks).
        _git(self.root, "branch", "-f", "keep", self.head)
        _git(self.root, "checkout", "-q", "-b", "elsewhere", "HEAD^")
        self.assertEqual(coalesce.decide(self.root, self.repo_id, self.head,
                                         cli.EMPTY_TREE).action, coalesce.RUN)

    def test_cli_post_commit_burst_produces_one_docs_commit(self) -> None:
        b = _commit(self.root, "src/api.py", "def retry(n=3, backoff=2): return n\n", "add backoff")
        seen = []

        def answer(prompt: str) -> str:
            seen.append(prompt)
            return _link() if "## Final response" in prompt else _upd()

        def run(sha: str) -> None:
            args = cli._build_parser().parse_args(
                ["update", "--commit", sha, "--trigger", "post_commit"])
            with mock.patch("choobi.cli.gitio.repo_root", return_value=self.root), \
                 mock.patch("choobi.cli.config.Config.load", return_value=config.Config()), \
                 mock.patch("choobi.cli.get_runtime", return_value=FakeRuntime(answer)), \
                 mock.patch("builtins.print"):
                self.assertEqual(cli._cmd_update(args, None), 0)

        run(self.head)          # older job wakes up after b exists -> coalesced, no model call
        self.assertEqual(seen, [])
        run(b)
        statuses = [(r["source_commit"], r["status"]) for r in history.recent(self.repo_id)]
        self.assertEqual(statuses, [(b, "committed"), (self.head, "coalesced")])
        self.assertIn("def retry(n=3, backoff=2)", seen[0])
        self.assertIn("-def retry(): pass", seen[0])          # widened back over `head`
        self.assertEqual(gitio.resolve(self.root, "HEAD^"), b)

    def test_engine_builds_on_the_owning_branch_when_user_already_switched(self) -> None:
        _git(self.root, "checkout", "-q", "-b", "elsewhere", "HEAD^")

        def answer(prompt: str) -> str:
            return _link() if "## Final response" in prompt else _upd()

        req = UpdateRequest(source_commit=self.head, rev_range=f"{self.head}^..{self.head}",
                            trigger="post_commit")
        result = engine.run_update(self.root, req, config.Config(), FakeRuntime(answer))
        self.assertEqual(result.status, "parked")
        self.assertEqual(gitio.resolve(self.root, f"{result.docs_commit}^"), self.head)
        self.assertIn("elsewhere", result.reason)


class StatusTest(HarnessCase):
    def test_status_lists_parked_commits_with_the_apply_hint(self) -> None:
        pending = _park(self)
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        history.add_record(repo_id, str(self.root), "post_commit", "parked",
                           source_commit=self.head, docs_commit=pending,
                           reason="checked-out branch is other")
        out = status.render(self.root)
        self.assertIn("choobi apply", out)
        self.assertIn(self.head[:7], out)
        self.assertIn("checked-out branch is other", out)
        report = status.report(self.root)
        self.assertEqual(report["parked"][0]["pending"], pending)


class AuditTest(HarnessCase):
    def test_audit_reports_contradicted_claims_and_writes_nothing_to_the_repo(self) -> None:
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            self.assertIn("Retries once.", prompt)
            self.assertIn("def retry(n=3)", prompt)
            return json.dumps({"findings": [
                {"claim": "Retries once.", "status": "contradicted",
                 "evidence": "src/api.py:1 retry(n=3) defaults to three attempts"},
                {"claim": "Works offline.", "status": "unverified",
                 "evidence": "no network code is covered by this document"},
            ]})

        findings, notes = audit.run_audit(self.root, config.Config(), FakeRuntime(answer))
        self.assertEqual(len(prompts), 1)                       # README.md has no covers: entry
        self.assertEqual([f.status for f in findings], ["contradicted", "unverified"])
        self.assertEqual(findings[0].doc, "docs/api.md")
        self.assertEqual(len(notes), 1)
        self.assertIn("README.md: skipped", notes[0])
        self.assertEqual(_git(self.root, "status", "--porcelain"), "")
        self.assertEqual(gitio.resolve(self.root, "HEAD"), self.head)
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        report = audit.report_path(repo_id).read_text()
        self.assertIn("## Contradicted", report)
        self.assertIn("**Retries once.**", report)
        self.assertIn("## Skipped", report)
        record = history.recent(repo_id, limit=1)[0]
        self.assertEqual(record["status"], "audit")
        self.assertIn("1 contradicted, 1 unverified, 1 skipped", record["summary"])

    def test_audit_rejects_malformed_findings_then_accepts_a_corrected_answer(self) -> None:
        calls = {"n": 0}

        def answer(prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps({"findings": [{"claim": "x", "status": "wrong"}]})
            self.assertIn("Previous answer was rejected", prompt)
            return json.dumps({"findings": []})

        findings, _ = audit.run_audit(self.root, config.Config(), FakeRuntime(answer))
        self.assertEqual(findings, [])
        self.assertEqual(calls["n"], 2)

    def test_cli_audit_prints_the_report(self) -> None:
        with mock.patch("choobi.cli.gitio.repo_root", return_value=self.root), \
             mock.patch("choobi.cli.config.Config.load", return_value=config.Config()), \
             mock.patch("choobi.cli.get_runtime",
                        return_value=FakeRuntime(json.dumps({"findings": []}))), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(cli.main(["audit"]), 0)
        text = "\n".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("# choobi audit", text)
        self.assertIn("report saved to", text)


class HookEndToEndTest(HarnessCase):
    """The real post-commit hook, a real detached process, a fake model: commit and walk away."""

    def _env(self, responses: list) -> dict:
        env = dict(os.environ)
        env.update({
            "CHOOBI_HOME": self._home.name,
            "CHOOBI_RUNTIME": "fake",
            "CHOOBI_FAKE_RESPONSE": json.dumps(responses),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PATH": os.environ.get("PATH", ""),
        })
        return env

    def _wait_for(self, predicate, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.25)
        return False

    def test_commit_and_push_carries_the_docs_commit_to_the_same_branch(self) -> None:
        add_remote(self.root, self.remote)
        _git(self.root, "push", "-q", "-u", "origin", "main")
        hooks.install(self.root)
        env = self._env([_link(), _upd()])

        (self.root / "src/api.py").write_text("def retry(n=3, jitter=True): return n\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "add retry jitter"], cwd=self.root, check=True,
                       env=env, capture_output=True)
        source = gitio.resolve(self.root, "HEAD")
        subprocess.run(["git", "push", "-q"], cwd=self.root, check=True, env=env,
                       capture_output=True)                       # the developer pushes at once

        landed = self._wait_for(lambda: gitio.resolve(self.root, "HEAD") != source)
        self.assertTrue(landed, (Path(self._home.name) / "logs/hook.log").read_text())
        docs_commit = gitio.resolve(self.root, "HEAD")
        self.assertEqual(gitio.resolve(self.root, "HEAD^"), source)
        self.assertEqual(gitio.commit_subject(self.root, docs_commit), "add retry jitter")
        self.assertEqual((self.root / "docs/api.md").read_text(), NEW_DOC)
        self.assertEqual(_git(self.root, "status", "--porcelain", "--", "docs", "src"), "")
        # Choobi pushed its commit because the developer had already pushed the source commit.
        self.assertTrue(self._wait_for(
            lambda: _git(self.remote, "rev-parse", "main") == docs_commit))
        # The docs commit's own hook exited (CHOOBI_GENERATING); exactly one background run.
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        self.assertTrue(self._wait_for(lambda: len(history.recent(repo_id)) >= 1))
        time.sleep(1.0)
        records = history.recent(repo_id)
        self.assertEqual([r["status"] for r in records], ["committed"])
        self.assertEqual(records[0]["push"], pushing.PUSHED)

    def test_commit_while_editing_the_doc_parks_instead_of_touching_it(self) -> None:
        hooks.install(self.root)
        env = self._env([_link(), _upd()])
        (self.root / "src/api.py").write_text("def retry(n=3, jitter=True): return n\n")
        subprocess.run(["git", "add", "src/api.py"], cwd=self.root, check=True, env=env)
        (self.root / "docs/api.md").write_text("# I am mid-edit\n")     # unstaged, untouched
        subprocess.run(["git", "commit", "-qm", "add retry jitter"], cwd=self.root, check=True,
                       env=env, capture_output=True)
        source = gitio.resolve(self.root, "HEAD")
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        self.assertTrue(self._wait_for(lambda: bool(history.recent(repo_id))),
                        (Path(self._home.name) / "logs/hook.log").read_text())
        record = history.recent(repo_id)[0]
        self.assertEqual(record["status"], "parked")
        self.assertEqual(gitio.resolve(self.root, "HEAD"), source)
        self.assertEqual((self.root / "docs/api.md").read_text(), "# I am mid-edit\n")
        self.assertEqual(list(gitio.pending_refs(self.root)), [source])
        self.assertIn("choobi apply", status.render(self.root))


if __name__ == "__main__":
    unittest.main()
