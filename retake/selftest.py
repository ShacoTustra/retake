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

    def server():
        import app
        srv, url = app.start_server(port=0)
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via an HTTP proxy
        try:
            html = local.open(url, timeout=10).read().decode()
            assert "window.RETAKE" in html
            req = urllib.request.Request(url + "api/state", headers={"X-Retake-Token": app.TOKEN})
            st = json.loads(local.open(req, timeout=10).read())
            assert st["version"] == runtime.VERSION
            return f"{url} ok, model: {st['model']}"
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
