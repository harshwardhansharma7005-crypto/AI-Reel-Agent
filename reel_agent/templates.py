"""Layout/typography templates with controlled variation."""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import Config


@dataclass(frozen=True)
class Template:
    name: str
    align: str                  # center | left
    font_max: int
    font_min: int
    max_lines: int
    uppercase: bool
    video_w: int
    border: str                 # glow | solid | brackets | thin
    accent_bar: bool
    username_pos: str           # below | bottom | bottom_left
    fonts: tuple[str, ...] = field(default_factory=tuple)


_BOLD_FALLBACK = ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf")

TEMPLATES: dict[str, Template] = {
    "classic": Template("classic", "center", 96, 56, 4, False, 1000, "glow", False, "below",
                        ("Montserrat-ExtraBold.ttf", "Poppins-ExtraBold.ttf", "ariblk.ttf", "segoeuib.ttf")
                        + _BOLD_FALLBACK),
    "left_bold": Template("left_bold", "left", 88, 52, 4, True, 960, "solid", True, "bottom",
                          ("Anton-Regular.ttf", "BebasNeue-Regular.ttf", "impact.ttf", "bahnschrift.ttf")
                          + _BOLD_FALLBACK),
    "bracket": Template("bracket", "center", 84, 50, 4, True, 980, "brackets", False, "below",
                        ("Poppins-Bold.ttf", "segoeuib.ttf", "arialbd.ttf") + _BOLD_FALLBACK),
    "minimal": Template("minimal", "left", 92, 56, 4, False, 1000, "thin", False, "bottom",
                        ("Inter-Bold.ttf", "segoeuib.ttf", "arialbd.ttf") + _BOLD_FALLBACK),
}


def get_template(name: str) -> Template:
    if name not in TEMPLATES:
        raise KeyError(f"Unknown template '{name}'. Available: {sorted(TEMPLATES)}")
    return TEMPLATES[name]


def choose_template(cfg: Config, recent: list[str], rng: random.Random | None = None) -> Template:
    """Random among enabled templates, never the same as the previous reel when possible."""
    rng = rng or random.Random()
    enabled = [n for n in cfg.get("reel.templates", list(TEMPLATES)) if n in TEMPLATES] or list(TEMPLATES)
    last = recent[0] if recent else None
    pool = [n for n in enabled if n != last] or enabled
    # prefer templates used least in the recent window
    counts = {n: recent.count(n) for n in pool}
    least = min(counts.values())
    pool = [n for n in pool if counts[n] == least]
    return TEMPLATES[rng.choice(pool)]
