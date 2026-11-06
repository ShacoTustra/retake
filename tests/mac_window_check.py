"""The editor in a real WKWebView, the engine of the Mac app (macOS only, run by CI):

  .venv/bin/python tests/mac_window_check.py

Opens a normal file and one like MediaRecorder writes (variable frame rate, fragments, Opus),
plays across a cut and reports what the engine did. A normal file must work; the result for
the second one is printed as a fact to act on.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["RETAKE_HOME"] = tempfile.mkdtemp(prefix="retake-window-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retake"))
import runtime  # noqa: E402

runtime.setup_tools()
import webview  # noqa: E402

import app  # noqa: E402
import projects  # noqa: E402

CUT = (8.0, 10.0)
CHECK = """
(async () => {
  const r = {}, a = window.retake, P = a.player, wait = ms => new Promise(f => setTimeout(f, ms));
  const v = P.cur;
  r.canPlay = { aac: v.canPlayType('video/mp4; codecs="avc1.640028,mp4a.40.2"'),
                opus: v.canPlayType('video/mp4; codecs="avc1.42E01E,opus"') };
  for (let i = 0; i < 100 && v.readyState < 2 && !v.error; i++) await wait(100);
  r.readyState = v.readyState;
  r.error = v.error ? v.error.message || String(v.error.code) : null;
  r.duration = v.duration;
  for (let i = 0; i < 100 && !a.model.peaks; i++) await wait(100);  // made on first request here
  r.waveform = !!a.model.peaks;
  r.frameStep = 1 / a.model.fps;
  const nav = performance.getEntriesByType('navigation')[0];
  r.pageLoadMs = nav ? Math.round(nav.loadEventEnd) : null;
  const st0 = performance.now();
  await fetch('/api/state', { headers: { 'X-Retake-Token': window.RETAKE.token } });
  r.stateMs = Math.round(performance.now() - st0);
  if (v.readyState >= 2) {
    r.seekMs = [];
    for (const t of [15, 3, 12, 6]) {
      const t0 = performance.now();
      P.seek(t);
      await new Promise(f => { v.addEventListener('seeked', f, { once: true }); setTimeout(f, 8000); });
      r.seekMs.push(Math.round(performance.now() - t0));
    }
    P.onPlayError = e => { r.playError = e.name + ': ' + e.message; };
    let jump = null;
    P.onJump = () => { jump = performance.now(); };
    P.seek(6.5);
    await wait(500);
    const first = P.cur;
    P.play();
    const t0 = performance.now();
    while (P.time < 11 && performance.now() - t0 < 12000) await wait(30);
    r.timeAfterCut = P.time;
    r.jumped = jump !== null;
    r.secondVideoTookOver = P.cur !== first;
    r.stillTwoVideos = P.dual;
    r.playing = !P.paused;
    r.audioTracks = P.cur.audioTracks ? P.cur.audioTracks.length : null;
    // is there sound, or only a picture? Measure the signal; the AAC file is the control
    try {
      const ctx = new AudioContext();
      await ctx.resume().catch(() => {});
      const an = ctx.createAnalyser();
      ctx.createMediaElementSource(P.cur).connect(an);
      an.connect(ctx.destination);
      const buf = new Float32Array(an.fftSize);
      let peak = 0;
      for (let i = 0; i < 40; i++) {
        an.getFloatTimeDomainData(buf);
        for (const x of buf) peak = Math.max(peak, Math.abs(x));
        await wait(50);
      }
      r.audioContext = ctx.state;
      r.audioPeak = +peak.toFixed(4);
    } catch (e) { r.audioError = String(e); }
    P.pause();
  }
  window.__check = r;
})();
"""


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_clips(work: Path) -> dict:
    normal, recorder = work / "normal.mp4", work / "recorder.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=20", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=20",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "50", "-c:a", "aac", str(normal))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=20",
           "-vf", r"select='not(mod(n\,3))+gt(t\,6)',format=yuv420p", "-fps_mode", "vfr", "-c:v", "libx264",
           "-profile:v", "baseline", "-g", "150", "-video_track_timescale", "30000", "-c:a", "libopus",
           "-movflags", "frag_keyframe+empty_moov", str(recorder))
    return {"normal": normal, "recorder": recorder}


def make_project(src: Path) -> projects.Project:
    p = projects.create(src)
    words = [{"s": t + 0.1, "e": t + 0.8, "t": f" слово{t}"} for t in range(20)]
    phrases = [{"start": w["s"], "end": w["e"], "w0": i, "w1": i + 1} for i, w in enumerate(words)]
    doc = {"source": str(src), "range": [0.0, 20.0], "words": words, "phrases": phrases,
           "edits": [{"id": 1, "kind": "retake", "start": CUT[0], "end": CUT[1], "on": True, "why": "проверка"}]}
    (p.dir / "edits.json").write_text(json.dumps(doc, ensure_ascii=False))
    p.save_meta(status={"state": "done"})
    return p


def run(win, url: str, clips: dict, results: dict):
    try:
        for name, src in clips.items():
            p = make_project(src)
            win.load_url(f"{url}p/{p.id}")
            r = None
            for _ in range(600):
                time.sleep(0.1)
                try:
                    if not r and win.evaluate_js("!!(window.retake && location.pathname.includes(%s))" % json.dumps(p.id)):
                        win.evaluate_js(CHECK + " 0")
                        r = True
                    elif r:
                        out = win.evaluate_js("window.__check ? JSON.stringify(window.__check) : null")
                        if out:
                            results[name] = json.loads(out)
                            break
                except Exception as e:  # noqa: BLE001 -- the page is between loads
                    print(f"{name}: {e}", flush=True)
            print(f"{name}: {json.dumps(results.get(name), ensure_ascii=False)}", flush=True)
    finally:
        win.destroy()


def main():
    work = Path(os.environ["RETAKE_HOME"])
    clips = make_clips(work)
    app.DESKTOP = True
    srv, url = app.start_server(port=0)
    results: dict = {}
    threading.Thread(target=lambda: (time.sleep(200), print("timeout", flush=True), os._exit(3)), daemon=True).start()
    win = webview.create_window("Retake check", url, width=1400, height=900)
    webview.start(func=run, args=(win, url, clips, results))
    n = results.get("normal") or {}
    ok = n.get("readyState", 0) >= 2 and n.get("jumped") and n.get("timeAfterCut", 0) >= CUT[1] and n.get("waveform")
    print("WINDOW CHECK " + ("PASSED" if ok else "FAILED"), flush=True)
    srv.shutdown()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
