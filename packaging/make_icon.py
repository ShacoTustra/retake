"""Draw the app icon (1024x1024 PNG) with numpy only: a play triangle cut by a diagonal gap.

  python packaging/make_icon.py packaging/icon.png
"""
import struct
import sys
import zlib

import numpy as np

S = 1024
SS = 2  # supersampling for smooth edges
N = S * SS


def png(path: str, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


y, x = (np.mgrid[0:N, 0:N] + 0.5) / N  # 0..1

# macOS icon grid: the shape fills ~80% of the canvas, corner radius ~22% of the shape
m, r = 0.1, 0.18
qx = np.clip(np.abs(x - 0.5) - (0.5 - m - r), 0, None)
qy = np.clip(np.abs(y - 0.5) - (0.5 - m - r), 0, None)
shape = (np.hypot(qx, qy) <= r)

top, bottom = np.array([64, 140, 255]), np.array([26, 70, 205])
bg = top[None, None, :] * (1 - y[..., None]) + bottom[None, None, :] * y[..., None]

# play triangle
cx, cy, size = 0.53, 0.5, 0.36
ax, ay = cx - size * 0.45, cy - size * 0.55
bx, by = cx - size * 0.45, cy + size * 0.55
tx, ty = cx + size * 0.65, cy


def side(px, py, qx_, qy_):
    return (x - px) * (qy_ - py) - (y - py) * (qx_ - px)


s1, s2, s3 = side(ax, ay, bx, by), side(bx, by, tx, ty), side(tx, ty, ax, ay)
tri = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
# the cut: a diagonal gap through the triangle
d = (x - 0.5) * 0.8 + (y - 0.5) * 0.6
gap = np.abs(d - 0.02) < 0.028
white = tri & ~gap

img = np.zeros((N, N, 4))
img[..., :3] = bg
img[white, :3] = [255, 255, 255]
img[..., 3] = shape * 255
# downsample (box filter) for anti-aliasing
img = img.reshape(S, SS, S, SS, 4).mean(axis=(1, 3))
png(sys.argv[1] if len(sys.argv) > 1 else "icon.png", np.clip(img, 0, 255).astype(np.uint8))
