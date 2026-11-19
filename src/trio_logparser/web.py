"""``trio-logs web``: a browser UI over Trio log exports or the organized tree, served locally.

The server listens on 127.0.0.1 only and answers only requests addressed to a loopback host name
(so another site can't reach it through DNS rebinding); pages load nothing from elsewhere and are
not cached. Log text is masked as ``show`` masks it unless ``--unmasked``.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import historylog, viewer
from .dataset import alias, discover, load_headers
from .export import TITLES
from .parse import is_export

MAX_DAYS = 7  # days of the organized tree one view loads
CACHED_VIEWS = 4
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class NotFound(Exception):
    pass


class App:
    """The catalog of what can be viewed, and the views loaded so far."""

    def __init__(self, files: list[Path], root: Path | None, *, names: bool = False, unmasked: bool = False,
                 tandemkit_repo: Path | None = None, tandemkit_rev: str = "HEAD"):
        self.files = files
        self.root = root
        self.names = names
        self.unmasked = unmasked
        self.tk_repo = tandemkit_repo
        self.tk_rev = tandemkit_rev
        self.people: list[tuple[str, dict[str, object]]] = []  # (person, {day: Day})
        if root:
            by_person: dict[str, dict] = collections.defaultdict(dict)
            for d in discover(root):
                by_person[d.person][d.day] = d
            self.people = sorted(by_person.items())
        self._lock = threading.Lock()
        self._views: collections.OrderedDict[str, tuple[viewer.Selection, bytes]] = collections.OrderedDict()
        self._key_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._history: dict[str, dict] = {}
        self._exporter: Path | None = None
        self._exporter_state = "unknown"  # unknown | building | ready | unavailable
        self._exporter_message = ""

    def who(self, person: str) -> str:
        return person if self.names else alias(person)

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> dict:
        out = {"files": [{"id": f"f{i}", "label": p.name} for i, p in enumerate(self.files)],
               "people": [], "max_days": MAX_DAYS, "masked": not self.unmasked}
        for n, (person, days) in enumerate(self.people):
            out["people"].append({"id": f"p{n}", "label": self.who(person), "days": [
                {"day": day, "kind": "export" if d.export_path else "old"} for day, d in sorted(days.items())]})
        return out

    def _select(self, key: str) -> viewer.Selection:
        if key.startswith("f") and key[1:].isdigit() and int(key[1:]) < len(self.files):
            path = self.files[int(key[1:])]
            return viewer.from_file(path, "")  # no person: the file is the label
        try:
            pid, start, count = key.split("/")
            person, days = self.people[int(pid.removeprefix("p"))]
            count = max(1, min(MAX_DAYS, int(count)))
        except (ValueError, IndexError):
            raise NotFound(key) from None
        chosen = [d for day, d in sorted(days.items()) if day >= start][:count]
        if not chosen or chosen[0].day != start:
            raise NotFound(key)
        # fresh Day objects: a view parses its own copy
        fresh = [type(d)(d.person, d.day, d.trio_path, d.tandem_path, d.export_path) for d in chosen]
        headers = load_headers(self.root, [person]).get(person, []) if self.root else []
        lo, hi = fresh[0].day, fresh[-1].day
        headers = [h for h in headers if h.created and lo <= h.created.date().isoformat()
                   and (h.window_start is None or h.window_start.date().isoformat() <= hi)]
        return viewer.from_days(fresh, headers, self.who(person))

    def view(self, key: str) -> tuple[viewer.Selection, bytes]:
        with self._lock:
            if key in self._views:
                self._views.move_to_end(key)
                return self._views[key]
            key_lock = self._key_locks[key]
        with key_lock:
            with self._lock:
                if key in self._views:
                    return self._views[key]
            sel = self._select(key)
            data = viewer.bundle(sel, names=self.names, unmasked=self.unmasked)
            data["key"] = key
            body = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), compresslevel=5)
            with self._lock:
                self._views[key] = (sel, body)
                while len(self._views) > CACHED_VIEWS:
                    old, _ = self._views.popitem(last=False)
                    self._history.pop(old, None)
            return sel, body

    # ------------------------------------------------------------------ pump history

    def _find_exporter(self) -> None:
        try:
            if self.tk_repo:
                self._exporter_message = "building TandemKit's history-log decoder (once per commit, a few minutes)…"
                self._exporter = historylog.build_exporter(self.tk_repo, self.tk_rev, log=lambda _m: None)
            else:
                cached = historylog.cached_exporters()  # the newest one built, e.g. by history-jsonl
                self._exporter = cached[-1] if cached else None
            if self._exporter:
                self._exporter_state, self._exporter_message = "ready", f"decoded with TandemKit {self._exporter.parent.name[:7]}"
            else:
                self._exporter_state = "unavailable"
                self._exporter_message = ("No TandemKit decoder: start with --tandemkit-repo (or $TANDEMKIT_REPO) to "
                                          "decode records; showing record headers only.")
        except historylog.ExportError as e:
            self._exporter_state, self._exporter_message = "unavailable", f"decoder build failed: {e}"

    def history(self, key: str) -> dict:
        with self._lock:
            state = self._history.get(key)
            if state is None:
                state = self._history[key] = {"status": "working", "message": "decoding…"}
                threading.Thread(target=self._decode, args=(key, state), daemon=True).start()
            out = dict(state)
            if out["status"] == "working" and self._exporter_state == "building":
                out["message"] = self._exporter_message
            return out

    def _decode(self, key: str, state: dict) -> None:
        try:
            with self._lock:
                need = self._exporter_state == "unknown"
                if need:
                    self._exporter_state = "building"
            if need:
                self._find_exporter()
            while self._exporter_state == "building":
                threading.Event().wait(0.5)
            sel, _ = self.view(key)
            result = viewer.history(sel, self._exporter, unmasked=self.unmasked)
            state.update(status="ready", message=self._exporter_message, **result)
        except Exception as e:  # noqa: BLE001 - reported to the page, which would otherwise keep polling
            traceback.print_exc()
            state.update(status="error", message=str(e))


def _host_ok(host: str) -> bool:
    if host.startswith("["):
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in LOOPBACK


def make_handler(app: App, verbose: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "trio-logs"

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str, gzipped: bool = False) -> None:
            if gzipped and "gzip" not in self.headers.get("Accept-Encoding", ""):
                body, gzipped = gzip.decompress(body), False
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):
            if not _host_ok(self.headers.get("Host", "")):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in STATIC:
                    name, ctype = STATIC[url.path]
                    body = resources.files(__package__).joinpath("web", name).read_bytes()
                    self._send(200, body, ctype)
                elif url.path == "/api/catalog":
                    self._json(app.catalog())
                elif url.path == "/api/view":
                    _, body = app.view(q.get("key", ""))
                    self._send(200, body, "application/json", gzipped=True)
                elif url.path == "/api/history":
                    self._json(app.history(q.get("key", "")))
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except NotFound:
                self._json({"error": "no such view"}, HTTPStatus.NOT_FOUND)
            except Exception as e:  # noqa: BLE001 - any failure becomes a JSON error for the page
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def cmd_web(args: argparse.Namespace) -> int:
    files = []
    for f in args.file or []:
        path = Path(f).expanduser()
        if not path.is_file() or not is_export(path):
            sys.exit(f"error: {path} is not a Trio log export (first line must be one of {', '.join(TITLES)})")
        files.append(path)
    dest = args.dest or os.environ.get("TRIO_LOGS_DIR")
    root = Path(dest).expanduser() if dest and not (files and not args.dest) else None
    if not files and not root:
        sys.exit("error: pass --file <export>, --dest, or set TRIO_LOGS_DIR")
    repo = args.tandemkit_repo or os.environ.get("TANDEMKIT_REPO")
    app = App(files, root, names=args.names, unmasked=args.unmasked,
              tandemkit_repo=Path(repo).expanduser() if repo else None, tandemkit_rev=args.tandemkit_rev)
    if args.host not in LOOPBACK:
        sys.exit("error: the viewer serves health data and only listens on loopback (127.0.0.1, ::1, localhost)")
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(app, args.verbose))
    except OSError:
        server = ThreadingHTTPServer((args.host, 0), make_handler(app, args.verbose))
    host = "[::1]" if args.host == "::1" else args.host
    url = f"http://{host}:{server.server_address[1]}/"
    what = [f"{len(files)} export file(s)"] if files else []
    if root:
        what.append(f"{len(app.people)} people in {root}")
    print(f"trio-logs web: {', '.join(what)} at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def add_command(sub) -> None:
    p = sub.add_parser("web", help="browse logs in a local web UI: log lines, CGM, oref, doses, pump comms, "
                                   "pump history, findings")
    p.add_argument("--file", action="append", help="a Trio log export to open; repeatable")
    p.add_argument("--dest", help="organized log root (default: $TRIO_LOGS_DIR, unless --file is given)")
    p.add_argument("--port", type=int, default=8765, help="port (default 8765; any free port if taken)")
    p.add_argument("--host", default="127.0.0.1", help="loopback address to listen on (default 127.0.0.1)")
    p.add_argument("--names", action="store_true", help="show full tester names instead of aliases")
    p.add_argument("--unmasked", action="store_true", help="show log text as logged (no masking of serials, emails)")
    p.add_argument("--tandemkit-repo", help="TandemKit checkout for decoding pump history (default: $TANDEMKIT_REPO)")
    p.add_argument("--tandemkit-rev", default="HEAD", help="TandemKit revision whose decoders to use (default: HEAD)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.add_argument("-v", "--verbose", action="store_true", help="log each request")
    p.set_defaults(func=cmd_web)
