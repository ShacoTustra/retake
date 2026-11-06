"""Projects (one per source video) and the background job queue that processes them.

<app dir>/projects/<id>/
  project.json     source path, name, status of the last job
  transcript.json  Whisper output
  edits.json       suggested cuts (rules + optional LLM opinion)
  decisions.json   what the reviewer changed, see Project.save_decisions
  media.npz        cached loudness envelope / static-screen intervals
  peaks.u8         the same envelope as bytes, for the timeline waveform
  thumbs.jpg/.json filmstrip for the video track
"""
import hashlib
import json
import math
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import detect as detect_mod  # noqa: E402
import llm as llm_mod  # noqa: E402
import render as render_mod  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
THUMB_HEIGHT = 72       # pixels; the video track draws them at about this size on a Retina screen
THUMB_COLS = 25
THUMB_MIN_INTERVAL = 5.0
THUMB_MAX_COUNT = 625   # a longer video gets rarer frames, the sprite stays about the same size


class Cancelled(Exception):
    pass


def projects_dir() -> Path:
    d = config.app_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)


class Project:
    def __init__(self, pid: str):
        self.id = pid
        self.dir = projects_dir() / pid

    # ---- files
    @property
    def meta(self) -> dict:
        return _read(self.dir / "project.json", {})

    def save_meta(self, **patch) -> dict:
        m = self.meta
        m.update(patch)
        _write(self.dir / "project.json", m)
        return m

    @property
    def source(self) -> Path:
        return Path(self.meta["source"])

    def transcript(self) -> dict | None:
        return _read(self.dir / "transcript.json")

    def edits(self) -> dict | None:
        return _read(self.dir / "edits.json")

    def decisions(self) -> dict:
        return _read(self.dir / "decisions.json", {"overrides": {}, "manual": [], "splits": []})

    def save_decisions(self, d: dict) -> None:
        """overrides: {id: {on, start, end, orig_start, orig_end, reviewed}}: changes to a suggestion.
        orig_* is the span the suggestion had when it was changed, see final_edits.
        manual: cuts the reviewer made. splits: positions of the blade on the timeline."""
        overrides = {}
        for k, v in (d.get("overrides") or {}).items():
            start, end = float(v["start"]), float(v["end"])
            if end <= start:
                raise ValueError("конец правки раньше начала")
            overrides[str(k)] = {"on": bool(v["on"]), "start": start, "end": end,
                                 "orig_start": float(v.get("orig_start", start)),
                                 "orig_end": float(v.get("orig_end", end)),
                                 "reviewed": bool(v.get("reviewed"))}
        clean = {
            "overrides": overrides,
            "manual": [{"id": int(m["id"]), "kind": "manual", "start": float(m["start"]), "end": float(m["end"]),
                        "on": bool(m.get("on", True)), "reviewed": True,
                        "removed": str(m.get("removed") or "ручная вырезка")[:4000]}
                       for m in (d.get("manual") or []) if float(m["end"]) > float(m["start"])],
            "splits": sorted(float(t) for t in (d.get("splits") or [])),
        }
        _write(self.dir / "decisions.json", clean)

    # ---- the reviewed result
    def final_edits(self) -> list[dict]:
        """Suggestions with the reviewer's changes applied (what the page shows and render uses)."""
        doc = self.edits()
        if not doc:
            return []
        dec = self.decisions()
        ov = dec.get("overrides", {})
        out = []
        for e in doc["edits"]:
            e = dict(e)
            e["orig_start"], e["orig_end"] = e["start"], e["end"]
            o = ov.get(str(e["id"]))
            # A decision only applies to the same cut: ids can be reused after re-detection.
            # Files written before trimming existed have no orig_*: their start/end are the original span.
            if o and abs(o.get("orig_start", o["start"]) - e["start"]) < 0.05 \
                    and abs(o.get("orig_end", o["end"]) - e["end"]) < 0.05:
                e["on"] = o["on"]
                e["start"], e["end"] = o["start"], o["end"]
                e["reviewed"] = bool(o.get("reviewed"))
                e["changed"] = True
            out.append(e)
        out += [dict(m) for m in dec.get("manual", [])]
        return out

    # ---- what the timeline draws
    def video_info(self) -> dict:
        """Frame size and working rate of the source, remembered in project.json."""
        m = self.meta
        src = str(m.get("source") or "")
        info = m.get("video")
        if info and info.get("source") == src:
            return info
        p = render_mod.probe(self.source)
        info = {"source": src, "w": p["w"], "h": p["h"], "fps": float(p["fps"])}
        self.save_meta(video=info)
        return info

    def peaks(self) -> bytes:
        """Loudness in 10 ms steps as bytes: 0 is -80 dB or quieter, 255 is 0 dB."""
        out = self.dir / "peaks.u8"
        cache = self.dir / "media.npz"
        if out.exists() and (not cache.exists() or out.stat().st_mtime >= cache.stat().st_mtime):
            return out.read_bytes()
        doc = self.edits()
        if not doc:
            raise RuntimeError("урок ещё не обработан")
        t0, t1 = doc["range"]
        env = detect_mod.analyze_media(self.source, t0, t1, cache_dir=self.dir)[0]
        data = (np.clip((env + 80.0) / 80.0, 0.0, 1.0) * 255.0).astype(np.uint8).tobytes()
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return data

    def thumbs(self) -> dict | None:
        """Filmstrip description, None until it is made."""
        if (self.dir / "thumbs.jpg").exists():
            return _read(self.dir / "thumbs.json")
        return None

    def make_thumbs(self) -> dict:
        """One sprite with a frame every few seconds. Only keyframes are decoded, so even a long
        lesson takes seconds; a frame can be late by one keyframe interval, enough to recognise a page."""
        info = self.video_info()
        duration = float(self.meta.get("duration") or render_mod.probe(self.source)["duration"])
        interval = max(THUMB_MIN_INTERVAL, duration / THUMB_MAX_COUNT)
        count = int(duration / interval) + 2
        rows = math.ceil(count / THUMB_COLS)
        h = THUMB_HEIGHT
        w = 2 * round(h * info["w"] / info["h"] / 2)
        tmp = self.dir / "thumbs.part.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-skip_frame", "nokey", "-i", str(self.source),
                        "-vf", f"fps=1/{interval},scale={w}:{h},tile={THUMB_COLS}x{rows}",
                        "-frames:v", "1", "-q:v", "5", str(tmp)],
                       check=True, capture_output=True, text=True)
        meta = {"w": w, "h": h, "cols": THUMB_COLS, "rows": rows, "count": count, "interval": interval}
        _write(self.dir / "thumbs.json", meta)
        tmp.replace(self.dir / "thumbs.jpg")
        return meta

    def summary(self) -> dict:
        m = self.meta
        out = {k: m.get(k) for k in ("id", "name", "source", "duration", "created", "status", "output", "llm_error")}
        out["exists"] = Path(m.get("source", "")).exists()
        out["thumbs"] = self.thumbs()
        doc = self.edits()
        if doc:
            t0, t1 = doc["range"]
            cuts = sorted((e["start"], e["end"]) for e in self.final_edits() if e.get("on"))
            total, cur = 0.0, None
            for a, b in cuts:  # merged length of the cuts
                if cur and a <= cur[1]:
                    cur[1] = max(cur[1], b)
                else:
                    if cur:
                        total += cur[1] - cur[0]
                    cur = [a, b]
            if cur:
                total += cur[1] - cur[0]
            kinds = {}
            for e in doc["edits"]:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
            out["result"] = {"length": t1 - t0, "cut": total, "kinds": kinds,
                             "llm": (doc.get("llm") or {}).get("model")}
        return out


def project_id(src: Path) -> str:
    h = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:8]
    stem = "".join(c if c.isalnum() else "-" for c in src.stem)[:40].strip("-") or "video"
    return f"{stem}-{h}"


def list_projects() -> list[Project]:
    ps = [Project(d.name) for d in projects_dir().iterdir() if (d / "project.json").exists()]
    return sorted(ps, key=lambda p: p.meta.get("created", 0), reverse=True)


def get(pid: str) -> Project | None:
    if not pid or "/" in pid or pid.startswith("."):
        return None
    p = Project(pid)
    return p if (p.dir / "project.json").exists() else None


def create(src: Path) -> Project:
    src = src.expanduser().resolve()
    if not src.is_file():
        raise ValueError(f"файл не найден: {src}")
    if src.suffix.lower() not in VIDEO_EXT:
        raise ValueError(f"это не видео: {src.name}")
    p = Project(project_id(src))
    if (p.dir / "project.json").exists():
        return p  # the same video again: reopen
    p.dir.mkdir(parents=True, exist_ok=True)
    p.save_meta(id=p.id, name=src.stem, source=str(src), created=time.time(),
                duration=transcribe_mod.media_duration(src), status={"state": "new"})
    return p


def delete(p: Project) -> None:
    shutil.rmtree(p.dir, ignore_errors=True)


# ---------------------------------------------------------------- jobs

class Jobs:
    """One worker: transcription and rendering both saturate the machine, so jobs run one at a time."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.cancel: set[str] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, p: Project, kind: str, **kw) -> None:
        st = p.meta.get("status") or {}
        if st.get("state") in ("queued", "running"):
            raise ValueError("проект уже обрабатывается")
        self.cancel.discard(p.id)
        self._status(p, state="queued", job=kind, progress=None, message="в очереди", error=None)
        self.q.put((p.id, kind, kw))

    def request_cancel(self, p: Project) -> None:
        st = p.meta.get("status") or {}
        if st.get("state") == "queued":
            self._status(p, state="cancelled", message="отменено")
        self.cancel.add(p.id)

    def _status(self, p: Project, **st) -> None:
        with self.lock:
            m = p.meta
            cur = m.get("status") or {}
            cur.update(st)
            cur["updated"] = time.time()
            p.save_meta(status=cur)

    def _worker(self):
        while True:
            pid, kind, kw = self.q.get()
            p = get(pid)
            if not p or pid in self.cancel:
                self.cancel.discard(pid)
                continue
            last = [0.0, None]

            def progress(frac, msg):
                if pid in self.cancel:
                    raise Cancelled()
                now = time.time()
                # don't rewrite project.json more than twice a second, unless the step changed
                if now - last[0] > 0.5 or msg != last[1] or frac in (None, 1.0):
                    last[0], last[1] = now, msg
                    self._status(p, progress=frac, message=msg)

            self._status(p, state="running", progress=None, message="запуск", started=time.time())
            try:
                getattr(self, "_job_" + kind)(p, progress, **kw)
                self._status(p, state="done", progress=1.0, message="готово")
            except Cancelled:
                self._status(p, state="cancelled", message="отменено")
            except Exception as e:  # noqa: BLE001 -- shown to the user
                traceback.print_exc()
                self._status(p, state="error", message="ошибка", error=str(e) or e.__class__.__name__)
            finally:
                self.cancel.discard(pid)

    # ---- job kinds
    def _job_process(self, p: Project, progress, force_transcribe: bool = False):
        s = config.load()
        if force_transcribe or not (p.dir / "transcript.json").exists():
            tr = transcribe_mod.transcribe(p.source, model=s["whisper"]["model"] or None,
                                           language=s["whisper"]["language"] or None, progress=progress)
            _write(p.dir / "transcript.json", tr)
        self._job_detect(p, progress)
        if s["llm"]["enabled"]:
            try:
                self._job_llm(p, progress)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 -- the rule-based result is still usable
                p.save_meta(llm_error=str(e))

    def _job_detect(self, p: Project, progress):
        s = config.load()
        tr = p.transcript()
        if not tr:
            raise RuntimeError("нет расшифровки")
        tr["source"] = str(p.source)  # the video may have been moved since transcription
        opts = detect_mod.Options(**{k: float(v) for k, v in s["detect"].items()})
        doc = detect_mod.detect(tr, opts, cache_dir=p.dir, progress=progress)
        _write(p.dir / "edits.json", doc)
        p.save_meta(llm_error=None)

    def _job_llm(self, p: Project, progress):
        s = config.load()
        doc = p.edits()
        if not doc:
            raise RuntimeError("сначала нужна обработка")
        cfg = llm_mod.LLMConfig.from_settings(s)
        p.save_meta(llm_error=None)
        try:
            llm_mod.review(doc, cfg, cache_dir=p.dir, progress=progress,
                           auto_apply=s["llm"].get("auto_apply", "high"))
        except llm_mod.LLMError as e:
            p.save_meta(llm_error=str(e))
            raise
        _write(p.dir / "edits.json", doc)

    def _job_render(self, p: Project, progress, out: str):
        doc = p.edits()
        src = p.source
        info = render_mod.probe(src)
        t0, t1 = doc["range"]
        edits = p.final_edits()
        keep = render_mod.keep_segments(edits, t0, t1, info["fps"])
        out_p = Path(out)
        s = config.load()
        enc = s["render"]["encoder"] or render_mod.default_encoder()
        _write(p.dir / "edits.final.json", {"source": str(src), "range": [t0, t1], "edits": edits})
        tmp = out_p.with_name(out_p.stem + ".part" + out_p.suffix)
        render_mod.render_mp4(src, keep, info["fps"], tmp, enc, t1, progress)
        tmp.replace(out_p)
        p.save_meta(output=str(out_p))


def output_path(p: Project, ext: str) -> Path:
    """Next to the source video when possible, otherwise in the app folder."""
    src = p.source
    name = f"{src.stem} (монтаж){ext}"
    if src.parent.exists() and _writable(src.parent):
        return src.parent / name
    d = config.app_dir() / "exports"
    d.mkdir(exist_ok=True)
    return d / name


def _writable(d: Path) -> bool:
    try:
        t = d / ".retake-write-test"
        t.write_text("")
        t.unlink()
        return True
    except OSError:
        return False


def write_fcpxml(p: Project) -> Path:
    doc = p.edits()
    src = p.source
    info = render_mod.probe(src)
    t0, t1 = doc["range"]
    keep = render_mod.keep_segments(p.final_edits(), t0, t1, info["fps"])
    out = output_path(p, ".fcpxml")
    render_mod.write_fcpxml(src, keep, info, out)
    return out
