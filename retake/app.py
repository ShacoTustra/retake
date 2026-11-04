"""Retake as a local app: a small web server + pages in the browser.

  python retake/app.py            # opens http://127.0.0.1:8765 in the browser

Only listens on 127.0.0.1. Every API call carries a per-run token (embedded into the pages
the server serves), and the Host header is checked, so other web pages open in the same
browser cannot drive the app or read its settings.
"""
import argparse
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm as llm_mod  # noqa: E402
import models  # noqa: E402
import projects  # noqa: E402
import report  # noqa: E402
import runtime  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402

HERE = runtime.resource_dir()
UI = HERE / "ui"
TOKEN = secrets.token_urlsafe(18)
IS_WSL = "microsoft" in platform.release().lower()
JOBS: projects.Jobs | None = None
PORT = 0
DESKTOP = False  # set by desktop.py: file dialogs and drag-and-drop come from the native window


# ---------------------------------------------------------------- OS helpers

def pick_file() -> str | None:
    """Native "open file" dialog. Returns a path, None if cancelled; raises if unavailable."""
    if sys.platform == "darwin":
        r = subprocess.run(["osascript", "-e",
                            'POSIX path of (choose file with prompt "Выберите запись урока" of type {"public.movie"})'],
                           capture_output=True, text=True)
        return r.stdout.strip() or None
    if shutil.which("zenity") and os.environ.get("DISPLAY"):
        r = subprocess.run(["zenity", "--file-selection", "--title=Выберите запись урока",
                            "--file-filter=Видео | *.mp4 *.mov *.m4v *.mkv *.webm *.MP4 *.MOV"],
                           capture_output=True, text=True)
        return r.stdout.strip() or None
    raise RuntimeError("unsupported")


def reveal(path: Path) -> None:
    """Show a file in Finder / Explorer / the file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif IS_WSL:
        win = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
        subprocess.Popen(["explorer.exe", f"/select,{win}"])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path.parent)])


def open_browser(url: str) -> None:
    if IS_WSL:
        try:
            subprocess.Popen(["explorer.exe", url])
            return
        except OSError:
            pass
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()


# ---------------------------------------------------------------- request handling

class Handler(BaseHTTPRequestHandler):
    server_version = "Retake"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: only errors
        if args and str(args[1]).startswith(("4", "5")) and str(args[1]) != "404":
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- plumbing
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def error(self, msg: str, code: int = 400):
        self.json({"error": msg}, code)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            raise ValueError("слишком большой запрос")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw or b"{}")

    def page(self, name: str, project: str | None = None):
        html = (UI / name).read_text() if name != "review" else (HERE / "review_template.html").read_text()
        boot = json.dumps({"token": TOKEN, "project": project})
        html = html.replace("<!--RETAKE-->", f"<script>window.RETAKE = {boot};</script>", 1)
        self._send(200, html.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                               "style-src 'self' 'unsafe-inline'; media-src 'self'; "
                                               "img-src 'self' data:; connect-src 'self'"})

    def allowed(self, url) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False  # DNS rebinding
        if url.path.startswith(("/api/", "/media/")):
            tok = self.headers.get("X-Retake-Token") or parse_qs(url.query).get("t", [""])[0]
            return secrets.compare_digest(tok, TOKEN)
        return True

    def route(self):
        url = urlparse(self.path)
        if not self.allowed(url):
            return self.error("forbidden", 403)
        path, m = url.path, self.command
        try:
            if m in ("GET", "HEAD"):
                if path == "/":
                    return self.page("index.html")
                if r := re.fullmatch(r"/p/([\w-]+)", path):
                    return self.page("review", r[1]) if projects.get(r[1]) else self.error("нет проекта", 404)
                if r := re.fullmatch(r"/media/([\w-]+)", path):
                    return self.media(r[1])
                if path == "/api/state":
                    return self.json(state())
                if path == "/api/settings":
                    return self.json(config.public(config.load()))
                if r := re.fullmatch(r"/api/projects/([\w-]+)", path):
                    p = need(r[1])
                    return self.json(p.summary())
                if r := re.fullmatch(r"/api/projects/([\w-]+)/review", path):
                    return self.json(review_data(need(r[1])))
                return self.error("not found", 404)
            if m == "POST":
                return self.post(path, self.body())
            if m == "PUT":
                b = self.body()
                if path == "/api/settings":
                    return self.json(save_settings(b))
                if r := re.fullmatch(r"/api/projects/([\w-]+)/decisions", path):
                    need(r[1]).save_decisions(b)
                    return self.json({"ok": True})
            if m == "DELETE":
                if r := re.fullmatch(r"/api/projects/([\w-]+)", path):
                    p = need(r[1])
                    if (p.meta.get("status") or {}).get("state") in ("queued", "running"):
                        return self.error("сначала отмените обработку")
                    projects.delete(p)
                    return self.json({"ok": True})
            return self.error("not found", 404)
        except NotFound as e:
            return self.error(str(e), 404)
        except (ValueError, KeyError, TypeError, RuntimeError, llm_mod.LLMError) as e:
            return self.error(str(e) or e.__class__.__name__)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def post(self, path: str, b: dict):
        if path == "/api/model/download":
            start_model_download()
            return self.json(model_state())
        if path == "/api/report":
            p = projects.get(b.get("project") or "")
            out = report.build(p)
            reveal(out)
            return self.json({"path": str(out)})
        if path == "/api/log":
            reveal(runtime.log_path())
            return self.json({"path": str(runtime.log_path())})
        if path == "/api/open-feedback":
            if runtime.FEEDBACK_URL:
                webbrowser.open(runtime.FEEDBACK_URL)
            return self.json({"ok": True})
        if path == "/api/pick":
            try:
                return self.json({"path": pick_file()})
            except RuntimeError:
                return self.json({"unsupported": True})
        if path == "/api/projects":
            p = projects.create(Path(str(b.get("path", "")).strip().strip("'\"")))
            st = (p.meta.get("status") or {}).get("state")
            if st in ("new", "error", "cancelled") or not (p.dir / "edits.json").exists():
                if st not in ("queued", "running"):
                    JOBS.submit(p, "process")
            return self.json(p.summary())
        if path == "/api/llm/models":
            s = config.load()
            cfg = llm_mod.LLMConfig.from_settings(
                s, base_url=b.get("base_url") or None, api_key=b.get("api_key") or None, timeout=20)
            cfg.model = b.get("model") or cfg.model
            models = llm_mod.list_models(cfg)
            return self.json({"models": models, "model_found": (cfg.model in models) if cfg.model else None})
        if r := re.fullmatch(r"/api/projects/([\w-]+)/(\w+)", path):
            p, action = need(r[1]), r[2]
            if action == "process":
                JOBS.submit(p, "process", force_transcribe=bool(b.get("force_transcribe")))
            elif action == "detect":
                JOBS.submit(p, "detect")
            elif action == "llm":
                JOBS.submit(p, "llm")
            elif action == "cancel":
                JOBS.request_cancel(p)
            elif action == "render":
                if b.get("format") == "fcpxml":
                    return self.json({"output": str(projects.write_fcpxml(p))})
                JOBS.submit(p, "render", out=str(projects.output_path(p, ".mp4")))
            elif action == "reveal":
                target = Path(b.get("path") or p.meta.get("output") or p.source)
                allowed = {str(p.source), str(p.meta.get("output") or "")}
                if str(target) not in allowed and not str(target).endswith(".fcpxml"):
                    return self.error("forbidden", 403)
                reveal(target)
            elif action == "relink":
                new = Path(str(b.get("path", "")).strip())
                if not new.is_file():
                    return self.error(f"файл не найден: {new}")
                p.save_meta(source=str(new.resolve()))
            else:
                return self.error("not found", 404)
            return self.json(p.summary())
        return self.error("not found", 404)

    def media(self, pid: str):
        """The source video, with Range support (Safari will not play or seek without it)."""
        p = need(pid)
        f = p.source
        if not f.is_file():
            return self.error("видео не найдено", 404)
        size = f.stat().st_size
        ctype = mimetypes.guess_type(f.name)[0] or "video/mp4"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (mr := re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())):
            if mr[1]:
                start = int(mr[1])
                end = int(mr[2]) if mr[2] else size - 1
            elif mr[2]:  # suffix range: the last N bytes
                start = max(0, size - int(mr[2]))
            end = min(end, size - 1)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = route


class NotFound(Exception):
    pass


def need(pid: str) -> projects.Project:
    p = projects.get(pid)
    if not p:
        raise NotFound("нет такого проекта")
    return p


# ---------------------------------------------------------------- API helpers

def llm_ready(s: dict) -> bool:
    return bool(s["llm"]["base_url"] and s["llm"]["model"])


def model_state() -> dict:
    """Is the speech model there? Only the Mac (MLX) build downloads it into the app folder."""
    s = config.load()
    name = s["whisper"]["model"] or transcribe_mod.default_model()
    if transcribe_mod.default_backend() != "mlx" or name not in models.MLX_MODELS:
        return {"needed": False, "ready": True}
    st = dict(models.status) if models.status.get("name") == name else {"state": "idle"}
    return {"needed": True, "ready": models.is_ready(name), "size": models.total_size(name), **st}


_dl_thread: threading.Thread | None = None


def start_model_download() -> None:
    global _dl_thread
    if _dl_thread and _dl_thread.is_alive():
        return
    s = config.load()
    name = s["whisper"]["model"] or transcribe_mod.default_model()
    if name not in models.MLX_MODELS or models.is_ready(name):
        return

    def run():
        try:
            models.ensure(name)
        except Exception as e:  # noqa: BLE001 -- shown through models.status
            print(f"model download failed: {e}", file=sys.stderr)

    _dl_thread = threading.Thread(target=run, daemon=True)
    _dl_thread.start()


def state() -> dict:
    s = config.load()
    return {
        "projects": [p.summary() for p in projects.list_projects()],
        "settings": config.public(s),
        "llm_ready": llm_ready(s),
        "platform": "wsl" if IS_WSL else sys.platform,
        "desktop": DESKTOP,
        "can_pick": DESKTOP or sys.platform == "darwin" or bool(shutil.which("zenity") and os.environ.get("DISPLAY")),
        "version": runtime.VERSION,
        "feedback_url": bool(runtime.FEEDBACK_URL),
        "model": model_state(),
    }


def review_data(p: projects.Project) -> dict:
    doc = p.edits()
    if not doc:
        raise RuntimeError("урок ещё не обработан")
    edits = p.final_edits()
    defaults = {e["id"]: e["on"] for e in doc["edits"]}
    for e in edits:
        e["default_on"] = defaults.get(e["id"], True)
    s = config.load()
    return {
        "project": p.summary(),
        "video": f"/media/{p.id}?t={TOKEN}",
        "source": doc["source"],
        "range": doc["range"],
        "words": doc["words"],
        "phrases": doc["phrases"],
        "edits": edits,
        "llm": doc.get("llm"),
        "llm_ready": llm_ready(s),
        "llm_model": s["llm"]["model"],
    }


def save_settings(b: dict) -> dict:
    patch = {}
    for sect, keys in {"whisper": ("model", "language", "hf_endpoint"),
                       "detect": ("min_pause", "keep_pause", "retake_window", "phrase_gap"),
                       "llm": ("enabled", "base_url", "model", "temperature", "timeout", "max_chars",
                               "max_tokens", "reasoning_effort", "auto_apply"),
                       "render": ("encoder",)}.items():
        for k in keys:
            if k in (b.get(sect) or {}):
                patch.setdefault(sect, {})[k] = b[sect][k]
    for k in ("min_pause", "keep_pause", "retake_window", "phrase_gap"):
        if k in patch.get("detect", {}):
            patch["detect"][k] = float(patch["detect"][k])
    llm = b.get("llm") or {}
    if llm.get("api_key"):  # empty = keep the current key
        patch.setdefault("llm", {})["api_key"] = str(llm["api_key"]).strip()
    if llm.get("clear_key"):
        patch.setdefault("llm", {})["api_key"] = ""
    if "base_url" in patch.get("llm", {}):
        patch["llm"]["base_url"] = str(patch["llm"]["base_url"]).strip().rstrip("/")
    return config.public(config.save(patch))


# ---------------------------------------------------------------- main

def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def recover_jobs() -> None:
    """A job that was running when the app was closed will not resume by itself."""
    for p in projects.list_projects():
        st = p.meta.get("status") or {}
        if st.get("state") in ("queued", "running"):
            st.update(state="error", error="приложение было закрыто во время обработки")
            p.save_meta(status=st)


def jobs_running() -> bool:
    return any((p.meta.get("status") or {}).get("state") in ("queued", "running") for p in projects.list_projects())


def start_server(port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    """Start the job queue and the HTTP server in background threads; returns (server, url)."""
    global JOBS, PORT
    recover_jobs()
    JOBS = projects.Jobs()
    PORT = free_port(port)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{PORT}/"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    runtime.setup_logging()
    runtime.setup_tools()
    if not runtime.single_instance():
        print("Retake уже запущен (тот же каталог данных).", file=sys.stderr)
        sys.exit(1)
    srv, url = start_server(args.port)
    print(f"Retake {runtime.VERSION}: {url}  (данные: {config.app_dir()})", flush=True)
    if not args.no_browser:
        open_browser(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
