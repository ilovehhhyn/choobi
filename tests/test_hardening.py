"""Regression tests for Choobi's automatic-write and harness boundaries."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from unittest import mock

from choobi import (
    agent_skill, auth, baseline, cli, commitwriter, config, docs, engine, gitio, help as help_mod,
    hooks, pr, repos, verify,
)
from choobi.errors import (
    ChoobiError, CommitFailed, Conflict, HookConflict, InvalidScope, InvalidSop, NotAllowedPath,
    PendingDocsUpdate, RuntimeOutputInvalid, RuntimeUnavailable, VerificationFailed,
)
from choobi.runtime import ClaudeCliRuntime, FakeRuntime, get_runtime
from choobi.ui import server as ui_server


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _repo() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    tmp = tempfile.TemporaryDirectory(prefix="choobi-hardening-")
    root = Path(tmp.name)
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "docs/api.md").write_text("---\ncovers: src/api.py\n---\n# API\n\nRetries once.\n")
    (root / "src/api.py").write_text("def retry(): pass\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return tmp, root


class HardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory(prefix="choobi-hardening-home-")
        os.environ["CHOOBI_HOME"] = self.home.name
        self.repo_tmp, self.root = _repo()

    def tearDown(self) -> None:
        self.repo_tmp.cleanup()
        self.home.cleanup()

    def test_allowlist_rejects_traversal(self) -> None:
        policy = baseline.policy()
        self.assertFalse(docs.is_allowed("docs/../../escape.md", policy))
        self.assertFalse(docs.is_allowed("/tmp/escape.md", policy))
        with self.assertRaises(ChoobiError):
            config.repo_dir("../../outside")

    def test_baseline_files_live_inside_the_installable_package(self) -> None:
        package_dir = Path(baseline.__file__).resolve().parent
        self.assertTrue((package_dir / "baseline/style.md").is_file())
        self.assertTrue((package_dir / "baseline/policy.yaml").is_file())

    def test_ui_uses_one_random_line_art_face_per_session(self) -> None:
        static = Path(ui_server.__file__).resolve().parent / "static"
        faces = sorted((static / "line-art").glob("face-*.png"))
        html = (static / "index.html").read_text()
        app = (static / "app.js").read_text()
        styles = (static / "styles.css").read_text()

        self.assertEqual(len(faces), 18)
        self.assertEqual(html.count('class="blob choobi-face"'), 2)
        self.assertIn("edit choobi's sop for each repo", html)
        self.assertIn("edit choobi's overall style", html)
        self.assertIn("view choobi's work", html)
        self.assertNotIn("cheese", html + styles)
        self.assertEqual(app.count("Math.floor(Math.random() * FACE_COUNT)"), 1)
        self.assertIn('querySelectorAll(".choobi-face")', app)
        self.assertIn('id="ob-runtime"', html)
        self.assertIn("sign in with claude", html)
        self.assertNotIn("<footer", html)
        self.assertIn('aria-label="close commands">×</button>', html.replace("\n", " "))
        self.assertIn("header { padding: 14px 12px 8px; border-bottom: 1px solid #000", styles)
        self.assertIn("min_size=(340, 480)", Path(ui_server.__file__).read_text())

        httpd, url = ui_server.start_server()
        parsed = urlparse(url)
        face_url = f"{parsed.scheme}://{parsed.netloc}/static/line-art/face-1.png"
        try:
            with urllib.request.urlopen(face_url) as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_onboarding_selects_runtime_and_requires_successful_login(self) -> None:
        httpd, url = ui_server.start_server()
        parsed = urlparse(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/api/onboard?{parsed.query}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"name": "Helen", "agent": "claude"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with mock.patch("choobi.ui.server.auth.ensure", return_value=["logged in"]), \
                 mock.patch("choobi.ui.server.auth.is_logged_in", return_value=True), \
                 urllib.request.urlopen(request) as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["runtime_state"], "ready")
            cfg = config.Config.load()
            self.assertEqual(cfg.name, "Helen")
            self.assertEqual(cfg.agent, "claude")
            self.assertTrue(cfg.onboarded)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_style_api_edits_a_full_personal_copy(self) -> None:
        httpd, url = ui_server.start_server()
        parsed = urlparse(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/api/style?{parsed.query}"
        try:
            with urllib.request.urlopen(endpoint) as response:
                payload = json.load(response)
            self.assertEqual(
                payload, {"content": baseline.baseline_style(), "is_personal": False}
            )

            customized = baseline.baseline_style().replace(
                "Use sentence-case headings", "Use title-case headings"
            )
            request = urllib.request.Request(
                endpoint.replace("/api/style?", "/api/style/save?"),
                data=json.dumps({"content": customized}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)["is_personal"])
            self.assertEqual(config.personal_style_path().read_text(), customized)
            self.assertEqual(baseline.resolved_style(), customized)

            reset = urllib.request.Request(
                endpoint.replace("/api/style?", "/api/style/reset?"),
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(reset) as response:
                self.assertEqual(
                    json.load(response), {"ok": True, "is_personal": False,
                                          "content": baseline.baseline_style()}
                )
            self.assertFalse(config.personal_style_path().exists())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_write_rejects_symlink_target(self) -> None:
        outside = Path(self.home.name) / "outside.md"
        outside.write_text("safe\n")
        (self.root / "docs/api.md").unlink()
        (self.root / "docs/api.md").symlink_to(outside)
        with self.assertRaises(NotAllowedPath):
            verify.check_write(
                self.root, "docs/api.md", "unsafe\n", is_create=False,
                expected_hash=gitio.file_hash(self.root, "docs/api.md"), policy=baseline.policy(),
            )
        self.assertEqual(outside.read_text(), "safe\n")

    def test_linkage_rejects_symlink_document_evidence(self) -> None:
        outside = Path(self.home.name) / "outside.md"
        outside.write_text("---\ncovers: src/api.py\n---\n# Outside\n")
        (self.root / "docs/api.md").unlink()
        (self.root / "docs/api.md").symlink_to(outside)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "replace doc with symlink")
        with self.assertRaises(NotAllowedPath):
            docs.candidate_docs(self.root, ["src/api.py"], baseline.policy())

    def test_prompt_rejects_symlink_source_evidence(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n",
        )
        outside = Path(self.home.name) / "outside.py"
        outside.write_text("EXTERNAL_SENTINEL = True\n")
        (self.root / "src/leak.py").symlink_to(outside)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add source symlink")
        head = gitio.resolve(self.root, "HEAD")

        def called(_prompt: str) -> str:
            raise AssertionError("external symlink content reached runtime")

        with self.assertRaises(NotAllowedPath):
            engine.run_update(
                self.root,
                engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
                config.Config(onboarded=True), FakeRuntime(called),
            )

    def test_changed_doc_remains_a_candidate_for_changed_code(self) -> None:
        candidates = docs.candidate_docs(
            self.root, ["src/api.py", "docs/api.md"], baseline.policy()
        )
        self.assertEqual(candidates, ["docs/api.md"])
        self.assertEqual(docs.candidate_docs(self.root, ["docs/api.md"], baseline.policy()), [])

    def test_target_must_be_clean(self) -> None:
        target = self.root / "docs/api.md"
        target.write_text(target.read_text() + "local edit\n")
        with self.assertRaises(Conflict):
            verify.check_write(
                self.root, "docs/api.md", target.read_text(), is_create=False,
                expected_hash=gitio.file_hash(self.root, "docs/api.md"), policy=baseline.policy(),
            )

    def test_covers_must_resolve(self) -> None:
        content = "---\ncovers: src/missing.py\n---\n# API\n"
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/api.md", content, is_create=False,
                expected_hash=gitio.file_hash(self.root, "docs/api.md"), policy=baseline.policy(),
            )

    def test_covers_must_be_a_string_or_string_list(self) -> None:
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/api.md", "---\ncovers: 123\n---\n# API\n",
                is_create=False, expected_hash=gitio.file_hash(self.root, "docs/api.md"),
                policy=baseline.policy(),
            )

    def test_links_cannot_escape_repository(self) -> None:
        outside = Path(self.home.name) / "outside.md"
        outside.write_text("private\n")
        relative = os.path.relpath(outside, self.root / "docs")
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/api.md", f"# API\n\n[private]({relative})\n",
                is_create=False, expected_hash=gitio.file_hash(self.root, "docs/api.md"),
                policy=baseline.policy(),
            )
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/api.md", f"# API\n\n[private](<{relative}>)\n",
                is_create=False, expected_hash=gitio.file_hash(self.root, "docs/api.md"),
                policy=baseline.policy(),
            )

    def test_created_examples_must_come_from_evidence(self) -> None:
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/new.md", "# New\n\n```python\ninvented()\n```\n",
                is_create=True, expected_hash=None, policy=baseline.policy(), evidence="",
            )

    def test_output_must_be_utf8(self) -> None:
        with self.assertRaises(VerificationFailed):
            verify.check_write(
                self.root, "docs/api.md", "# API\n\ud800\n", is_create=False,
                expected_hash=gitio.file_hash(self.root, "docs/api.md"),
                policy=baseline.policy(),
            )
    def test_created_examples_can_use_verified_source_content(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n",
        )
        source = "def enabled():\n    return True\n"
        (self.root / "src/new_api.py").write_text(source)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add public api")
        head = gitio.resolve(self.root, "HEAD")
        response = json.dumps({
            "disposition": "create", "target": "docs/internal/features/new-api.md",
            "summary": "documented the new API",
            "content": f"# New API\n\n```python\n{source}```\n",
            "source_paths": ["src/new_api.py"],
        })
        result = engine.run_update(
            self.root, engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
            config.Config(onboarded=True), FakeRuntime([
                json.dumps({"action": "create", "doc": "", "area": "public API",
                            "scope": "area"}),
                response,
            ]),
        )
        self.assertEqual(result.status, "committed")

    def test_create_rejects_a_staged_deletion(self) -> None:
        target = "docs/internal/features/old.md"
        path = self.root / target
        path.parent.mkdir(parents=True)
        path.write_text("# Old\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add old doc")
        path.unlink()
        _git(self.root, "add", "-A")
        with self.assertRaises(Conflict):
            verify.check_write(
                self.root, target, "# Recreated\n", is_create=True, expected_hash=None,
                policy=baseline.policy(),
            )

    def test_commit_failure_restores_clean_target(self) -> None:
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
        before = (self.root / "docs/api.md").read_text()
        with self.assertRaises(CommitFailed):
            commitwriter.write_and_commit(
                self.root, {"docs/api.md": "# overwritten\n"}, "docs: fail",
                source_commit=gitio.resolve(self.root, "HEAD"),
                expected_hashes={"docs/api.md": gitio.file_hash(self.root, "docs/api.md")},
            )
        self.assertEqual((self.root / "docs/api.md").read_text(), before)
        self.assertEqual(gitio._run(self.root, "status", "--porcelain"), "")

    def test_os_commit_failure_restores_clean_target(self) -> None:
        before = (self.root / "docs/api.md").read_text()
        with mock.patch("choobi.commitwriter.gitio.commit_paths", side_effect=OSError("disk full")):
            with self.assertRaises(CommitFailed):
                commitwriter.write_and_commit(
                    self.root, {"docs/api.md": "# overwritten\n"}, "docs: fail",
                    source_commit=gitio.resolve(self.root, "HEAD"),
                    expected_hashes={"docs/api.md": gitio.file_hash(self.root, "docs/api.md")},
                )
        self.assertEqual((self.root / "docs/api.md").read_text(), before)
        self.assertEqual(gitio._run(self.root, "status", "--porcelain"), "")

    def test_failed_commit_preserves_a_concurrent_user_save(self) -> None:
        target = self.root / "docs/api.md"

        def fail_after_user_save(*_args: object, **_kwargs: object) -> str:
            target.write_text("# Human save during commit\n")
            raise RuntimeError("signing failed")

        with mock.patch("choobi.commitwriter.gitio.commit_paths", side_effect=fail_after_user_save):
            with self.assertRaises(CommitFailed):
                commitwriter.write_and_commit(
                    self.root, {"docs/api.md": "# Model version\n"}, "docs: fail",
                    source_commit=gitio.resolve(self.root, "HEAD"),
                    expected_hashes={"docs/api.md": gitio.file_hash(self.root, "docs/api.md")},
                )
        self.assertEqual(target.read_text(), "# Human save during commit\n")

    def test_automatic_commit_is_built_off_checkout(self) -> None:
        source = gitio.resolve(self.root, "HEAD")
        expected = gitio.file_hash(self.root, "docs/api.md")
        result = commitwriter.write_and_commit(
            self.root,
            {"docs/api.md": "---\ncovers: src/api.py\n---\n# API\n\nRetries three times.\n"},
            "docs: isolate",
            source_commit=source,
            expected_hashes={"docs/api.md": expected},
        )
        self.assertEqual(result, gitio.resolve(self.root, "HEAD"))
        self.assertIn("Retries three", (self.root / "docs/api.md").read_text())
        self.assertEqual(gitio._run(self.root, "status", "--porcelain"), "")

    def test_automatic_commit_uses_the_verified_target_hash(self) -> None:
        source = gitio.resolve(self.root, "HEAD")
        expected = gitio.file_hash(self.root, "docs/api.md")
        (self.root / "docs/api.md").write_text("# Human committed after verification\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "human docs edit")

        with self.assertRaises(Conflict):
            commitwriter.write_and_commit(
                self.root, {"docs/api.md": "# Model version\n"}, "docs: isolate",
                source_commit=source,
                expected_hashes={"docs/api.md": expected},
            )
        self.assertEqual(
            (self.root / "docs/api.md").read_text(), "# Human committed after verification\n"
        )

    def test_document_text_and_hash_are_one_snapshot(self) -> None:
        response = json.dumps({
            "disposition": "update", "target": "docs/api.md", "summary": "model update",
            "content": "---\ncovers: src/api.py\n---\n# API\n\nModel from old body.\n",
            "source_paths": [],
        })
        read_snapshot = docs.read_snapshot
        raced = False

        def commit_after_snapshot(root: Path, path: str) -> tuple[str, str]:
            nonlocal raced
            snapshot = read_snapshot(root, path)
            if path == "docs/api.md" and not raced:
                raced = True
                (root / path).write_text(
                    "---\ncovers: src/api.py\n---\n# API\n\nHuman committed body.\n"
                )
                _git(root, "add", path)
                _git(root, "commit", "-qm", "human docs edit")
            return snapshot

        with mock.patch("choobi.engine.docs.read_snapshot", side_effect=commit_after_snapshot):
            with self.assertRaises(Conflict):
                engine.run_update(
                    self.root,
                    engine.UpdateRequest(targets=["docs/api.md"], detached=True,
                                         instruction="update"),
                    config.Config(onboarded=True), FakeRuntime(response),
                )
        self.assertIn("Human committed body.", (self.root / "docs/api.md").read_text())

    def test_isolated_writer_rejects_a_concurrent_symlink_commit(self) -> None:
        source = gitio.resolve(self.root, "HEAD")
        expected = gitio.file_hash(self.root, "docs/api.md")
        victim = Path(self.home.name) / "victim.md"
        original = (self.root / "docs/api.md").read_text()
        victim.write_text(original)
        (self.root / "docs/api.md").unlink()
        (self.root / "docs/api.md").symlink_to(victim)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "replace doc with symlink")

        with mock.patch("choobi.commitwriter.gitio.commit_paths",
                        side_effect=RuntimeError("must not reach the writer")) as commit_paths:
            with self.assertRaises(NotAllowedPath):
                commitwriter.write_and_commit(
                    self.root, {"docs/api.md": "# Model\n"}, "docs: reject symlink",
                    source_commit=source, expected_hashes={"docs/api.md": expected},
                )
        commit_paths.assert_not_called()
        self.assertEqual(victim.read_text(), original)

    def test_install_refuses_to_overwrite_an_unmanaged_hook(self) -> None:
        hook = self.root / ".git/hooks/post-commit"
        hook.write_text("#!/bin/sh\necho keep-me\n")
        with self.assertRaises(HookConflict):
            hooks.install(self.root)
        self.assertIn("keep-me", hook.read_text())

    def test_creation_is_opt_in(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        self.assertFalse(repos.sop_allows_create(repo_id, str(self.root)))
        default = repos.default_sop(str(self.root))
        self.assertIn("data retention periods", default)
        self.assertIn("privacy boundaries", default)
        self.assertIn("user-visible configuration keys", default)
        self.assertIn("Repository areas and cross-cutting features", default)

    def test_invalid_sop_fails_instead_of_disabling_policy(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        with self.assertRaises(InvalidSop):
            repos.save_sop(repo_id, "---\nallow_create: [\n---\n")

    def test_creation_stays_inside_sop_roots(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n",
        )
        response = json.dumps({
            "disposition": "create", "target": "docs/public/new.md", "summary": "x",
            "content": "# New\n\nSupported behavior.\n", "source_paths": [],
        })
        with self.assertRaises(NotAllowedPath):
            engine.run_update(
                self.root,
                engine.UpdateRequest(targets=["docs/public/new.md"], detached=True,
                                     instruction="create it"),
                config.Config(onboarded=True), FakeRuntime(response),
            )

    def test_denied_creation_is_a_gap_not_silence(self) -> None:
        (self.root / "src/new_api.py").write_text('"""Public API."""\n')
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add public api")
        head = gitio.resolve(self.root, "HEAD")
        response = json.dumps({"action": "create", "doc": "", "area": "public API",
                               "scope": "area"})
        result = engine.run_update(
            self.root,
            engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
            config.Config(onboarded=True), FakeRuntime(response),
        )
        self.assertEqual((result.status, result.reason), ("gap", "documentation_gap"))

    def test_failed_generation_does_not_advance_snapshot(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_snapshot(repo_id, ["src/api.py"], gitio.resolve(self.root, "HEAD"))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n"
            "Use docs/internal/features/.\n",
        )
        (self.root / "src/new_api.py").write_text('"""Public API."""\n')
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add public api")
        head = gitio.resolve(self.root, "HEAD")
        with self.assertRaises(RuntimeOutputInvalid):
            engine.run_update(
                self.root,
                engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
                config.Config(onboarded=True), FakeRuntime("not json"),
            )
        self.assertEqual(repos.load_snapshot(repo_id), {"src/api.py"})

    def test_corrupt_snapshot_fails_loudly(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        path = repos.snapshot_path(repo_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        with self.assertRaises(ChoobiError):
            repos.load_snapshot(repo_id)

    def test_drift_content_is_visible_and_unselected_drift_stays_pending(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_snapshot(repo_id, ["src/api.py"], gitio.resolve(self.root, "HEAD"))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n",
        )
        (self.root / "src/alpha.py").write_text("def alpha():\n    return 'ALPHA_SENTINEL'\n")
        (self.root / "src/beta.py").write_text("def beta():\n    return 'BETA_SENTINEL'\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "missed public APIs")
        (self.root / "notes.txt").write_text("unrelated follow-up\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "current hook event")
        head = gitio.resolve(self.root, "HEAD")

        response = json.dumps({
            "disposition": "create", "target": "docs/internal/features/alpha.md",
            "summary": "documented alpha", "content": "# Alpha\n\nReturns alpha.\n",
            "source_paths": ["src/alpha.py"],
        })

        def answer(prompt: str) -> str:
            self.assertIn("ALPHA_SENTINEL", prompt)
            self.assertIn("BETA_SENTINEL", prompt)
            if "## Final response" in prompt:
                return json.dumps({"action": "create", "doc": "", "area": "public API",
                                   "scope": "cross_cutting"})
            return response

        result = engine.run_update(
            self.root,
            engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}",
                                 trigger="post_commit"),
            config.Config(onboarded=True), FakeRuntime(answer),
        )
        self.assertEqual(result.status, "committed")
        snapshot = repos.load_snapshot(repo_id)
        self.assertIn("src/alpha.py", snapshot)
        self.assertNotIn("src/beta.py", snapshot)

    def test_config_never_persists_api_keys(self) -> None:
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text(json.dumps({"name": "t", "api_key": "secret"}))
        cfg = config.Config.load()
        cfg.save()
        self.assertFalse(hasattr(cfg, "api_key"))
        self.assertNotIn("api_key", json.loads(config.config_path().read_text()))

    def test_prompt_keeps_late_diff_files(self) -> None:
        early = "diff --git a/a.py b/a.py\n" + ("+noise\n" * 4000)
        late = "diff --git a/z.py b/z.py\n@@ -0,0 +1 @@\n+LATE_SENTINEL\n"
        prompt = engine._build_prompt(
            engine.UpdateRequest(instruction="document it"), early + late,
            {"docs/api.md": (self.root / "docs/api.md").read_text()}, baseline.policy(),
        )
        self.assertIn("LATE_SENTINEL", prompt)
        self.assertNotIn("truncated", prompt.lower())

    def test_prompt_keeps_late_chat_decisions(self) -> None:
        chat = ("early context\n" * 1000) + "FINAL_DECISION: retries are disabled\n"
        prompt = engine._build_prompt(
            engine.UpdateRequest(chat_context=chat), "",
            {"docs/api.md": (self.root / "docs/api.md").read_text()}, baseline.policy(),
        )
        self.assertIn("FINAL_DECISION", prompt)
        self.assertNotIn("truncated", prompt.lower())

    def test_oversized_prompt_fails_before_runtime(self) -> None:
        target = self.root / "docs/api.md"
        target.write_text(target.read_text() + ("large evidence\n" * 9000))
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add oversized doc")

        def called(_prompt: str) -> str:
            raise AssertionError("oversized prompt reached runtime")

        with self.assertRaises(ChoobiError):
            engine.run_update(
                self.root,
                engine.UpdateRequest(targets=["docs/api.md"], detached=True,
                                     instruction="update it"),
                config.Config(onboarded=True), FakeRuntime(called),
            )

    def test_auth_status_timeout_is_not_runtime_ready(self) -> None:
        with mock.patch("choobi.auth.shutil.which", return_value="/claude"), \
             mock.patch("choobi.auth.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(["claude"], 10)):
            self.assertIsNone(auth.is_logged_in("claude"))

    def test_secret_diff_never_reaches_runtime(self) -> None:
        (self.root / "src/api.py").write_text(
            'TOKEN = "sk-ant-1234567890abcdefghijkl"\n'
        )
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "accidentally commit a token")
        head = gitio.resolve(self.root, "HEAD")

        def called(_prompt: str) -> str:
            raise AssertionError("runtime received secret-shaped evidence")

        with self.assertRaises(VerificationFailed):
            engine.run_update(
                self.root,
                engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
                config.Config(onboarded=True), FakeRuntime(called),
            )

    def test_out_of_scope_coverage_is_rejected(self) -> None:
        response = json.dumps({
            "disposition": "update", "target": "docs/api.md", "summary": "x",
            "content": (self.root / "docs/api.md").read_text(),
            "source_paths": ["src/unrelated.py"],
        })
        with self.assertRaises(RuntimeOutputInvalid):
            engine.run_update(
                self.root, engine.UpdateRequest(targets=["docs/api.md"], detached=True,
                                                 instruction="update"),
                config.Config(onboarded=True), FakeRuntime(response),
            )

    def test_update_cannot_drop_frontmatter_or_live_covers(self) -> None:
        (self.root / "src/other.py").write_text("def other(): pass\n")
        (self.root / "docs/api.md").write_text(
            "---\ntitle: API reference\ncovers:\n  - src/api.py\n  - src/other.py\n---\n"
            "# API\n\nRetries once.\n"
        )
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "add API metadata")
        response = json.dumps({
            "disposition": "update", "target": "docs/api.md", "summary": "updated API",
            "content": "---\ncovers: src/api.py\n---\n# API\n\nRetries three times.\n",
            "source_paths": [],
        })
        with self.assertRaises(VerificationFailed):
            engine.run_update(
                self.root,
                engine.UpdateRequest(targets=["docs/api.md"], detached=True,
                                     instruction="update retries"),
                config.Config(onboarded=True), FakeRuntime(response),
            )

    def test_hook_persists_only_required_choobi_environment(self) -> None:
        os.environ["CHOOBI_SECRET_TEST"] = "do-not-persist"
        try:
            hooks.install(self.root)
            script = (self.root / ".git/hooks/post-commit").read_text()
        finally:
            os.environ.pop("CHOOBI_SECRET_TEST", None)
        self.assertNotIn("CHOOBI_SECRET_TEST", script)
        self.assertNotIn("do-not-persist", script)

    def test_runtime_schema_is_enforced_locally(self) -> None:
        response = json.dumps({
            "disposition": "silent", "target": "", "summary": "", "content": "",
            "source_paths": [], "unexpected": "field",
        })
        with self.assertRaises(RuntimeOutputInvalid):
            engine.run_update(
                self.root,
                engine.UpdateRequest(targets=["docs/api.md"], detached=True,
                                     instruction="inspect"),
                config.Config(onboarded=True), FakeRuntime(response),
            )

    def test_deleted_source_is_not_reintroduced_into_covers(self) -> None:
        (self.root / "src/api.py").unlink()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "remove api")
        head = gitio.resolve(self.root, "HEAD")
        response = json.dumps({
            "disposition": "update", "target": "docs/api.md", "summary": "remove old api",
            "content": "# API\n\nThe legacy API was removed.\n", "source_paths": [],
        })
        result = engine.run_update(
            self.root,
            engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
            config.Config(onboarded=True), FakeRuntime([
                json.dumps({"action": "doc", "doc": "docs/api.md", "area": "backend API",
                            "scope": "area"}),
                response,
            ]),
        )
        self.assertEqual(result.status, "committed")
        self.assertNotIn("src/api.py", (self.root / "docs/api.md").read_text())

    def test_cli_rejects_ambiguous_scopes_and_multiple_targets(self) -> None:
        parser = cli._build_parser()
        with self.assertRaises(InvalidScope):
            cli._validate_update_args(parser.parse_args(
                ["update", "docs/a.md", "docs/b.md", "--detached"]
            ))
        with self.assertRaises(InvalidScope):
            cli._validate_update_args(parser.parse_args(
                ["update", "docs/a.md", "--commit", "HEAD", "--detached"]
            ))
        with self.assertRaises(InvalidScope):
            cli._validate_update_args(parser.parse_args(
                ["update", "--commit", "HEAD", "--range", "HEAD^..HEAD"]
            ))
        with self.assertRaises(InvalidScope):
            cli._validate_update_args(parser.parse_args(
                ["update", "--commit", "HEAD", "--working"]
            ))

    def test_cli_lock_contention_is_a_typed_failure(self) -> None:
        args = cli._build_parser().parse_args(["update", "docs/api.md", "--detached"])
        silent = json.dumps({"disposition": "silent", "target": "", "summary": "",
                             "content": "", "source_paths": []})
        with mock.patch("choobi.cli.gitio.repo_root", return_value=self.root), \
             mock.patch("choobi.cli.config.Config.load", return_value=config.Config()), \
             mock.patch("choobi.cli.get_runtime", return_value=FakeRuntime(silent)), \
             mock.patch("choobi.cli.locking.RepoLock.acquire", return_value=False):
            with self.assertRaises(PendingDocsUpdate):
                cli._cmd_update(args, "update it")

    def test_pr_holds_the_update_lock_while_creating(self) -> None:
        lock = mock.Mock()
        lock.acquire.return_value = True
        with mock.patch("choobi.pr.locking.RepoLock", return_value=lock), \
             mock.patch("choobi.pr._gh", side_effect=["https://example.test/pr/1", "base head"]), \
             mock.patch("choobi.pr._has_docs_commit", return_value=False):
            self.assertEqual(pr.create(self.root), "https://example.test/pr/1")
        lock.acquire.assert_called_once_with()
        lock.release.assert_called_once_with()

    def test_help_matches_the_validated_update_scope(self) -> None:
        update = next(c for c in help_mod.COMMANDS if c["command"].startswith("choobi update"))
        self.assertIn("--pr <number>", update["detail"])
        self.assertIn("--detached --staged", update["detail"])
        self.assertNotIn("shows runtime status", update["detail"])

    def test_agent_skill_does_not_interpolate_chat_into_shell(self) -> None:
        body = agent_skill._skill_body()
        self.assertNotIn("printf '%s'", body)
        self.assertIn("<<'CHOOBI_CONTEXT'", body)
        self.assertIn("\nCHOOBI_CONTEXT\n", body)

    def test_runtime_rejects_tool_capable_codex_adapter(self) -> None:
        with self.assertRaises(RuntimeUnavailable):
            get_runtime(config.Config(agent="codex"))

    def test_claude_runtime_disables_tools_and_uses_schema(self) -> None:
        envelope = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": '{"disposition":"silent"}'}), stderr="",
        )
        schema = {"type": "object", "properties": {"disposition": {"type": "string"}}}
        with mock.patch("choobi.runtime.shutil.which", return_value="/claude"), \
             mock.patch("choobi.runtime.subprocess.run", return_value=envelope) as run:
            ClaudeCliRuntime().complete("prompt", "system", schema=schema)
        command = run.call_args.args[0]
        self.assertNotIn("prompt", command)
        self.assertEqual(run.call_args.kwargs["input"], "prompt")
        self.assertIn("--tools", command)
        self.assertIn("--json-schema", command)
        self.assertIn("--no-session-persistence", command)

    def test_pr_command_dispatches(self) -> None:
        with mock.patch("choobi.cli.gitio.repo_root", return_value=self.root), \
             mock.patch("choobi.cli.pr.create", return_value="https://example.test/pr/1") as create:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["pr", "create"]), 0)
        create.assert_called_once_with(self.root)

    def test_frontend_does_not_render_model_text_as_html(self) -> None:
        app = (Path(__file__).parents[1] / "choobi/ui/static/app.js").read_text()
        self.assertNotIn("li.innerHTML = `<span class=\"log-title\">${title}", app)
        self.assertIn('addEventListener("unhandledrejection"', app)
        self.assertIn("tabIndex = 0", app)
        self.assertIn("refreshClLogs", app)
        self.assertIn("setInterval", app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
