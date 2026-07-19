"""Fixture-based evaluation of choobi's disposition quality (build-plan Phase 1 exit).

Each fixture is a small synthetic repo plus a change, labeled with the disposition choobi
*should* reach: stay silent, update a specific doc, or create a new one. We run the engine
over each and score three numbers:

    precision = of the times choobi wrote, how often it wrote the right thing
    recall    = of the changes that needed a doc, how many choobi caught
    silence   = of the changes that needed nothing, how often choobi stayed silent

Run it two ways:
    - deterministic (a scripted runtime returns each fixture's labeled disposition): the
      SCORING is what's under test, so it must report a perfect run.
    - live (`python -m choobi.evaluate`): the real runtime, to measure the model's judgment.
"""
from __future__ import annotations

import difflib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from . import config, engine, gitio, repos
from .errors import ChoobiError
from .runtime import FakeRuntime, Runtime, get_runtime


@dataclass
class Fixture:
    name: str
    build: Callable[[Path], None]   # populate + commit; leaves HEAD as the source commit
    expect: str                     # "silent" | "update:<path>" | "create"
    fake_response: str = ""         # what a scripted runtime returns (deterministic mode)
    allow_create: bool = False
    required: Tuple[str, ...] = ()
    forbidden: Tuple[str, ...] = ()
    preserved: Tuple[str, ...] = ()
    max_changed_lines: int = 0


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _init(root: Path, files: Dict[str, str]) -> None:
    for path, content in files.items():
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.co")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _edit(root: Path, path: str, content: str, msg: str) -> None:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _upd(path: str, content: str, *source_paths: str) -> str:
    import json
    return json.dumps({"disposition": "update", "target": path, "summary": "eval",
                       "content": content, "source_paths": list(source_paths)})


def _silent() -> str:
    import json
    return json.dumps({"disposition": "silent", "target": "", "summary": "", "content": "",
                       "source_paths": []})


def _create(path: str, content: str, *source_paths: str) -> str:
    import json
    return json.dumps({"disposition": "create", "target": path, "summary": "eval",
                       "content": content, "source_paths": list(source_paths)})


def _link(path: str) -> str:
    import json
    return json.dumps({"action": "doc", "doc": path, "area": "feature",
                       "scope": "area"})


def _link_create(area: str = "feature", scope: str = "area") -> str:
    import json
    return json.dumps({"action": "create", "doc": "", "area": area, "scope": scope})


def _link_none(area: str = "internal", scope: str = "area") -> str:
    import json
    return json.dumps({"action": "none", "doc": "", "area": area, "scope": scope})


# --- realistic, unambiguously doc-worthy scenarios ---

_CACHE_DOC = ("---\ncovers: src/cache.py\n---\n# Cache reference\n\n"
              "## get(key)\nReturns the cached value for `key`, or `None`.\n\n"
              "## put(key, value)\nStores `value` under `key`. Entries never expire.\n")
_CACHE_CODE = "_store = {}\n\ndef get(key):\n    return _store.get(key)\n\ndef put(key, value):\n    _store[key] = value\n"
_CACHE_DOC_UPDATED = ("---\ncovers: src/cache.py\n---\n# Cache reference\n\n"
                      "## get(key)\nReturns the cached value for `key`, or `None`.\n\n"
                      "## put(key, value)\nStores `value` under `key`, expiring after "
                      "`ttl` seconds (default 300).\n")
_LOGIN_DOC = "# Login\n\nUsers sign in with email and password. Sessions last 24 hours.\n"
_LOGIN_DOC_UPDATED = "# Login\n\nUsers sign in with email and password. Sessions last 7 days.\n"
_AUTH_CODE = "SESSION_SECONDS = 24 * 3600  # 24 hours\n\ndef login(email, password):\n    return make_session(SESSION_SECONDS)\n"
_RATELIMIT_CODE = (
    '"""Public rate-limiting API."""\n\n__all__ = ["allow"]\n\n'
    "from collections import defaultdict, deque\nfrom time import monotonic\n\n"
    "_events = defaultdict(deque)\n\n"
    "def allow(key, limit=100, window=60):\n"
    "    now = monotonic()\n    events = _events[key]\n"
    "    while events and events[0] <= now - window:\n        events.popleft()\n"
    "    if len(events) >= limit:\n        return False\n"
    "    events.append(now)\n    return True\n"
)
_RATELIMIT_DOC = ("# Rate limiter\n\n`allow(key, limit, window)` returns True while `key` is under "
                  "`limit` requests per `window` seconds.\n")
_RETENTION_README = ("# Terminal helper\n\n## What it does\n\nTracks coding sessions.\n\n"
                     "## Settings\n\nSettings are stored in `config.json`.\n")
_RETENTION_README_UPDATED = (
    "# Terminal helper\n\n## What it does\n\nTracks coding sessions.\n\n"
    "## Settings\n\nSet `terminal_retention_days` in `config.json`; it defaults to 5 days. "
    "Cleanup removes session-list entries, not the underlying transcript files.\n"
)


def _f_update(root: Path) -> None:
    _init(root, {"README.md": "# demo\n", "docs/reference/cache.md": _CACHE_DOC,
                 "src/cache.py": _CACHE_CODE})
    _edit(root, "src/cache.py",
          "import time\n\n_store = {}\n\ndef get(key):\n    return _store.get(key)\n\n"
          "def put(key, value, ttl=300):\n    _store[key] = (value, time.time() + ttl)\n",
          "add TTL expiry to cache entries")


def _f_silent_refactor(root: Path) -> None:
    _init(root, {"README.md": "# demo\n", "docs/reference/cache.md": _CACHE_DOC,
                 "src/cache.py": _CACHE_CODE})
    _edit(root, "src/cache.py",
          "_store = {}\n\ndef get(key):\n    value = _store.get(key)\n    return value\n\n"
          "def put(key, value):\n    _store[key] = value\n", "rename local in cache.get")


def _f_silent_test(root: Path) -> None:
    _init(root, {"README.md": "# demo\n", "docs/reference/cache.md": _CACHE_DOC,
                 "src/cache.py": _CACHE_CODE})
    _edit(root, "tests/test_cache.py", "def test_get(): assert True\n", "add cache test")


def _f_create(root: Path) -> None:
    _init(root, {"README.md": "# demo\n", "docs/reference/cache.md": _CACHE_DOC,
                 "src/cache.py": _CACHE_CODE})
    _edit(root, "src/ratelimit.py", _RATELIMIT_CODE, "add a rate limiter")


def _f_semantic(root: Path) -> None:
    # login.md has no covers and never names auth.py, so deterministic linkage misses it;
    # only the step-2 model pass can connect the session-lifetime change to the login doc.
    _init(root, {"README.md": "# demo\n", "docs/features/login.md": _LOGIN_DOC,
                 "src/auth.py": _AUTH_CODE})
    _edit(root, "src/auth.py",
          "SESSION_SECONDS = 7 * 24 * 3600  # 7 days\n\ndef login(email, password):\n"
          "    return make_session(SESSION_SECONDS)\n", "extend session lifetime to 7 days")


def _f_cli_flag(root: Path) -> None:
    _init(root, {
        "docs/reference/cli.md": ("---\ncovers: src/cli.py\n---\n# CLI\n\n"
                                  "```bash\ntool --json\n```\n"),
        "src/cli.py": 'parser.add_argument("--json", action="store_true")\n',
    })
    _edit(root, "src/cli.py",
          'parser.add_argument("--format", choices=("text", "json"), default="text")\n',
          "replace json flag with output format")


def _f_required_env(root: Path) -> None:
    _init(root, {
        "README.md": "---\ncovers: src/config.py\n---\n# Service\n\nNo setup is required.\n",
        "src/config.py": "TOKEN = None\n",
    })
    _edit(root, "src/config.py", 'import os\nTOKEN = os.environ["SERVICE_TOKEN"]\n',
          "require service token")


def _f_configurable_retention(root: Path) -> None:
    _init(root, {
        "README.md": _RETENTION_README,
        "src/config.rs": "pub const RETENTION_DAYS: u64 = 5;\n",
        "src/manager.js": "export const retentionDays = 5;\n",
        "src/session_store.rs": "pub fn load_entries() { /* load metadata only */ }\n",
    })
    (root / "src/config.rs").write_text(
        "pub struct Config { pub terminal_retention_days: u64 }\n"
        "pub const DEFAULT_TERMINAL_RETENTION_DAYS: u64 = 5;\n"
    )
    (root / "src/manager.js").write_text(
        "export const saveRetention = (days) => invoke('set_terminal_retention_days', { days });\n"
    )
    (root / "src/session_store.rs").write_text(
        "// Removes only session-list metadata; underlying transcript files are never deleted.\n"
        "pub fn prune_unused_metadata(retention_days: u64) { /* retain recent entries */ }\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "make terminal retention configurable")


def _f_error_contract(root: Path) -> None:
    _init(root, {
        "docs/reference/client.md": "---\ncovers: src/client.py\n---\n# Client\n\n`send()` returns the response.\n",
        "src/client.py": "def send(response):\n    return response\n",
    })
    _edit(root, "src/client.py",
          "def send(response):\n    if response.status == 429:\n"
          "        raise RateLimitError(retry_after=30)\n    return response\n",
          "surface rate limit errors")


def _f_frontend_workflow(root: Path) -> None:
    _init(root, {
        "docs/features/settings.md": "---\ncovers: web/settings.js\n---\n# Settings\n\nSave your profile.\n",
        "web/settings.js": "export const save = (profile) => api.save(profile);\n",
    })
    _edit(root, "web/settings.js",
          "export const save = (profile) => {\n"
          "  if (!profile.emailVerified) throw new Error('verify email first');\n"
          "  return api.save(profile);\n};\n", "require verified email for settings")


def _f_feature_gated(root: Path) -> None:
    _init(root, {"README.md": "# demo\n"})
    _edit(root, "src/beta.py",
          "FEATURE_BETA = False\n\ndef beta_export():\n    if not FEATURE_BETA:\n"
          "        raise RuntimeError('disabled')\n", "add disabled beta implementation")


def _f_restore_documented_behavior(root: Path) -> None:
    _init(root, {
        "docs/reference/parser.md": "---\ncovers: src/parser.py\n---\n# Parser\n\n`parse()` raises `ValueError` for empty input.\n",
        "src/parser.py": "def parse(text):\n    if not text:\n        return None\n    return text\n",
    })
    _edit(root, "src/parser.py",
          "def parse(text):\n    if not text:\n        raise ValueError('empty input')\n    return text\n",
          "restore empty input error")


def _f_docs_already_updated(root: Path) -> None:
    _init(root, {
        "docs/api.md": "---\ncovers: src/api.py\n---\n# API\n\nRetries once.\n",
        "src/api.py": "def retry(n=1): return n\n",
    })
    (root / "docs/api.md").write_text(
        "---\ncovers: src/api.py\n---\n# API\n\nRetries three times by default.\n"
    )
    (root / "src/api.py").write_text("def retry(n=3): return n\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "increase retries and update docs")


def _f_generated_document(root: Path) -> None:
    _init(root, {
        "docs/reference/generated-client.md": (
            "---\ncovers: tools/generate_client.py\n---\n"
            "<!-- GENERATED DOCUMENT. DO NOT EDIT. -->\n# Generated client\n\nVersion 1.\n"
        ),
        "tools/generate_client.py": "SCHEMA_VERSION = 1\n",
    })
    _edit(root, "tools/generate_client.py", "SCHEMA_VERSION = 2\n", "update client schema")


def _f_remove_public_api(root: Path) -> None:
    _init(root, {
        "docs/reference/exports.md": (
            "---\ncovers: src/exports.py\n---\n# Exports\n\n"
            "## keep()\nReturns the stable value.\n\n"
            "## legacy()\nReturns the legacy value.\n"
        ),
        "src/exports.py": (
            "def keep():\n    return 'stable'\n\n"
            "def legacy():\n    return 'legacy'\n"
        ),
    })
    _edit(root, "src/exports.py", "def keep():\n    return 'stable'\n", "remove legacy API")


FIXTURES: List[Fixture] = [
    Fixture("update_documented_api", _f_update, "update:docs/reference/cache.md",
            [_link("docs/reference/cache.md"),
             _upd("docs/reference/cache.md", _CACHE_DOC_UPDATED, "src/cache.py")],
            required=("ttl", "300"),
            preserved=("## get(key)\nReturns the cached value for `key`, or `None`.",),
            max_changed_lines=4),
    Fixture("silent_refactor", _f_silent_refactor, "silent",
            [_link("docs/reference/cache.md"), _silent()]),
    Fixture("silent_test_only", _f_silent_test, "silent", _silent()),
    Fixture("create_new_feature", _f_create, "create",
            [_link_create(),
             _create("docs/internal/features/ratelimit.md", _RATELIMIT_DOC, "src/ratelimit.py")],
            allow_create=True, required=("allow(", "limit", "window"),
            forbidden=("TooManyRequests", "user_id", "client IP", "`str`",
                       "handle_request", "reject_request", "from ratelimit import")),
    # two scripted calls: the linkage pass (pick the doc), then the disposition (update it)
    Fixture("semantic_link_update", _f_semantic, "update:docs/features/login.md",
            [_link("docs/features/login.md"),
             _upd("docs/features/login.md", _LOGIN_DOC_UPDATED, "src/auth.py")],
            required=("7 days",)),
    Fixture("update_cli_flag", _f_cli_flag, "update:docs/reference/cli.md",
            [_link("docs/reference/cli.md"),
             _upd("docs/reference/cli.md",
                  "---\ncovers: src/cli.py\n---\n# CLI\n\n```bash\ntool --format json\n```\n"
                  "The default format is `text`.\n", "src/cli.py")],
            required=("tool --format json", "text"), forbidden=("tool --json",)),
    Fixture("update_required_env", _f_required_env, "update:README.md",
            [_link("README.md"),
             _upd("README.md",
                  "---\ncovers: src/config.py\n---\n# Service\n\nSet the required `SERVICE_TOKEN` "
                  "environment variable before starting the service.\n", "src/config.py")],
            required=("SERVICE_TOKEN", "environment variable")),
    Fixture("update_configurable_retention", _f_configurable_retention, "update:README.md",
            [_link("README.md"),
             _upd("README.md", _RETENTION_README_UPDATED,
                  "src/config.rs", "src/manager.js", "src/session_store.rs")],
            required=("terminal_retention_days", "5", "transcript"),
            preserved=("## What it does\n\nTracks coding sessions.",),
            max_changed_lines=20),
    Fixture("update_error_contract", _f_error_contract, "update:docs/reference/client.md",
            [_link("docs/reference/client.md"),
             _upd("docs/reference/client.md",
                  "---\ncovers: src/client.py\n---\n# Client\n\n`send()` raises `RateLimitError` "
                  "with `retry_after=30` for a 429 response.\n", "src/client.py")],
            required=("RateLimitError", "retry_after", "30")),
    Fixture("update_frontend_workflow", _f_frontend_workflow,
            "update:docs/features/settings.md",
            [_link("docs/features/settings.md"),
             _upd("docs/features/settings.md",
                  "---\ncovers: web/settings.js\n---\n# Settings\n\nVerify your email before "
                  "saving your profile.\n", "web/settings.js")],
            required=("Verify", "email", "saving")),
    Fixture("silent_feature_gated", _f_feature_gated, "silent", _link_none(),
            allow_create=True),
    Fixture("silent_restored_behavior", _f_restore_documented_behavior, "silent",
            [_link("docs/reference/parser.md"), _silent()]),
    Fixture("silent_docs_already_updated", _f_docs_already_updated, "silent",
            [_link("docs/api.md"), _silent()]),
    Fixture("silent_generated_document", _f_generated_document, "silent", _link_none(),
            allow_create=True),
    Fixture("remove_public_api", _f_remove_public_api, "update:docs/reference/exports.md",
            [_link("docs/reference/exports.md"),
             _upd("docs/reference/exports.md",
                  "---\ncovers: src/exports.py\n---\n# Exports\n\n"
                  "## keep()\nReturns the stable value.\n", "src/exports.py")],
            required=("keep()", "stable value"), forbidden=("legacy()", "legacy value"),
            max_changed_lines=4),
]


def _files_at(root: Path, rev: str) -> set:
    out = gitio._run(root, "ls-tree", "-r", "--name-only", rev)
    return {line for line in out.splitlines() if line.strip()}


def _predict(root: Path, head: str, result: engine.UpdateResult) -> str:
    if result.status == "no_op":
        return "silent"
    if result.status == "gap":
        return "gap"
    path = result.docs_changed[0]
    return f"update:{path}" if path in _files_at(root, head) else "create"


def run_eval(runtime_for: Callable[[Fixture], Runtime],
             fixtures: List[Fixture] = None,
             cfg: config.Config = None) -> Dict:
    """Run the engine over each fixture and score precision, recall, and silence."""
    fixtures = fixtures if fixtures is not None else FIXTURES
    cfg = cfg or config.Config(name="eval", onboarded=True)
    rows = []
    for fx in fixtures:
        with tempfile.TemporaryDirectory(prefix="choobi-eval-") as tmp:
            root = Path(tmp)
            fx.build(root)
            head = gitio.resolve(root, "HEAD")
            if fx.allow_create:
                repo_id = config.checkout_id(gitio.common_dir(root))
                repos.save_sop(
                    repo_id,
                    "---\nallow_create: true\ncreate_roots: [docs/internal/features/]\n---\n"
                    "Use docs/internal/features/.\n",
                )
            try:
                result = engine.run_update(
                    root,
                    engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}",
                                         trigger="post_commit"),
                    cfg, runtime_for(fx),
                )
                predicted = _predict(root, head, result)
                path = result.docs_changed[0] if result.docs_changed else ""
                content = (root / path).read_text() if path else ""
            except ChoobiError as exc:
                predicted = f"error:{exc.reason}"
                path = ""
                content = ""
            lower = content.lower()
            missing = [fact for fact in fx.required if fact.lower() not in lower]
            unsupported = [fact for fact in fx.forbidden if fact.lower() in lower]
            missing_preserved = [text for text in fx.preserved if text not in content]
            changed_lines = 0
            if path and path in _files_at(root, head):
                old = gitio._run(root, "show", f"{head}:{path}")
                changed_lines = sum(
                    1 for line in difflib.unified_diff(old.splitlines(), content.splitlines())
                    if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
                )
            too_large = bool(fx.max_changed_lines and changed_lines > fx.max_changed_lines)
            rows.append({"name": fx.name, "expect": fx.expect, "predicted": predicted,
                         "decision_correct": predicted == fx.expect,
                         "missing_required": missing, "forbidden_present": unsupported,
                         "missing_preserved": missing_preserved,
                         "changed_lines": changed_lines, "too_many_changed_lines": too_large,
                         "correct": predicted == fx.expect and not missing and not unsupported
                         and not missing_preserved and not too_large})

    positives = [r for r in rows if r["expect"] != "silent"]
    negatives = [r for r in rows if r["expect"] == "silent"]
    wrote = [r for r in rows if r["predicted"] not in ("silent", "gap")
             and not r["predicted"].startswith("error:")]

    def frac(num, den):
        return round(num / den, 3) if den else 1.0

    correct_writes = [r for r in wrote if r["correct"]]
    required_total = sum(len(fx.required) for fx in fixtures)
    required_found = required_total - sum(len(r["missing_required"]) for r in rows)
    forbidden_total = sum(len(fx.forbidden) for fx in fixtures)
    forbidden_found = sum(len(r["forbidden_present"]) for r in rows)
    preserved_total = sum(len(fx.preserved) for fx in fixtures)
    preserved_found = preserved_total - sum(len(r["missing_preserved"]) for r in rows)
    return {
        "decision_accuracy": frac(len([r for r in rows if r["decision_correct"]]), len(rows)),
        "precision": frac(len(correct_writes), len(wrote)),
        "recall": frac(len([r for r in positives if r["correct"]]), len(positives)),
        "silence": frac(len([r for r in negatives if r["correct"]]), len(negatives)),
        "required_fact_recall": frac(required_found, required_total),
        "forbidden_claim_rate": frac(forbidden_found, forbidden_total),
        "preservation_rate": frac(preserved_found, preserved_total),
        "rows": rows,
    }


def render(report: Dict) -> str:
    lines = [f"decision {report['decision_accuracy']}   precision {report['precision']}   "
             f"recall {report['recall']}   silence {report['silence']}",
             f"required facts {report['required_fact_recall']}   forbidden probes "
             f"{report['forbidden_claim_rate']}   preservation {report['preservation_rate']}", ""]
    for r in report["rows"]:
        mark = "ok " if r["correct"] else "MISS"
        lines.append(f"  [{mark}] {r['name']}: expected {r['expect']}, got {r['predicted']}")
        if r["missing_required"]:
            lines.append(f"         missing: {', '.join(r['missing_required'])}")
        if r["forbidden_present"]:
            lines.append(f"         forbidden: {', '.join(r['forbidden_present'])}")
        if r["missing_preserved"]:
            lines.append(f"         lost preserved text: {len(r['missing_preserved'])}")
        if r["too_many_changed_lines"]:
            lines.append(f"         changed lines: {r['changed_lines']} (over fixture ceiling)")
    return "\n".join(lines)


def main() -> int:
    """Live evaluation against the configured runtime. Uses an isolated temp CHOOBI_HOME."""
    import os
    with tempfile.TemporaryDirectory(prefix="choobi-eval-home-") as home:
        os.environ["CHOOBI_HOME"] = home
        cfg = config.Config.load()
        cfg.onboarded = True
        print(render(run_eval(lambda _fx: get_runtime(cfg), cfg=cfg)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
