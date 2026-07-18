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

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List

from . import config, engine, gitio
from .errors import ChoobiError
from .runtime import FakeRuntime, Runtime, get_runtime


@dataclass
class Fixture:
    name: str
    build: Callable[[Path], None]   # populate + commit; leaves HEAD as the source commit
    expect: str                     # "silent" | "update:<path>" | "create"
    fake_response: str = ""         # what a scripted runtime returns (deterministic mode)


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


def _upd(path: str, content: str) -> str:
    import json
    return json.dumps({"disposition": "update", "target": path, "summary": "eval", "content": content})


def _silent() -> str:
    import json
    return json.dumps({"disposition": "silent"})


def _create(path: str, content: str) -> str:
    import json
    return json.dumps({"disposition": "create", "target": path, "summary": "eval", "content": content})


def _link(path: str) -> str:
    import json
    return json.dumps({"doc": path})


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
_RATELIMIT_CODE = ('def allow(key, limit=100, window=60):\n'
                   '    """Return True if key is under limit requests per window seconds."""\n'
                   '    ...\n')
_RATELIMIT_DOC = ("# Rate limiter\n\n`allow(key, limit, window)` returns True while `key` is under "
                  "`limit` requests per `window` seconds.\n")


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


FIXTURES: List[Fixture] = [
    Fixture("update_documented_api", _f_update, "update:docs/reference/cache.md",
            _upd("docs/reference/cache.md", _CACHE_DOC_UPDATED)),
    Fixture("silent_refactor", _f_silent_refactor, "silent", _silent()),
    Fixture("silent_test_only", _f_silent_test, "silent", _silent()),
    Fixture("create_new_feature", _f_create, "create",
            _create("docs/internal/features/ratelimit.md", _RATELIMIT_DOC)),
    # two scripted calls: the linkage pass (pick the doc), then the disposition (update it)
    Fixture("semantic_link_update", _f_semantic, "update:docs/features/login.md",
            [_link("docs/features/login.md"), _upd("docs/features/login.md", _LOGIN_DOC_UPDATED)]),
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
        tmp = tempfile.mkdtemp(prefix="choobi-eval-")
        root = Path(tmp)
        fx.build(root)
        head = gitio.resolve(root, "HEAD")
        try:
            result = engine.run_update(
                root,
                engine.UpdateRequest(source_commit=head, rev_range=f"{head}^..{head}",
                                     trigger="post_commit"),
                cfg, runtime_for(fx),
            )
            predicted = _predict(root, head, result)
        except ChoobiError as exc:
            predicted = f"error:{exc.reason}"
        rows.append({"name": fx.name, "expect": fx.expect, "predicted": predicted,
                     "correct": predicted == fx.expect})

    positives = [r for r in rows if r["expect"] != "silent"]
    negatives = [r for r in rows if r["expect"] == "silent"]
    wrote = [r for r in rows if r["predicted"] not in ("silent", "gap")]

    def frac(num, den):
        return round(num / den, 3) if den else 1.0

    correct_writes = [r for r in wrote if r["correct"]]
    return {
        "precision": frac(len(correct_writes), len(wrote)),
        "recall": frac(len([r for r in positives if r["correct"]]), len(positives)),
        "silence": frac(len([r for r in negatives if r["correct"]]), len(negatives)),
        "rows": rows,
    }


def render(report: Dict) -> str:
    lines = [f"precision {report['precision']}   recall {report['recall']}   "
             f"silence {report['silence']}", ""]
    for r in report["rows"]:
        mark = "ok " if r["correct"] else "MISS"
        lines.append(f"  [{mark}] {r['name']}: expected {r['expect']}, got {r['predicted']}")
    return "\n".join(lines)


def main() -> int:
    """Live evaluation against the configured runtime. Uses an isolated temp CHOOBI_HOME."""
    import os
    os.environ["CHOOBI_HOME"] = tempfile.mkdtemp(prefix="choobi-eval-home-")
    cfg = config.Config.load()
    cfg.onboarded = True
    report = run_eval(lambda _fx: get_runtime(cfg), cfg=cfg)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
