"""Time helpers. Stored timestamps are Unix seconds; display uses the local zone."""
from __future__ import annotations

from datetime import datetime, timezone
import time


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def parse_iso(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def span(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def local(ts: float, reference: float | None = None) -> str:
    # Round to the nearest minute: providers report resets like 02:59:59.67.
    moment = datetime.fromtimestamp(ts + 30)
    ref = datetime.fromtimestamp(time.time() if reference is None else reference)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    days = (moment.date() - ref.date()).days
    if days == 0:
        return clock
    if 0 < days < 7:
        return f"{moment.strftime('%a')} {clock}"
    return f"{moment.strftime('%a %b')} {moment.day}, {clock}"
