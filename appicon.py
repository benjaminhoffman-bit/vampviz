"""VAMPVIZ icon, drawn in code: a Fluent rounded plate with a crisp crimson ring and hot core, like scene 1.
icon(size) gives RGBA pixels (window and taskbar icons) for any size from 16 up; write_ico(path) writes a
multi-size .ico for shortcuts and the tray, PNG-in-ICO, with zlib only. No image file to lose.
"""
import struct
import zlib
from pathlib import Path

import numpy as np

PNG_MAGIC, PNG_ROW_PREFIX = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A]) + b"\n", bytes([0])  # filter 0 per row
CRIMSON, VIOLET, BLOOD, VOID = (np.array(c, np.float32) for c in ((1.0, 0.10, 0.28), (0.45, 0.0, 1.0), (0.42, 0.0, 0.08), (0.03, 0.0, 0.05)))


def icon(size: int = 256) -> np.ndarray:
    """RGBA app icon: Fluent rounded plate, crisp crimson ring and hot core; halo from 40 px, spectrum bars from 64 px."""
    s = float(size)
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5           # pixel centres, in pixels
    p = np.stack([x, y], -1) - s / 2                                    # origin at the middle, y down
    r, ang = np.sqrt((p ** 2).sum(-1)), np.arctan2(p[..., 1], p[..., 0])
    cov = lambda d: np.clip(0.5 - d, 0, 1)[..., None]                   # signed px distance (+ outside) -> coverage, 1 px AA
    t = (p[..., 1] / s + 0.5)[..., None]                                # 0 at the top .. 1 at the bottom
    small = size < 40                                                   # tray / taskbar tier: fewer parts, pixel-snapped
    half = s / 2 - 1 if small else s * 0.42                             # plate half-size: 84 % of the canvas
    rad = round(half * 0.32) if small else half * 0.32                  # Fluent corner: ~16 % of the plate width
    q = np.abs(p) - (half - rad)
    d_plate = np.sqrt((np.maximum(q, 0) ** 2).sum(-1)) + np.minimum(np.maximum(q[..., 0], q[..., 1]), 0) - rad
    rgb = (BLOOD * 0.55 + VOID) * (1 - t) + VOID * t                    # dried blood at the top fading to void
    if small:
        R, W, C = round(s * 0.30), max(1.0, round(s * 0.13) / 2), max(1.5, round(s * 0.20) / 2)
    else:
        R, W, C = s * 0.25, s * 0.04, s * 0.085
        halo = np.exp(-((r - R) / (s * 0.11)) ** 2)[..., None] * 0.55
        rgb = rgb * (1 - halo) + VIOLET * 0.55 * halo
    if size >= 64:                                                      # 24 mirrored spectrum bars: scene 1's ring
        n, gap = 24, s * 0.02
        u = np.abs(ang) / np.pi * (n / 2); k = np.floor(u)
        h = s * (0.04 + 0.06 * (0.5 + 0.5 * np.sin(k * 2.399 + 0.7)))   # fixed heights, 4..10 % of the canvas: stays inside the plate
        a_d = (np.abs(u - k - 0.5) - 0.30) * (2 * np.pi * r / n)        # angular distance to the bar edge, in px
        bars = cov(np.maximum(a_d, np.maximum(R + W + gap - r, r - (R + W + gap + h))))
        rgb = rgb * (1 - bars) + (CRIMSON * (1 - t) + VIOLET * t) * bars
    ring = cov(np.abs(r - R) - W)
    rgb = rgb * (1 - ring) + (CRIMSON * (1 - t * 0.6) + VIOLET * t * 0.6) * ring
    core = cov(r - C)
    hot = np.array([1.0, 0.55, 0.62], np.float32) * np.clip(1 - r / max(C, 1), 0, 1)[..., None] ** 1.5
    rgb = rgb * (1 - core) + (CRIMSON * 0.9 + hot) * core
    img = np.concatenate([np.clip(rgb, 0, 1), cov(d_plate)], -1)
    return (img * 255 + 0.5).astype(np.uint8)


def write_ico(path: Path):
    """ICO: plain 32-bit bitmaps up to 128 px (what Explorer extracts) and a PNG at 256 px; zlib only, no imaging library."""
    def png(a: np.ndarray) -> bytes:
        def chunk(tag, data): return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        raw = b"".join(PNG_ROW_PREFIX + row.tobytes() for row in a)
        return (PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", a.shape[1], a.shape[0], 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    def dib(a: np.ndarray) -> bytes:  # 32-bit BGRA bottom-up DIB plus the 1-bit AND mask Explorer's extractor still wants
        h, w = a.shape[:2]
        bgra = a[::-1][..., [2, 1, 0, 3]].tobytes()
        mask = b"\0" * ((w + 31) // 32 * 4) * h
        return struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(bgra) + len(mask), 0, 0, 0, 0) + bgra + mask
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(icon(s)) if s == 256 else dib(icon(s)) for s in sizes]  # Explorer only reads PNG entries at 256 px
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset, entries = 6 + 16 * len(sizes), b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(head + entries + b"".join(images))


if __name__ == "__main__":  # self-check: asserts only, prints nothing
    import tempfile
    import time
    for s in (16, 20, 24, 32, 36, 48, 64, 72, 108, 256):
        a = icon(s)
        assert a.shape == (s, s, 4) and a.dtype == np.uint8, s
        assert a[0, 0, 3] == 0 and a[s // 2, s // 2, 3] == 255, s          # transparent corner, opaque centre
        R = round(s * (0.30 if s < 40 else 0.25))
        assert a[s // 2, s // 2 - R, 0] > 200, (s, a[s // 2, s // 2 - R])  # ring is crimson
    a = icon(256)
    yy, xx = np.mgrid[0:256, 0:256] + 0.5 - 128
    crimson = (a[..., 0] > 180) & (a[..., 1] < 60) & (np.hypot(xx, yy) > 0.42 * 256)
    assert (a[..., 3][crimson] == 255).all()                                 # bars never run past the plate
    t0 = time.perf_counter(); icon(256); assert time.perf_counter() - t0 < 0.060
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.ico"
        write_ico(p)
        b = p.read_bytes()
        assert b[:6] == b"\x00\x00\x01\x00\x07\x00"
        for i in range(7):
            off = struct.unpack_from("<I", b, 6 + 16 * i + 12)[0]
            assert b[off:off + 4] == (PNG_MAGIC[:4] if i == 6 else b"\x28\x00\x00\x00")  # DIB header below 256 px, PNG at 256
