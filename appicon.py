"""VAMPVIZ icon, drawn in code: a crimson ring with a violet halo on black, like scene 1. No image file to lose.
icon(size) gives RGBA pixels (window and taskbar icons); write_ico(path) writes a multi-size .ico for shortcuts
and the tray, PNG-in-ICO, with zlib only.
"""
import struct
import zlib
from pathlib import Path

import numpy as np

PNG_MAGIC, PNG_ROW_PREFIX = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A]) + b"\n", bytes([0])  # filter 0 per row


def icon(size: int = 256) -> np.ndarray:
    """The app icon as RGBA: a crimson ring with a violet halo on black, like scene 1. Drawn, so no image file."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    uv = (np.stack([x, y], -1) + 0.5) / size - 0.5
    r = np.sqrt((uv ** 2).sum(-1))
    ring = np.exp(-((r - 0.30) / 0.045) ** 2)
    halo = np.exp(-((r - 0.40) / 0.06) ** 2) * 0.7
    core = np.clip(1 - r / 0.22, 0, 1) ** 2
    rgb = (np.array([1.0, 0.10, 0.28]) * (ring + core * 0.9)[..., None]
           + np.array([0.45, 0.0, 1.0]) * halo[..., None] + np.array([0.08, 0.0, 0.03]) * core[..., None])
    alpha = np.clip(r < 0.48, 0, 1) * np.clip((0.48 - r) * size, 0, 1)
    img = np.concatenate([np.clip(rgb, 0, 1), alpha[..., None]], -1)
    return (img * 255 + 0.5).astype(np.uint8)


def write_ico(path: Path):
    """ICO holding PNGs (Vista and later): one chunk helper, zlib, no imaging library."""
    def png(a: np.ndarray) -> bytes:
        def chunk(tag, data): return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        raw = b"".join(PNG_ROW_PREFIX + row.tobytes() for row in a)
        return (PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", a.shape[1], a.shape[0], 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(icon(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset, entries = 6 + 16 * len(sizes), b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(head + entries + b"".join(images))
