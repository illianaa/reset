"""Normalized usage windows shared by providers. Unknown stays None, never zero."""
from __future__ import annotations

import math


def percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def window(key, used, reset=None, duration=None, label=None):
    used = percentage(used)
    result = {"id": key, "usedPercent": used,
              "remainingPercent": None if used is None else 100 - used,
              "resetsAt": reset, "windowDurationMins": duration}
    if label:
        result["label"] = label
    return result


def describe(exc: BaseException) -> str:
    """A short, log-safe description (never provider stdout or credentials)."""
    message = " ".join(str(exc).split())[:160]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def unavailable(provider: str, reason: str) -> dict:
    return {"provider": provider, "state": "unavailable", "error": reason, "windows": []}
