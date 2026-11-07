"""End-to-end check of an installed or built Retake: bundled ffmpeg, speech recognition, detection,
render, the local server. Used by CI on the built .app and handy on a tester's Mac:

  Retake.app/Contents/MacOS/Retake --selftest [--model tiny] [--keep]
  python retake/desktop.py --selftest

Exit code 0 = everything works.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

import runtime


def _say_clip(work: Path) -> Path | None:
    """A short spoken clip with a restart and a long pause (macOS `say`)."""
    if not shutil.which("say"):
        return None
    aiff = work / "speech.aiff"
    text = ("Hello, this is a short test of the editor. [[slnc 2500]] "
            "The second sentence has a mistake. Oops, again. The second sentence has no mistake.")
    r = subprocess.run(["say", "-o", str(aiff), text], capture_output=True, text=True)
    return aiff if r.returncode == 0 and aiff.exists() else None


def make_clip(work: Path) -> tuple[Path, bool]:
    speech = _say_clip(work)
    clip = work / "clip.mp4"
    if speech:
        audio = ["-i", str(speech)]
    else:  # no speech synthesizer: two tones with a gap (exercises everything but the words)
        audio = ["-f", "lavfi", "-i", "sine=f=440:d=3,apad=pad_dur=3"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    *audio, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                   check=True)
    return clip, bool(speech)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="Retake --selftest")
    ap.add_argument("--model", default=None, help="speech model (default: tiny on a Mac, the dev model elsewhere)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args(argv)

    import detect
    import render
    import transcribe

    print(f"Retake {runtime.VERSION} self-test · {runtime.platform_info()}", flush=True)
    work = Path(tempfile.mkdtemp(prefix="retake-selftest-"))
    failed: list[str] = []
    ctx: dict = {}

    def step(name, fn):
        t = time.time()
        try:
            info = fn()
            print(f"[ OK ] {name} ({time.time() - t:.1f} s){': ' + str(info) if info else ''}", flush=True)
        except Exception:  # noqa: BLE001
            failed.append(name)
            print(f"[FAIL] {name} ({time.time() - t:.1f} s)\n{traceback.format_exc()}", flush=True)

    def tools():
        out = []
        for t in ("ffmpeg", "ffprobe"):
            p = shutil.which(t)
            assert p, f"{t} not found"
            if runtime.FROZEN:
                assert str(runtime.resource_dir()) in p, f"{t} is not the bundled one: {p}"
            out.append(p)
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        need = ["libx264"] + (["h264_videotoolbox"] if sys.platform == "darwin" else [])
        missing = [e for e in need if e not in enc]
        assert not missing, f"encoders missing: {missing}"
        return f"{out[0]}; encoders ok; default {render.default_encoder()}"

    def clip():
        ctx["clip"], ctx["speech"] = make_clip(work)
        return f"{ctx['clip'].name}, speech={'say' if ctx['speech'] else 'none (tones)'}"

    def asr():
        backend = transcribe.default_backend()
        model = args.model or ("tiny" if backend == "mlx" else None)
        if backend == "mlx":
            backend += " on " + transcribe.mlx_device()
        last = [0.0]

        def prog(f, m):
            if time.time() - last[0] > 5:
                last[0] = time.time()
                print(f"       … {m} {'' if f is None else f'{f * 100:.0f}%'}", flush=True)

        tr = transcribe.transcribe(ctx["clip"], model=model, language="en" if ctx["speech"] else None, progress=prog)
        ctx["tr"] = tr
        words = sum(len(s["words"]) for s in tr["segments"])
        if ctx["speech"]:
            assert words >= 8, f"only {words} words recognised"
        text = " ".join(s["text"] for s in tr["segments"])
        return f"{backend}, {words} words: {text[:120]}"

    def find():
        doc = detect.detect(ctx["tr"], cache_dir=work, progress=lambda f, m: None)
        ctx["doc"] = doc
        kinds = {}
        for e in doc["edits"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        if ctx["speech"]:
            assert kinds.get("pause"), "the 2.5 s pause was not found"
        return kinds

    def cut():
        doc = ctx["doc"]
        info = render.probe(ctx["clip"])
        t0, t1 = doc["range"]
        keep = render.keep_segments(doc["edits"], t0, t1, info["fps"])
        out = work / "cut.mp4"
        used = render.render_mp4(ctx["clip"], keep, info["fps"], out, render.default_encoder(), t1)
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                            str(out)], capture_output=True, text=True, check=True)
        d = {s["codec_type"]: float(s["duration"]) for s in json.loads(r.stdout)["streams"]}
        assert abs(d["video"] - d["audio"]) < 0.1, f"audio/video length differ: {d}"
        return f"{used}: {info['duration']:.1f} s -> {d['video']:.1f} s"

    def uneven():
        """Frames not evenly spaced, fragments, timescale 30000: what a browser recording looks like."""
        src, out = work / "vfr.mp4", work / "vfr-cut.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=9",
                        "-f", "lavfi", "-i", "sine=f=440:duration=9",
                        "-vf", r"select='not(mod(n\,3))+gt(t\,4)',format=yuv420p", "-fps_mode", "vfr", "-c:v", "libx264",
                        "-video_track_timescale", "30000", "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", str(src)],
                       check=True)
        info = render.probe(src)
        assert info["vfr"], f"not seen as variable: {info}"
        keep = render.keep_segments([{"start": 2, "end": 5, "on": True}], 0, 9, info["fps"])
        used = render.render_mp4(src, keep, info["fps"], out, render.default_encoder(), 9)  # checks the lengths too
        return f"{used}: {info['fps']} fps, {info['duration']:.1f} s -> {render.probe(out)['video_duration']:.1f} s"

    def many_cuts():
        """More clips than a plain ffmpeg expression can hold: an ordinary lesson has that many."""
        src, out = work / "many.mp4", work / "many-cut.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=10",
                        "-f", "lavfi", "-i", "sine=f=440:duration=10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(src)], check=True)
        keep = [(i * 2, i * 2 + 1) for i in range(150)]
        used = render.render_mp4(src, keep, render.Fraction(30), out, render.default_encoder(), 10)  # checks the lengths
        return f"{used}: {len(keep)} clips, 10.0 s -> {render.probe(out)['video_duration']:.1f} s"

    def server():
        t = [time.time()]
        import app
        t.append(time.time())
        srv, url = app.start_server(port=0)
        t.append(time.time())
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via an HTTP proxy
        try:
            with local.open(url, timeout=10) as r:
                assert "window.RETAKE" in r.read().decode()
            t.append(time.time())
            req = urllib.request.Request(url + "api/state", headers={"X-Retake-Token": app.TOKEN})
            with local.open(req, timeout=10) as r:
                st = json.loads(r.read())
            assert st["version"] == runtime.VERSION
            t.append(time.time())
            for f in ("theme.css", "editor.css", "js/editor.js", "js/model.js", "js/player.js", "js/timeline.js",
                      "js/panels.js"):
                with local.open(url + "ui/" + f, timeout=10) as r:
                    assert r.status == 200 and r.read(), f"{f} is missing from the build"
            t.append(time.time())
            assert (app.UI / "editor.html").is_file(), "editor.html is missing from the build"
            steps = "import, start, page, state, files: " + ", ".join(f"{b - a:.1f}" for a, b in zip(t, t[1:]))
            return f"{url} ok ({steps} s), model: {st['model']}"
        finally:
            srv.shutdown()

    def window_lib():
        import webview  # noqa: F401
        if sys.platform == "darwin":
            from webview.platforms import cocoa  # noqa: F401  -- pyobjc + WebKit are bundled
        import llm
        if ctx.get("doc"):
            llm.build_messages(ctx["doc"], 0, len(ctx["doc"]["phrases"]), [])
        try:
            from importlib.metadata import version
            return "pywebview " + version("pywebview")
        except Exception:  # noqa: BLE001 -- no package metadata inside the app bundle
            return "pywebview imported"

    step("ffmpeg/ffprobe", tools)
    step("test clip", clip)
    if "test clip" not in failed:
        step("speech recognition", asr)
    if "tr" in ctx:
        step("detection", find)
    if "doc" in ctx:
        step("render", cut)
    step("render of a variable frame rate", uneven)
    step("render of many cuts", many_cuts)
    step("local server", server)
    step("window library", window_lib)

    if args.keep:
        print(f"files kept in {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print("SELFTEST " + ("PASSED" if not failed else f"FAILED: {', '.join(failed)}"), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    runtime.setup_tools()
    sys.exit(main(sys.argv[1:]))
