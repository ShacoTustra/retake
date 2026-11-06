"""Apply reviewed edits: render a cut MP4 and/or write an FCPXML timeline.

  render.py edits.json --mp4 out.mp4       # finished file -> import into iMovie
  render.py edits.json --fcpxml out.fcpxml # timeline for Final Cut Pro / DaVinci Resolve
"""
import argparse
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def _rate(value: str | None) -> Fraction | None:
    try:
        f = Fraction(value or "")
    except (ValueError, ZeroDivisionError):  # ffprobe writes 0/0 when it does not know
        return None
    return f if f > 0 else None


def working_rate(r_frame_rate: str | None, avg_frame_rate: str | None) -> tuple[Fraction, bool]:
    """The frame rate to cut at, and whether the source has a variable one.

    r_frame_rate is ffprobe's guess at the grid of the timestamps, not a promise about the frames:
    a MediaRecorder file says 30000/1 with about 30 real frames a second. It only means a frame rate
    when the average agrees. Variable-rate sources are cut at a standard constant rate."""
    r, avg = _rate(r_frame_rate), _rate(avg_frame_rate)
    if r and r <= 240 and (avg is None or abs(r - avg) / r < Fraction(1, 100)):
        return r, False
    return (Fraction(60) if avg and avg > 40 else Fraction(30)), True


def probe(src: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
         "-of", "json", str(src)],
        check=True, capture_output=True, text=True)
    j = json.loads(r.stdout)
    video = next((st for st in j["streams"] if st["codec_type"] == "video"), None)
    if not video:
        raise ValueError(f"в файле нет видео: {src.name}")
    audio = next((st for st in j["streams"] if st["codec_type"] == "audio"), {})
    fps, vfr = working_rate(video.get("r_frame_rate"), video.get("avg_frame_rate"))
    return {"w": video["width"], "h": video["height"], "fps": fps, "vfr": vfr,
            "duration": float(j["format"]["duration"]),
            "video_duration": float(video["duration"]) if video.get("duration") else None,
            "audio_duration": float(audio["duration"]) if audio.get("duration") else None}


def keep_segments(edits: list[dict], t0: float, t1: float, fps: Fraction) -> list[tuple[int, int]]:
    """Return kept ranges as (start_frame, end_frame), cuts snapped to the frame grid.

    Snapping both audio and video cuts to whole frames keeps A/V in sync no matter
    how many cuts there are.
    """
    f = lambda t: round(t * fps)
    cuts = sorted((f(e["start"]), f(e["end"])) for e in edits if e.get("on"))
    merged = []
    for a, b in cuts:
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    keep, pos = [], f(t0)
    for a, b in merged:
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    if f(t1) > pos:
        keep.append((pos, f(t1)))
    return [(a, b) for a, b in keep if b - a >= 2]  # drop 1-frame slivers


def default_encoder() -> str:
    """Hardware encoder when there is one: VideoToolbox on a Mac, NVENC with an NVIDIA card."""
    if sys.platform == "darwin":
        return "videotoolbox"
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        if "h264_nvenc" in r.stdout:
            return "nvenc"
    return "x264"


def render_mp4(src: Path, keep, fps: Fraction, out: Path, encoder: str, t_end: float, progress=None) -> str:
    """Render the kept segments; returns the encoder used.

    A hardware encoder that cannot start (a virtual machine, an unusual Mac) falls back to libx264.
    progress(fraction, message) is called while ffmpeg runs; raising from it cancels the render."""
    if not keep:
        raise ValueError("вырезано всё видео: собирать нечего")
    try:
        _render_mp4(src, keep, fps, out, encoder, t_end, progress)
        return encoder
    except RuntimeError as e:
        if encoder == "x264":
            raise
        print(f"{encoder} failed, falling back to x264: {e}", file=sys.stderr)
        _render_mp4(src, keep, fps, out, "x264", t_end, progress)
        return "x264"


def _render_mp4(src: Path, keep, fps: Fraction, out: Path, encoder: str, t_end: float, progress=None):
    sel = "+".join(f"between(n,{a},{b - 1})" for a, b in keep)
    # half-open [a, b): an inclusive upper bound adds ~1 ms per splice, which drifts over 100+ cuts
    asel = "+".join(f"gte(t,{float(a / fps) - 0.0005:.4f})*lt(t,{float(b / fps) - 0.0005:.4f})" for a, b in keep)
    # tiny fades at each splice would be nicer; boundaries are already snapped to silence
    rate = f"{fps.numerator}/{fps.denominator}"
    fc = (
        # fps first: frame numbers in `sel` are time * rate, which holds only for a constant rate. Real
        # recordings skip frames (always with MediaRecorder); this fills them in and changes nothing else.
        f"[0:v]fps=fps={rate}:start_time=0,select='{sel}',setpts=N/({rate})/TB[v];"
        # aselect works on whole audio frames (~21 ms for AAC); re-chunk to 1 ms so every
        # splice is sample-accurate and A/V stays in sync over hundreds of cuts
        f"[0:a]aresample=48000,asetnsamples=n=48:p=0,aselect='{asel}',asetpts=N/SR/TB[a]"
    )
    venc = {
        "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-tune", "stillimage"],
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"],
        "videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "65"],  # Apple Silicon hardware encoder
    }[encoder]
    cmd = ["ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1", "-y", "-to", f"{t_end + 1:.3f}",
           # the graph goes inline: -filter_complex_script is gone in ffmpeg 8+, and its replacement
           # (-/filter_complex) does not exist before 7; even 500 cuts are well under the argv limit
           "-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
           *venc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(out)]
    total = float(sum(b - a for a, b in keep) / fps)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us=") and progress:
                    try:
                        t = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    progress(min(1.0, t / total) if total else None, "кодирование")
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg: {err.strip()[-500:]}")
            _check_lengths(src, out, total)
        except BaseException:
            proc.kill()
            proc.wait()
            out.unlink(missing_ok=True)
            raise


def _check_lengths(src: Path, out: Path, expected: float) -> None:
    """ffmpeg can succeed and still write a broken film: never hand one over as finished."""
    made, source = probe(out), probe(src)
    v, a = made["video_duration"], made["audio_duration"]
    # a recording's tracks can differ a little in length, and the result inherits that
    slack = 1.0 + abs((source["video_duration"] or 0) - (source["audio_duration"] or 0)) \
        if source["video_duration"] and source["audio_duration"] else 1.0
    if v is None or a is None or abs(v - expected) > slack or abs(a - expected) > slack:
        raise RuntimeError(f"готовый файл неполный: видео {v or 0:.1f} с, звук {a or 0:.1f} с, "
                           f"а должно быть {expected:.1f} с. Исходник не изменён.")


def write_fcpxml(src: Path, keep, info: dict, out: Path):
    if not keep:
        raise ValueError("вырезано всё видео: собирать нечего")
    fps = info["fps"]
    fd = Fraction(1) / fps  # frame duration
    r = lambda frames: f"{(frames * fd).numerator}/{(frames * fd).denominator}s" if frames else "0s"
    total_src = round(Fraction(info["duration"]) * fps)
    rate_name = str(fps.numerator) if fps.denominator == 1 else str(round(float(fps) * 100))  # 2997
    clips, offset = [], 0
    for a, b in keep:
        clips.append(
            f'        <asset-clip ref="r2" offset="{r(offset)}" start="{r(a)}" duration="{r(b - a)}" '
            f'name="{src.stem}" format="r1" tcFormat="NDF" audioRole="dialogue"/>'
        )
        offset += b - a
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat{info["h"]}p{rate_name}" frameDuration="{fd.numerator}/{fd.denominator}s" width="{info["w"]}" height="{info["h"]}"/>
    <asset id="r2" name="{src.stem}" start="0s" duration="{r(total_src)}" hasVideo="1" hasAudio="1" format="r1" audioSources="1" audioChannels="2">
      <media-rep kind="original-media" src="{src.resolve().as_uri()}"/>
    </asset>
  </resources>
  <library>
    <event name="Retake">
      <project name="{src.stem} (cut)">
        <sequence format="r1" duration="{r(offset)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
    out.write_text(xml)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edits", type=Path, help="edits JSON (from detect.py or saved from review page)")
    ap.add_argument("--mp4", type=Path)
    ap.add_argument("--fcpxml", type=Path)
    ap.add_argument("--encoder", choices=["x264", "nvenc", "videotoolbox"], default=default_encoder())
    args = ap.parse_args()

    d = json.loads(args.edits.read_text())
    src = Path(d["source"])
    info = probe(src)
    t0, t1 = d["range"]
    keep = keep_segments(d["edits"], t0, t1, info["fps"])
    kept = sum(b - a for a, b in keep) / info["fps"]
    print(f"{len(keep)} segments, {t1 - t0:.0f}s -> {float(kept):.0f}s", file=sys.stderr)

    if args.fcpxml:
        write_fcpxml(src, keep, info, args.fcpxml)
        print(args.fcpxml)
    if args.mp4:
        render_mp4(src, keep, info["fps"], args.mp4, args.encoder, t1)
        print(args.mp4)


if __name__ == "__main__":
    main()
