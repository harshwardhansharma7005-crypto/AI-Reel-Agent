"""Slot logic: map 'now' to IST publishing slots."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config


def _tz(cfg: Config):
    try:
        return ZoneInfo(cfg.get("schedule.timezone", "Asia/Kolkata"))
    except Exception:  # tzdata missing on some Windows installs
        from datetime import timezone
        return timezone(timedelta(hours=5, minutes=30))


def resolve_slot(cfg: Config, now: datetime | None = None, requested: str = "auto") -> tuple[str, str]:
    """Return (slot_name, slot_key). slot_key is unique per scheduled slot per day.

    requested: 'auto' | a configured slot name | 'manual'
    """
    tz = _tz(cfg)
    now = (now or datetime.now(tz)).astimezone(tz)
    slots: dict[str, str] = cfg.get("schedule.slots", {})
    date = now.strftime("%Y-%m-%d")
    if requested in slots:
        return requested, f"{date}-{requested}"
    if requested == "manual":
        return "manual", f"{date}-manual-{now.strftime('%H%M%S')}"
    tol = float(cfg.get("schedule.slot_tolerance_hours", 3)) * 3600
    best, best_diff = None, None
    for name, hhmm in slots.items():
        h, m = map(int, hhmm.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff = abs((now - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = name, diff
    if best is not None and best_diff is not None and best_diff <= tol:
        return best, f"{date}-{best}"
    return "manual", f"{date}-manual-{now.strftime('%H%M%S')}"
