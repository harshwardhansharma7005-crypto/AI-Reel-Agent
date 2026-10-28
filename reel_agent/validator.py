"""Post-render validation: a Reel is only 'ready' if every check passes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from .config import Config
from .media import MediaError, ffmpeg_bin, probe, run


@dataclass
class ValidationReport:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def fail(self, name: str, msg: str) -> None:
        self.checks[name] = False
        self.errors.append(f"{name}: {msg}")
        self.ok = False

    def passed(self, name: str) -> None:
        self.checks.setdefault(name, True)


def _max_volume_db(path: Path) -> float | None:
    r = run([ffmpeg_bin(), "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
            timeout=300)
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not m:
        return None
    return -999.0 if m.group(1) == "-inf" else float(m.group(1))


def validate_reel(cfg: Config, path: str | Path, expected_duration: float | None = None,
                  layout=None) -> ValidationReport:
    path = Path(path)
    rep = ValidationReport(True)
    if not path.exists():
        rep.fail("exists", "file missing")
        return rep
    try:
        info = probe(path)
    except MediaError as e:
        rep.fail("probe", str(e))
        return rep
    W, H = int(cfg["reel.width"]), int(cfg["reel.height"])
    (rep.passed if info["vcodec"] == "h264" else lambda n: rep.fail(n, f"video codec {info['vcodec']}"))("h264")
    (rep.passed if info["acodec"] == "aac" else lambda n: rep.fail(n, f"audio codec {info['acodec']}"))("aac")
    (rep.passed if (info["width"], info["height"]) == (W, H) else
     lambda n: rep.fail(n, f"{info['width']}x{info['height']} != {W}x{H}"))("dimensions")
    (rep.passed if info["pix_fmt"] == "yuv420p" else lambda n: rep.fail(n, f"pix_fmt {info['pix_fmt']}"))("pix_fmt")
    lo, hi = float(cfg.get("reel.min_duration", 3)), float(cfg.get("reel.max_duration", 90))
    d = info["duration"]
    (rep.passed if lo - 0.5 <= d <= min(hi + 0.5, 90) else lambda n: rep.fail(n, f"duration {d:.1f}s out of range"))("duration")
    if expected_duration is not None and abs(d - expected_duration) > 0.75:
        rep.fail("duration_match", f"{d:.2f}s vs expected {expected_duration:.2f}s")
    size_mb = info["size"] / 1e6
    (rep.passed if size_mb <= float(cfg.get("validation.max_file_mb", 100)) else
     lambda n: rep.fail(n, f"{size_mb:.1f} MB too large"))("file_size")
    if not info["has_audio"]:
        rep.fail("audio_present", "no audio stream")
    else:
        mv = _max_volume_db(path)
        if mv is None or mv < -50:
            rep.fail("audio_not_silent", f"max volume {mv} dB")
        else:
            rep.passed("audio_not_silent")
    # frame checks
    cap = cv2.VideoCapture(str(path))
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n // 2))
        ok, fr = cap.read()
    finally:
        cap.release()
    if not ok or fr is None:
        rep.fail("frame_read", "cannot decode a frame")
        return rep
    if fr.shape[0] != H or fr.shape[1] != W:
        return rep          # dimensions already reported as failed; pixel checks would be meaningless
    m = int(cfg.get("validation.side_margin_px", 24))
    if int(fr[:, :m].max()) > 60 or int(fr[:, -m:].max()) > 60:
        rep.fail("side_margins", "content intrudes into outer margins")
    else:
        rep.passed("side_margins")
    if layout is not None:
        x0, y0, x1, y1 = layout.headline_bbox
        region = fr[max(0, y0 - 10):y1 + 10, :]
        vx, vy, vw, vh = layout.video_rect
        vreg = fr[vy + 4:vy + vh - 4, vx + 4:vx + vw - 4]
    else:
        region = fr[150:640, :]
        vreg = fr[900:1100, 200:880]
    white = int(((region.min(axis=2) > 200)).sum())
    if white < int(cfg.get("validation.min_headline_pixels", 400)):
        rep.fail("headline_visible", f"only {white} bright text pixels")
    else:
        rep.passed("headline_visible")
    if float(vreg.std()) < 2.0:
        rep.fail("video_visible", "video area looks blank")
    else:
        rep.passed("video_visible")
    return rep
