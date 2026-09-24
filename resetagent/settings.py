"""Settings the user tunes by chatting with Reset's AI: a fixed list, each value checked before it's saved.

Every change the AI makes is also announced by Reset itself, in a message the AI doesn't write, with an Undo
button. So a setting can't change without the user seeing it, even if text hidden in a run's output talked the AI
into it. Approving runs, redeeming resets and the chat connection are not settings: the AI can't touch them.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import secrets

from resetagent import config, db, models, notify, workspace
from resetagent.timeutil import now


class SettingError(ValueError):
    """The setting or value isn't allowed; the message says what is."""


@dataclass(frozen=True)
class Setting:
    path: tuple        # where it lives in config.json
    label: str         # how messages name it
    about: str
    kind: str          # number, switch, choice, days or folder
    low: int = 0
    high: int = 0
    choices: tuple = ()
    unit: str = ""


SETTINGS = {
    "floor": Setting(("runs", "keepPercent"), "the floor", "The share of every usage limit runs leave alone: none "
                     "starts below it, and running ones stop if a subscription drops under it.", "number", 0, 100,
                     unit="%"),
    "runs_at_once": Setting(("runs", "maxRunsPerEngine"), "runs at once", "How many runs can work at the same time "
                            "on each subscription.", "number", 1, 10),
    "run_budget_tokens": Setting(("runs", "budgetTokens"), "the token budget per run", "Tokens a run may use when "
                                 "its request doesn't say.", "number", 10_000, 2_000_000, unit=" tokens"),
    "run_minutes": Setting(("runs", "maxMinutes"), "the time limit per run", "Minutes a run may take when its "
                           "request doesn't say.", "number", 5, 240, unit=" min"),
    "effort": Setting(("runs", "effort"), "the default effort", "Effort runs use when their request doesn't say.",
                      "choice", choices=models.EFFORTS),
    "access": Setting(("runs", "access"), "run access", "full: runs can run commands, install packages and use the "
                      "network without asking. sandboxed: they only change files in their own folder.", "choice",
                      choices=("full", "sandboxed")),
    "show_in_apps": Setting(("runs", "showInApps"), "showing runs in the apps", "Pin Codex runs, and show Claude "
                            "runs live in the Claude app before they move to Claude Desktop.", "switch"),
    "projects_folder": Setting(("runs", "projectsRoot"), "the projects folder", "Where the user's projects live and "
                               "new ones go. \"auto\" finds ~/Projects, ~/code and the like.", "folder"),
    "usage_check_minutes": Setting(("monitor", "statusMinutes"), "how often usage is read", "Minutes between "
                                   "usage readings (and status updates).", "number", 5, 240, unit=" min"),
    "reset_reminder_days": Setting(("monitor", "grantNoticeDays"), "one-time reset reminders", "Days before a "
                                   "one-time reset expires to send a reminder, like \"7, 1\".", "days"),
    "weekly_heads_up_hours": Setting(("monitor", "weeklyHeadsUpHours"), "the weekly heads-up", "Hours before a "
                                     "weekly limit resets to say how much is still unused (0 turns it off).",
                                     "number", 0, 168, unit=" h"),
    "weekly_heads_up_unused_percent": Setting(("monitor", "weeklyHeadsUpMinUnusedPercent"),
                                              "the weekly heads-up threshold", "Only give the weekly heads-up when "
                                              "at least this share of the week is unused.", "number", 0, 100,
                                              unit="%"),
}


def _parent(cfg: dict, setting: Setting) -> dict:
    """The part of config.json that holds the setting."""
    node = cfg
    for key in setting.path[:-1]:
        node = node[key]
    return node


def _get(cfg: dict, setting: Setting):
    return _parent(cfg, setting)[setting.path[-1]]


def show(name: str, value) -> str:
    setting = SETTINGS[name]
    if setting.kind == "switch":
        return "on" if value else "off"
    if setting.kind == "days":
        if not value:
            return "off"
        return ", ".join(f"{d}" for d in value) + (" day before" if value == [1] else " days before")
    if setting.kind == "folder":
        return "found automatically" if not value else workspace.short(value)
    if setting.kind == "number" and setting.unit == " tokens":
        return f"{value / 1000:g}k tokens"
    return f"{value}{setting.unit}"


def parse(name: str, value):
    """The checked value to save, or SettingError saying what's allowed."""
    setting = SETTINGS.get(name)
    if setting is None:
        raise SettingError(f"There's no setting “{name}”. Settings: {', '.join(SETTINGS)}.")
    text = str(value).strip().lower()
    if setting.kind == "number":
        digits = text.rstrip("%").replace(",", "").replace("_", "")
        scale = 1000 if digits.endswith("k") else 1
        try:
            number = float(digits.rstrip("k")) * scale
        except ValueError:
            number = math.nan
        # Nothing is rounded: "4.9" for the floor is refused rather than quietly made 4.
        if not math.isfinite(number) or number != int(number) or not setting.low <= number <= setting.high:
            raise SettingError(f"{name} takes a whole number from {setting.low} to {setting.high}.")
        return int(number)
    if setting.kind == "switch":
        if text in ("on", "true", "yes", "1"):
            return True
        if text in ("off", "false", "no", "0"):
            return False
        raise SettingError(f"{name} is on or off.")
    if setting.kind == "choice":
        if text not in setting.choices:
            raise SettingError(f"{name} is one of: {', '.join(setting.choices)}.")
        return text
    if setting.kind == "days":
        if text in ("", "off", "none", "[]"):
            return []
        try:  # "7, 1", "7 1" or "[7, 1]"; a space never joins two numbers into one
            days = sorted({int(part) for part in re.split(r"[\s,]+", text.strip("[]")) if part}, reverse=True)
        except ValueError:
            days = None
        if not days or any(d < 1 or d > 60 for d in days):
            raise SettingError(f"{name} is a list of days from 1 to 60, like “7, 1”, or “off”.")
        return days
    # a folder
    if text in ("", "auto", "none"):
        return None
    home = Path.home().resolve()
    path = Path(str(value).strip()).expanduser()
    path = (path if path.is_absolute() else home / path).resolve()  # "code" means ~/code
    inside = home in path.parents
    if not path.is_dir() or not inside or path.relative_to(home).parts[0].startswith(".") \
            or path.relative_to(home).parts[0] == "Library":
        raise SettingError(f"{name} must be an existing folder in your home folder (not a hidden one or Library).")
    return str(path)


def listing(cfg: dict) -> dict:
    return {name: {"value": show(name, _get(cfg, s)), "about": s.about,
                   "allowed": (f"{s.low} to {s.high}" if s.kind == "number" else
                               " or ".join(s.choices) if s.kind == "choice" else
                               {"switch": "on or off", "days": "days from 1 to 60, like “7, 1”, or “off”",
                                "folder": "a folder in your home folder, or “auto”"}[s.kind])}
            for name, s in SETTINGS.items()}


def change(conn, name: str, value, by_ai: bool) -> tuple:
    """Check and save a setting. Returns (old, new). A change the AI made is announced, with an Undo button.

    The announcement is queued in the same step as the save: if it can't be queued, nothing is saved.
    """
    new = parse(name, value)
    setting = SETTINGS[name]
    with db.transaction(conn), config.editing() as cfg:
        old = _get(cfg, setting)
        _parent(cfg, setting)[setting.path[-1]] = new
        if by_ai and old != new:
            nonce = secrets.token_hex(4)
            db.kv_set(conn, f"setting-undo:{nonce}", {"name": name, "old": old, "new": new, "at": now()})
            notify.enqueue(conn, "setting", f"Reset's AI changed {setting.label}: {show(name, old)} → "
                           f"{show(name, new)}. To put it back, tap Undo or send “undo {nonce}”.",
                           dedupe_key=f"setting:{nonce}", buttons=[[{"text": "Undo", "data": f"undo:{nonce}"}]])
    return old, new


def undo(conn, nonce: str) -> str:
    """Put back a setting the AI changed (the user's Undo button), unless it was changed again since."""
    record = db.kv_get(conn, f"setting-undo:{nonce}")
    setting = SETTINGS.get(record["name"]) if record else None
    if setting is None:
        return "That change can't be undone any more."
    with config.editing() as cfg:  # checked and put back under the same lock, so no change slips in between
        current = _get(cfg, setting)
        if current == record["new"]:
            _parent(cfg, setting)[setting.path[-1]] = record["old"]
    if current != record["new"]:
        return f"{setting.label[0].upper() + setting.label[1:]} was changed again since, so I left it at " \
               f"{show(record['name'], current)}."
    db.kv_delete(conn, f"setting-undo:{nonce}")
    return f"Undone: {setting.label} is back to {show(record['name'], record['old'])}."
