"""choobi v1 test suite. stdlib unittest, no external runner needed.

Each test builds a throwaway git repo and points CHOOBI_HOME at a temp dir, so nothing
touches the real ~/.choobi. The engine runs against a FakeRuntime, so no tokens are spent.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from choobi import baseline, config, docs, engine, evaluate, gitio, history, repos, status, views
from choobi.engine import UpdateRequest, run_update, _parse_disposition
from choobi.errors import (
    AmbiguousTarget, Conflict, RuntimeOutputInvalid, SourceCommitRequired,
    TargetNotFound, VerificationFailed,
)
from choobi.runtime import FakeRuntime


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True, text=True)


def make_repo(root: Path) -> str:
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "README.md").write_text("# demo\n")
    (root / "docs" / "api.md").write_text("---\ncovers: src/api.py\n---\n# API\n\nRetries once.\n")
    (root / "src" / "api.py").write_text("def retry(): pass\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.co")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    (root / "src" / "api.py").write_text("def retry(n=3): return n\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add configurable retry backoff")
    return gitio.resolve(root, "HEAD")


UPDATE_RESP = json.dumps({
    "disposition": "update", "target": "docs/api.md",
    "summary": "documented the configurable retry backoff in docs/api.md",
    "content": "---\ncovers: src/api.py\n---\n# API\n\nRetries up to n times (default 3).\n",
    "source_paths": [],
})
SILENT_RESP = json.dumps({
    "disposition": "silent", "target": "", "summary": "", "content": "",
    "source_paths": [],
})


class ChoobiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._home = tempfile.mkdtemp(prefix="choobi-home-")
        self._repo = tempfile.mkdtemp(prefix="choobi-repo-")
        os.environ["CHOOBI_HOME"] = self._home
        os.environ.pop("CHOOBI_RUNTIME", None)
        self.root = Path(self._repo)
        self.head = make_repo(self.root)
        self.cfg = config.Config(name="t", onboarded=True)

    # --- allowlist / discovery ---
    def test_allowlist_globs(self) -> None:
        pol = baseline.policy()
        self.assertTrue(docs.is_allowed("docs/api.md", pol))
        self.assertTrue(docs.is_allowed("docs/deep/nested/x.md", pol))
        self.assertTrue(docs.is_allowed("README.md", pol))
        self.assertFalse(docs.is_allowed("src/api.py", pol))
        self.assertFalse(docs.is_allowed("docs/api.txt", pol))

    def test_candidate_linkage(self) -> None:
        pol = baseline.policy()
        changed = gitio.changed_files(self.root, f"{self.head}^..{self.head}")
        cands = docs.candidate_docs(self.root, changed, pol)
        self.assertIn("docs/api.md", cands)      # via covers: src/api.py
        self.assertNotIn("README.md", cands)     # root README does not own the src/ subtree
        # a root-level change IS owned by the root README (direct child)
        (self.root / "config.md").write_text("x")  # not committed; test ownership only
        self.assertIn("README.md", docs.candidate_docs(self.root, ["setup.sh"], pol))

    def test_tracked_document_inventory_includes_full_read_only_and_generated_docs(self) -> None:
        (self.root / "CONTRIBUTING.md").write_text("# Contributing\n\nread-only owner body\n")
        (self.root / "docs" / "generated.mdx").write_text(
            "<!-- GENERATED DOCUMENT. DO NOT EDIT. -->\n# Generated\n\nfull body\n"
        )
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "add docs")
        inventory = {record.path: record for record in
                     docs.tracked_documents(self.root, baseline.policy())}
        self.assertFalse(inventory["CONTRIBUTING.md"].writable)
        self.assertIn("read-only owner body", inventory["CONTRIBUTING.md"].content)
        self.assertTrue(inventory["docs/generated.mdx"].writable)
        self.assertTrue(inventory["docs/generated.mdx"].generated)

    def test_unlinked_change_gate_has_skip_semantic_and_priority_tiers(self) -> None:
        self.assertEqual(engine._linkage_tier(["tests/test_api.py"], "+ assert True")[0], "skip")
        self.assertEqual(engine._linkage_tier(["src/panel.css"], "+ color: black")[0],
                         "semantic")
        tier, paths, signals = engine._linkage_tier(
            ["config/product.conf"], "+ terminal_retention_days = 5"
        )
        self.assertEqual(tier, "priority")
        self.assertEqual(paths, ["config/product.conf"])
        self.assertIn("data retention", signals)

    def test_resolve_target(self) -> None:
        pol = baseline.policy()
        self.assertEqual(docs.resolve_target(self.root, "docs/api.md", pol), "docs/api.md")
        self.assertEqual(docs.resolve_target(self.root, "api", pol), "docs/api.md")
        with self.assertRaises(TargetNotFound):
            docs.resolve_target(self.root, "zzz", pol)
        (self.root / "docs" / "apix.md").write_text("x")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "x")
        with self.assertRaises(AmbiguousTarget):
            docs.resolve_target(self.root, "api", pol)

    # --- disposition parsing ---
    def test_parse_disposition_variants(self) -> None:
        self.assertEqual(_parse_disposition(UPDATE_RESP)["disposition"], "update")
        fenced = "```json\n" + UPDATE_RESP + "\n```"
        self.assertEqual(_parse_disposition(fenced)["target"], "docs/api.md")
        chatty = "Sure!\n" + UPDATE_RESP + "\nHope that helps."
        self.assertEqual(_parse_disposition(chatty)["disposition"], "update")
        with self.assertRaises(RuntimeOutputInvalid):
            _parse_disposition("no json here")

    # --- engine happy paths ---
    def _run(self, req: UpdateRequest, resp: str) -> engine.UpdateResult:
        if not req.targets and isinstance(resp, str):
            try:
                parsed = json.loads(resp)
            except json.JSONDecodeError:
                parsed = {}
            if "disposition" in parsed:
                owner = parsed.get("target") or "docs/api.md"
                resp = [json.dumps({"action": "doc", "doc": owner, "area": "backend",
                                    "scope": "area"}), resp]
        return run_update(self.root, req, self.cfg, FakeRuntime(resp))

    def test_update_reuses_source_message(self) -> None:
        r = self._run(UpdateRequest(source_commit=self.head,
                                    rev_range=f"{self.head}^..{self.head}",
                                    trigger="post_commit"), UPDATE_RESP)
        self.assertEqual(r.status, "committed")
        self.assertEqual(gitio.commit_subject(self.root, r.docs_commit),
                         "add configurable retry backoff")
        self.assertIn("retry backoff", r.completion_message)

    def test_idempotent_post_commit(self) -> None:
        req = UpdateRequest(source_commit=self.head, rev_range=f"{self.head}^..{self.head}",
                            trigger="post_commit")
        self._run(req, UPDATE_RESP)
        before = gitio.resolve(self.root, "HEAD")
        r2 = self._run(req, UPDATE_RESP)
        self.assertEqual(gitio.resolve(self.root, "HEAD"), before)  # no second commit
        self.assertEqual(r2.status, "committed")

    def test_silent_is_no_op(self) -> None:
        r = self._run(UpdateRequest(targets=["docs/api.md"], source_commit=self.head,
                                    trigger="post_commit"), SILENT_RESP)
        self.assertEqual(r.status, "no_op")

    def test_no_candidates_no_model_call(self) -> None:
        # Test-only commits remain the narrow deterministic skip boundary.
        (self.root / "tests").mkdir()
        (self.root / "tests" / "notes.txt").write_text("v1\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "add settings")
        (self.root / "tests" / "notes.txt").write_text("v2\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "bump settings")
        h = gitio.resolve(self.root, "HEAD")
        rt = FakeRuntime("SHOULD NOT BE CALLED")
        r = run_update(self.root, UpdateRequest(source_commit=h, rev_range=f"{h}^..{h}",
                                                trigger="post_commit"), self.cfg, rt)
        self.assertEqual(r.status, "no_op")
        self.assertIsNone(rt.last_prompt)

    def test_semantic_linkage_none_is_distinct_from_gate_skip(self) -> None:
        (self.root / "src" / "worker.py").write_text("def helper(): return 1\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "add helper")
        head = gitio.resolve(self.root, "HEAD")
        rt = FakeRuntime(json.dumps({"action": "none", "doc": "", "area": "backend",
                                     "scope": "area"}))
        result = run_update(
            self.root,
            UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}",
                          trigger="post_commit"),
            self.cfg, rt,
        )
        self.assertEqual(result.reason, "model_linkage_none_semantic")
        self.assertIn("semantic review", rt.last_prompt)

    def test_priority_linkage_sees_sop_and_document_outlines(self) -> None:
        (self.root / "README.md").write_text(
            "# demo\n\n## What it does\n\nA tool.\n\n## How to change settings\n\nSettings.\n"
        )
        (self.root / "src" / "retention.py").write_text(
            "terminal_retention_days = 5\n\ndef delete_expired(entries):\n    return entries\n"
        )
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm",
             "make retention configurable")
        head = gitio.resolve(self.root, "HEAD")
        responses = [
            json.dumps({"action": "doc", "doc": "README.md", "area": "terminal lifecycle",
                        "scope": "cross_cutting"}),
            json.dumps({
                "disposition": "update", "target": "README.md",
                "summary": "documented configurable retention",
                "content": ("# demo\n\n## What it does\n\nA tool.\n\n"
                            "## How to change settings\n\n"
                            "Terminal retention is configurable and defaults to five days.\n"),
                "source_paths": ["src/retention.py"],
            }),
        ]
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return responses.pop(0)

        result = run_update(
            self.root,
            UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}",
                          trigger="post_commit"),
            self.cfg, FakeRuntime(answer),
        )
        self.assertEqual(result.status, "committed")
        self.assertIn("priority review", prompts[0])
        self.assertIn("data retention", prompts[0])
        self.assertIn("Always surface for documentation review", prompts[0])
        self.assertIn("How to change settings", prompts[0])
        self.assertIn("# API", prompts[0])
        self.assertIn('"writable":true', prompts[0])
        self.assertIn("repository-specific area: terminal lifecycle", prompts[1])
        self.assertIn("scope: cross_cutting", prompts[1])
        self.assertIn("five days", (self.root / "README.md").read_text())

    def test_full_document_linkage_batches_then_arbitrates_shortlist(self) -> None:
        records = [
            docs.TrackedDocument(f"docs/{name}.md", f"# {name}\n\n" + name * 1200,
                                 True, False)
            for name in "abcd"
        ]
        prompts = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            if "## Batch response" in prompt:
                candidates = ["docs/a.md"] if '"path":"docs/a.md"' in prompt else []
                return json.dumps({"area": "backend", "scope": "area",
                                   "candidates": candidates, "create": False})
            return json.dumps({"action": "doc", "doc": "docs/a.md", "area": "backend",
                               "scope": "area"})

        with mock.patch.object(engine, "MAX_PROMPT_BYTES", 4500):
            decision = engine._llm_linkage(
                "+ feature = true", records, baseline.policy(), FakeRuntime(answer),
                sop_body="SOP", changed_inputs=["src/a.py"],
            )
        self.assertEqual(decision.doc, "docs/a.md")
        self.assertGreaterEqual(len(prompts), 3)  # bounded batches, then final arbitration
        self.assertTrue(all("## Batch response" in prompt for prompt in prompts[:-1]))
        self.assertTrue(any("b" * 1200 in prompt for prompt in prompts))
        self.assertTrue(any("d" * 1200 in prompt for prompt in prompts))
        self.assertIn("Prior batch classifications", prompts[-1])
        self.assertIn("a" * 1200, prompts[-1])
        self.assertNotIn("c" * 1200, prompts[-1])

    def test_read_only_true_owner_surfaces_documentation_gap(self) -> None:
        (self.root / "CONTRIBUTING.md").write_text("# Contributor workflow\n\nRun workers.\n")
        (self.root / "src" / "worker.py").write_text("def run(): return 'new workflow'\n")
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "change worker flow")
        head = gitio.resolve(self.root, "HEAD")
        response = json.dumps({"action": "doc", "doc": "CONTRIBUTING.md",
                               "area": "developer workflow", "scope": "area"})
        result = run_update(
            self.root,
            UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}"),
            self.cfg, FakeRuntime(response),
        )
        self.assertEqual((result.status, result.reason), ("gap", "documentation_gap"))
        self.assertIn("CONTRIBUTING.md", result.summary)
        self.assertIn("read-only", result.summary)

    def test_detached_generates_message(self) -> None:
        r = self._run(UpdateRequest(targets=["docs/api.md"], detached=True,
                                    instruction="clarify", trigger="detached"), UPDATE_RESP)
        self.assertTrue(gitio.commit_subject(self.root, r.docs_commit).startswith("docs:"))

    # --- guards ---
    def test_source_commit_required(self) -> None:
        with self.assertRaises(SourceCommitRequired):
            self._run(UpdateRequest(targets=["docs/api.md"], use_working=True), UPDATE_RESP)

    def test_broken_link_rejected(self) -> None:
        bad = json.dumps({"disposition": "update", "target": "docs/api.md", "summary": "x",
                          "content": "# API\n\nSee [gone](./nope.md).\n", "source_paths": []})
        with self.assertRaises(VerificationFailed):
            self._run(UpdateRequest(targets=["docs/api.md"], detached=True, instruction="x"), bad)

    def test_secret_rejected(self) -> None:
        secret = "ghp_" + "a" * 36
        bad = json.dumps({"disposition": "update", "target": "docs/api.md", "summary": "x",
                          "content": f"# API\n\ntoken {secret}\n", "source_paths": []})
        with self.assertRaises(VerificationFailed):
            self._run(UpdateRequest(targets=["docs/api.md"], detached=True, instruction="x"), bad)

    def test_off_scope_target_rejected(self) -> None:
        bad = json.dumps({"disposition": "update", "target": "README.md", "summary": "x",
                          "content": "# demo\n", "source_paths": []})
        with self.assertRaises(RuntimeOutputInvalid):
            self._run(UpdateRequest(targets=["docs/api.md"], detached=True, instruction="x"), bad)

    def test_create_denied_is_gap(self) -> None:
        # With the SOP explicitly disabling creation, a would-create is a gap, not a write.
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_sop(repo_id, "---\nallow_create: false\n---\nno new docs\n")
        crt = json.dumps({"disposition": "create", "target": "docs/new.md",
                          "summary": "new doc", "content": "# New\n", "source_paths": []})
        r = self._run(UpdateRequest(targets=["docs/new.md"], detached=True, instruction="x"), crt)
        self.assertEqual(r.status, "gap")
        self.assertEqual(r.reason, "documentation_gap")
        self.assertFalse((self.root / "docs" / "new.md").exists())

    def test_only_target_committed(self) -> None:
        # build-plan §10: the docs commit must contain ONLY the doc, never sweep in unrelated
        # user changes sitting in the working tree.
        (self.root / "README.md").write_text("# demo\n\nunrelated local edit\n")
        (self.root / "src" / "api.py").write_text("def retry(n=5): return n  # unrelated\n")
        r = self._run(UpdateRequest(targets=["docs/api.md"], detached=True,
                                    instruction="x"), UPDATE_RESP)
        committed = gitio._run(self.root, "show", "--name-only", "--format=", r.docs_commit).split()
        self.assertEqual(committed, ["docs/api.md"])
        # the unrelated edits are still uncommitted
        dirty = gitio._run(self.root, "status", "--porcelain")
        self.assertIn("README.md", dirty)
        self.assertIn("src/api.py", dirty)

    def test_update_dropping_sections_rejected(self) -> None:
        # A wholesale rewrite that nukes several sections is refused; one rename is allowed (§5.5).
        multi = ("---\ncovers: src/api.py\n---\n# API\n\n## get\n\ng\n\n## put\n\np\n\n"
                 "## delete\n\nd\n")
        (self.root / "docs" / "api.md").write_text(multi)
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-qm", "expand api doc")
        drop = json.dumps({"disposition": "update", "target": "docs/api.md", "summary": "x",
                           "content": "---\ncovers: src/api.py\n---\n# API\n\nonly intro now.\n",
                           "source_paths": []})
        with self.assertRaises(VerificationFailed):
            self._run(UpdateRequest(targets=["docs/api.md"], detached=True, instruction="x"), drop)

    def test_update_preserving_sections_ok(self) -> None:
        keep = json.dumps({"disposition": "update", "target": "docs/api.md",
                           "summary": "add a note", "content":
                           "---\ncovers: src/api.py\n---\n# API\n\nRetries once.\n\n## Notes\n\nnew.\n",
                           "source_paths": []})
        r = self._run(UpdateRequest(targets=["docs/api.md"], detached=True, instruction="x"), keep)
        self.assertEqual(r.status, "committed")
        self.assertIn("## Notes", (self.root / "docs" / "api.md").read_text())

    def test_concurrent_edit_conflict(self) -> None:
        # Model returns content, but the file changes after hashing (simulated) -> conflict.
        class MutatingRuntime(FakeRuntime):
            def complete(self, prompt: str, system: str = "", timeout: int = 180,
                         schema=None) -> str:
                (Path(self.root) / "docs" / "api.md").write_text("mutated after read\n")  # type: ignore[attr-defined]
                return UPDATE_RESP
        rt = MutatingRuntime(UPDATE_RESP)
        rt.root = str(self.root)  # type: ignore[attr-defined]
        with self.assertRaises(Conflict):
            run_update(self.root, UpdateRequest(targets=["docs/api.md"], detached=True,
                                                instruction="x"), self.cfg, rt)

    # --- history / status ---
    def test_status_render(self) -> None:
        self._run(UpdateRequest(source_commit=self.head, rev_range=f"{self.head}^..{self.head}",
                                trigger="post_commit"), UPDATE_RESP)
        out = status.render(self.root)
        self.assertIn("checkpoint", out)
        self.assertIn("nothing running now!", out)

    # --- browsing views ---
    def test_views_docs_and_changelog(self) -> None:
        r = self._run(UpdateRequest(source_commit=self.head, rev_range=f"{self.head}^..{self.head}",
                                    trigger="post_commit"), UPDATE_RESP)
        docs_out = views.render_docs(self.root)
        self.assertIn("docs/api.md", docs_out)
        self.assertIn("covers: src/api.py", docs_out)
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        cl = views.render_changelog(history.recent(repo_id, 30), "(this repo)")
        self.assertIn("documented the configurable retry backoff", cl)
        rec = views.render_record(history.recent(repo_id, 1)[0])
        self.assertIn("--- patch ---", rec)
        self.assertEqual(views.render_record(None), "no such changelog entry.")

    # --- repo registry ---
    def test_repo_registry(self) -> None:
        history.register_repo("id1", "/p1", initialized=True)
        history.register_repo("id2", "/p2")
        rows = {r["repo_id"]: r for r in history.list_repos()}
        self.assertEqual(rows["id1"]["initialized"], 1)
        self.assertEqual(rows["id2"]["initialized"], 0)
        # an update auto-registers its repo
        self._run(UpdateRequest(source_commit=self.head, rev_range=f"{self.head}^..{self.head}",
                                trigger="post_commit"), UPDATE_RESP)
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        self.assertIn(repo_id, {r["repo_id"] for r in history.list_repos()})

    # --- SOP ---
    def test_sop_default_and_save(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        content, is_default = repos.read_sop(repo_id, str(self.root))
        self.assertTrue(is_default)
        self.assertFalse(repos.sop_allows_create(repo_id, str(self.root)))
        repos.save_sop(repo_id, "---\nallow_create: false\n---\nNo new docs here.\n")
        _, is_default2 = repos.read_sop(repo_id, str(self.root))
        self.assertFalse(is_default2)
        self.assertFalse(repos.sop_allows_create(repo_id, str(self.root)))
        self.assertIn("No new docs", repos.sop_prompt_body(repo_id, str(self.root)))
        repos.reset_sop(repo_id)
        self.assertFalse(repos.sop_allows_create(repo_id, str(self.root)))

    def test_sop_enables_create(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        repos.save_sop(
            repo_id,
            "---\nallow_create: true\ncreate_roots: [docs/]\n---\nCreate docs for new features.\n",
        )
        crt = json.dumps({"disposition": "create", "target": "docs/new.md",
                          "summary": "new feature doc", "content": "# New feature\n\nDetails.\n",
                          "source_paths": []})
        r = self._run(UpdateRequest(targets=["docs/new.md"], detached=True, instruction="x"), crt)
        self.assertEqual(r.status, "committed")
        self.assertTrue((self.root / "docs" / "new.md").exists())

    def test_knowledge_generation(self) -> None:
        repo_id = config.checkout_id(gitio.common_dir(self.root))
        kb = repos.generate_knowledge(repo_id, str(self.root))
        self.assertIn("choobi knowledge", kb)
        self.assertIn("docs/api.md", kb)
        self.assertIn("covers: src/api.py", kb)

    # --- evaluation harness ---
    def test_eval_scoring_perfect(self) -> None:
        # With a scripted runtime returning each fixture's labeled disposition, the scoring
        # must report a perfect run over all fixtures (exercises steps 1-3 end to end).
        report = evaluate.run_eval(lambda fx: FakeRuntime(fx.fake_response))
        self.assertEqual(report["precision"], 1.0)
        self.assertEqual(report["recall"], 1.0)
        self.assertEqual(report["silence"], 1.0)
        self.assertEqual(report["required_fact_recall"], 1.0)
        self.assertEqual(report["forbidden_claim_rate"], 0.0)
        self.assertEqual(report["preservation_rate"], 1.0)

    def test_eval_semantic_linkage(self) -> None:
        # Step 2: a modified file no doc covers is linked to the right doc by the model pass.
        fx = [f for f in evaluate.FIXTURES if f.name == "semantic_link_update"]
        report = evaluate.run_eval(lambda f: FakeRuntime(f.fake_response), fx)
        self.assertEqual(report["rows"][0]["predicted"], "update:docs/features/login.md")

    def test_covers_self_reinforces(self) -> None:
        # Step 3: after choobi writes a doc for a change, that doc's covers names the code,
        # so the next time deterministic linkage finds it without a model pass.
        merged = docs.merge_covers(
            "# Login\n\nbody\n", "# Login\n\nbody\n", ["src/auth.py"], ["src/auth.py"]
        )
        self.assertIn("covers", merged)
        self.assertIn("src/auth.py", merged)

    def test_recall_surface(self) -> None:
        pol = baseline.policy()
        self.assertEqual(docs.documentable_surface(self.root, {"src/new.py"}, pol), ["src/new.py"])
        self.assertEqual(docs.documentable_surface(self.root, {"src/api.py"}, pol), [])  # covered
        self.assertEqual(docs.documentable_surface(self.root, {"tests/test_x.py"}, pol), [])  # test
        rid = config.checkout_id(gitio.common_dir(self.root))
        repos.save_snapshot(rid, ["src/api.py"], self.head)
        self.assertEqual(repos.load_snapshot(rid), {"src/api.py"})

    def test_failed_record_and_status(self) -> None:
        try:
            engine.run_update_guarded(self.root, UpdateRequest(targets=["docs/api.md"],
                                      use_working=True), self.cfg, FakeRuntime(UPDATE_RESP))
        except SourceCommitRequired:
            pass
        out = status.render(self.root)
        self.assertIn("choobi is sorry", out)
        self.assertIn("source_commit_required", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
