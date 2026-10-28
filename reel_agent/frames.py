"""Frame extraction (OpenCV), perceptual hashing, base64 encoding."""
from __future__ import annotations

import base64
from pathlib import Path

import cv2
import numpy as np


def extract_frames(path: str | Path, count: int = 6, max_width: int = 768,
                   start_frac: float = 0.05, end_frac: float = 0.95) -> list[np.ndarray]:
    """Return up to `count` BGR frames evenly spaced through the video."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frames: list[np.ndarray] = []
        if total <= 0:
            return []
        count = max(1, count)
        idxs = [int(total * (start_frac + (end_frac - start_frac) * i / max(1, count - 1))) for i in range(count)] \
            if count > 1 else [total // 2]
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total - 1))
            ok, fr = cap.read()
            if not ok or fr is None:
                continue
            h, w = fr.shape[:2]
            if w > max_width:
                fr = cv2.resize(fr, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
            frames.append(fr)
        return frames
    finally:
        cap.release()


def extract_representative_frames(path: str | Path, count: int = 6, max_width: int = 768,
                                  pool_factor: int = 3) -> list[np.ndarray]:
    """Return up to `count` visually distinct frames, in time order.

    Samples a larger pool, drops near-black/blank frames, then greedily keeps the frames that differ most from
    those already chosen (farthest-point selection on a perceptual hash) so the AI sees different scenes
    instead of six near-identical shots.
    """
    count = max(1, count)
    pool = extract_frames(path, count * max(1, pool_factor), max_width)
    pool = [f for f in pool if float(f.mean()) > 8 and float(f.std()) > 4] or pool
    if len(pool) <= count:
        return pool
    hashes = [dhash(f) for f in pool]
    chosen = [0]
    while len(chosen) < count:
        best, best_d = None, -1
        for i in range(len(pool)):
            if i in chosen:
                continue
            d = min(bin(hashes[i] ^ hashes[j]).count("1") for j in chosen)
            if d > best_d:
                best, best_d = i, d
        chosen.append(best)
    return [pool[i] for i in sorted(chosen)]


def frame_to_jpeg_b64(frame: np.ndarray, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("JPEG encode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def dhash(frame: np.ndarray, size: int = 8) -> int:
    """64-bit difference hash."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(b)
    return bits


def video_phash(path: str | Path) -> str:
    """Perceptual signature: 3 frames x 64-bit dHash, plus mean colour of each frame.

    Format: '<48 hex chars of hash>:<18 hex chars of mean B,G,R per frame>'. '' if unreadable.
    """
    frames = extract_frames(path, count=3, max_width=320, start_frac=0.2, end_frac=0.8)
    if len(frames) < 3:
        return ""
    sig = 0
    colors = bytearray()
    for fr in frames:
        sig = (sig << 64) | dhash(fr)
        colors.extend(int(round(float(c))) for c in fr.reshape(-1, 3).mean(axis=0))
    return f"{sig:048x}:{colors.hex()}"


def hamming_hex(a: str, b: str) -> int:
    if not a or not b:
        return 10**6
    return bin(int(a.split(":")[0], 16) ^ int(b.split(":")[0], 16)).count("1")


def color_distance(a: str, b: str) -> float:
    """Mean absolute difference (0-255) between the colour parts of two signatures."""
    try:
        ca, cb = bytes.fromhex(a.split(":")[1]), bytes.fromhex(b.split(":")[1])
    except (IndexError, ValueError):
        return 255.0
    return sum(abs(x - y) for x, y in zip(ca, cb)) / max(1, len(ca))


def is_near_duplicate(a: str, b: str, max_hamming: int, max_color: float) -> bool:
    return hamming_hex(a, b) <= max_hamming and color_distance(a, b) <= max_color
