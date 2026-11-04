"""QA: measure loudness of the source audio at every splice point of an edit list.

A splice inside speech (> -40 dBFS) means a word may be clipped.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from render import keep_segments, probe  # noqa: E402

d = json.load(open(sys.argv[1]))
info = probe(Path(d["source"]))
fps = float(info["fps"])
t0, t1 = d["range"]
keep = keep_segments(d["edits"], t0, t1, info["fps"])
raw = subprocess.run(["ffmpeg", "-v", "error", "-to", str(t1 + 1), "-i", d["source"], "-ac", "1", "-ar", "48000",
                      "-f", "s16le", "-"], capture_output=True, check=True).stdout
a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def lvl(t):
    seg = a[int((t - 0.03) * 48000): int((t + 0.03) * 48000)]
    return 20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9)


bad = []
pts = []
for (s0, e0), (s1, e1) in zip(keep, keep[1:]):
    out_t, in_t = e0 / fps, s1 / fps
    lo, li = lvl(out_t), lvl(in_t)
    pts += [lo, li]
    if max(lo, li) > -40:
        bad.append((out_t, in_t, lo, li))
print(f"{len(keep) - 1} splices, median level {np.median(pts):.1f} dBFS, in speech (> -40 dBFS): {len(bad)}")
for out_t, in_t, lo, li in bad:
    print(f"  {out_t:8.2f} -> {in_t:8.2f}   out {lo:6.1f} dB, in {li:6.1f} dB")
