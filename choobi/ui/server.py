"""Local UI server: loopback-only, per-launch access token (build-plan §8).

Serves the static dooni-style configuration and inspection front end. Its JSON API exposes the
same history, help, style, SOP, and generated knowledge data as the CLI modules.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .. import auth, config, gitio, help as help_mod, history, repos as repos_mod
from ..errors import ChoobiError, RuntimeUnavailable

_STATIC = Path(__file__).resolve().parent / "static"
_TOKEN = secrets.token_urlsafe(24)

_CTYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}


def _repo_root() -> Optional[Path]:
    try:
        return gitio.repo_root(Path.cwd())
    except RuntimeError:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:  # silence access log
        pass

    # --- helpers ---
    def _authed(self, query: Dict[str, list]) -> bool:
        supplied = self.headers.get("X-Choobi-Token") or (query.get("token", [""])[0])
        return hmac.compare_digest(supplied, _TOKEN)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    # --- routing ---
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/" or path == "":
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path.startswith("/api/"):
            if not self._authed(query):
                return self._json({"error": "unauthorized"}, 403)
            return self._get_api(path, query)
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not parsed.path.startswith("/api/") or not self._authed(query):
            return self._json({"error": "unauthorized"}, 403)
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        try:
            return self._post_api(parsed.path, payload)
        except ChoobiError as exc:
            return self._json({"error": exc.message, "reason": exc.reason}, 400)
        except RuntimeError as exc:
            return self._json({"error": str(exc), "reason": "error"}, 400)

    def _serve_static(self, rel: str) -> None:
        target = (_STATIC / rel).resolve()
        if _STATIC not in target.parents and target != _STATIC or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = _CTYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _repo_arg(self, query: Dict[str, list]) -> "tuple[str, str]":
        repo_id = query.get("repo", [""])[0]
        rec = history.get_repo(repo_id)
        return repo_id, (rec["path"] if rec else "")

    def _get_api(self, path: str, query: Dict[str, list]) -> None:
        cfg = config.Config.load()
        root = _repo_root()
        if path == "/api/config":
            runtime_state = auth.is_logged_in(cfg.agent)
            return self._json({"name": cfg.name, "onboarded": cfg.onboarded,
                               "mode": cfg.mode, "agent": cfg.agent,
                               "runtime_state": ({True: "ready", False: "not logged in",
                                                  None: "not installed"}[runtime_state]),
                               "has_repo": root is not None,
                               "repo": str(root) if root else ""})
        if path == "/api/commands":
            return self._json(help_mod.COMMANDS)
        if path == "/api/style":
            personal = config.personal_style_path()
            is_personal = personal.exists() and bool(personal.read_text().strip())
            content = personal.read_text() if is_personal else ""
            return self._json({"content": content, "is_personal": is_personal})
        if path == "/api/repos":
            return self._json({"repos": history.list_repos()})
        if path == "/api/repo/sop":
            repo_id, repo_path = self._repo_arg(query)
            content, is_default = repos_mod.read_sop(repo_id, repo_path)
            return self._json({"content": content, "is_default": is_default})
        if path == "/api/repo/knowledge":
            repo_id, repo_path = self._repo_arg(query)
            return self._json({"content": repos_mod.read_knowledge(repo_id, repo_path)})
        if path == "/api/repo/changelog":
            repo_id, _ = self._repo_arg(query)
            return self._json({"records": history.recent(repo_id, limit=200)})
        if path == "/api/record":
            rid = int(query.get("id", ["0"])[0])
            return self._json({"record": history.get(rid)})
        self._json({"error": "not found"}, 404)

    def _post_api(self, path: str, payload: Dict[str, Any]) -> None:
        cfg = config.Config.load()
        if path == "/api/onboard":
            if payload.get("agent", "claude") != "claude":
                raise RuntimeUnavailable("Choobi V1 requires the tool-free Claude runtime")
            cfg.name = payload.get("name", "").strip()
            cfg.agent = "claude"
            cfg.onboarded = True
            cfg.save()
            return self._json({"ok": True})
        if path == "/api/style/save":
            p = config.personal_style_path()
            content = payload.get("content", "")
            is_personal = bool(content.strip())
            if is_personal:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
            else:
                p.unlink(missing_ok=True)
            return self._json({"ok": True, "is_personal": is_personal})
        if path == "/api/style/reset":
            p = config.personal_style_path()
            if p.exists():
                p.unlink()
            return self._json({"ok": True, "is_personal": False, "content": ""})
        if path == "/api/repo/sop/save":
            repos_mod.save_sop(payload["repo"], payload.get("content", ""))
            return self._json({"ok": True, "is_default": False})
        if path == "/api/repo/sop/reset":
            rec = history.get_repo(payload["repo"])
            repos_mod.reset_sop(payload["repo"])
            content, is_default = repos_mod.read_sop(payload["repo"], rec["path"] if rec else "")
            return self._json({"ok": True, "content": content, "is_default": is_default})
        if path == "/api/repo/knowledge/refresh":
            rec = history.get_repo(payload["repo"])
            content = repos_mod.generate_knowledge(payload["repo"], rec["path"] if rec else "")
            return self._json({"content": content})
        self._json({"error": "not found"}, 404)


def _bind() -> "tuple[ThreadingHTTPServer, str]":
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={_TOKEN}"
    return httpd, url


def start_server() -> "tuple[ThreadingHTTPServer, str]":
    """Bind and serve the API in a background daemon thread. Returns (httpd, url).

    Used by the native-window path and by headless tests (curl the url, then shutdown()).
    """
    httpd, url = _bind()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, url


def serve() -> None:
    """Launch the native desktop window (pywebview / WKWebView), like Tauri.

    The native window is the only UI. If pywebview is unavailable we fail loudly rather
    than falling back to a browser.
    """
    httpd, url = start_server()
    print(f"choobi window: {url}", flush=True)
    try:
        import webview
    except ImportError as exc:
        httpd.shutdown()
        raise RuntimeError("the native window needs pywebview — `pip install pywebview`") from exc
    webview.create_window("choobi", url, width=380, height=560, resizable=True)
    webview.start()
    httpd.shutdown()
