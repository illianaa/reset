"""Runs as chats in the Codex and Claude desktop apps, there to read and continue any time, like chats you started.

- Codex: a run's thread is named "Reset #N: <idea>" and pinned (the app's Pinned section), so it shows in the
  app's sidebar as soon as it starts. Once the run is done it's a normal thread you can continue.
- Claude: runs switch on Claude Code's Remote Control, so they can be watched live in the Claude app and on the
  phone. When the work is done, the live chat stays open while you're in the Claude app, so it never vanishes in
  front of you. Once you leave the app (or step away, or ask), Reset hands the session to Claude Desktop the way
  `/desktop` does (claude://resume), and it becomes a normal Code-tab session. Handing over switches the app to that
  session, which is why it waits until you're not using the app.

All of it is best-effort: if an app can't show a run, the run itself goes on.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from resetagent import config
from resetagent.timeutil import now

PINNED = "Pinned"  # the Codex section the app shows as pinned threads
NAMES = {"codex": "Codex", "claude": "Claude"}
CLAUDE_DESKTOP = Path("/Applications/Claude.app")  # where Claude Code's own /desktop looks for it
CLAUDE_BUNDLE = "com.anthropic.claudefordesktop"
AWAY_SECONDS = 180  # no keyboard or mouse for this long counts as away from the Mac


def title(run, idea) -> str:
    return f"Reset #{run['id']}: {idea['title']}"


def desktop() -> bool:
    """Whether finished Claude runs can move into Claude Desktop here: a Mac with the app installed."""
    return sys.platform == "darwin" and CLAUDE_DESKTOP.exists()


def link(run) -> str | None:
    """The deep link that opens a run's chat in its desktop app."""
    if not run["thread_id"]:
        return None
    if run["engine"] == "codex":
        return f"codex://threads/{run['thread_id']}"
    if run["engine"] == "claude":
        return f"claude://resume?session={run['thread_id']}"
    return None


def _open(url: str, background: bool = False) -> str | None:
    """Open a deep link with macOS `open`. Returns an error, or None when it worked."""
    try:
        result = subprocess.run(["open", *(["-g"] if background else []), url], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if result.returncode == 0 else ((result.stderr or "").strip()[:160] or "is it installed?")


def open_run(conn, run) -> str:
    """Open a run's chat in its desktop app on this Mac. Returns what happened, in words for the user."""
    app, url = NAMES.get(run["engine"]), link(run)
    if app is None or url is None:
        return f"Run #{run['id']} has no chat to open."
    if sys.platform != "darwin":
        return "Opening chats in the desktop apps works on macOS only."
    if run["engine"] == "claude":
        # Claude Desktop takes the session over, so the run's own process must be finished first.
        if run["status"] != "done" and run["outcome"] == "completed":  # done, and its live chat is still open
            if not CLAUDE_DESKTOP.exists():
                return "Claude Desktop isn't installed on this Mac, so the run can't move there."
            conn.execute("UPDATE runs SET handoff = 'requested' WHERE id = ?", (run["id"],))
            return f"Moving run #{run['id']} into Claude Desktop now."
        if run["status"] != "done":
            live = f" Watch it live in the Claude app: {run['live_url']}." if run["live_url"] else ""
            later = " It moves to Claude Desktop once it's done." if CLAUDE_DESKTOP.exists() else ""
            return f"Run #{run['id']} is still going.{live}{later}"
        if not Path(run["workdir"]).is_dir():
            return f"Run #{run['id']}'s folder is gone, so Claude Desktop can't open it."
    error = _open(url)
    if error:
        return f"Couldn't open the {app} app ({error})."
    if run["engine"] == "claude":
        conn.execute("UPDATE runs SET handoff = 'done', handed_off_at = ? WHERE id = ?", (now(), run["id"]))
    return f"Opened run #{run['id']} in the {app} app on your Mac."


def button(run) -> list | None:
    """A Telegram button that opens the run's chat in its app on the Mac (none where it couldn't open)."""
    if run["engine"] not in NAMES or not run["thread_id"] or sys.platform != "darwin":
        return None
    if run["engine"] == "claude" and not desktop():
        return None
    return [[{"text": f"Open in {NAMES[run['engine']]}", "data": f"run:{run['id']}:open"}]]


def away_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input on this Mac, or None when it can't be read."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def claude_in_front() -> bool | None:
    """Whether Claude Desktop is the app you're using, or None when that can't be read."""
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", front], capture_output=True, text=True,
                              timeout=5).stdout if front else ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    return CLAUDE_BUNDLE in info if info else None


def left_claude() -> bool:
    """You're not using the Claude app right now: another app is in front, or you've stepped away from the Mac."""
    fake = config.env("RESET_FAKE_PRESENCE")
    if fake:
        return Path(fake).read_text().strip() != "claude"
    if sys.platform != "darwin":
        return True  # Reset only knows Claude Desktop on macOS, so nobody is in it here
    away = away_seconds()
    return (away is not None and away >= AWAY_SECONDS) or claude_in_front() is False


def sweep(conn, cfg: dict) -> int | None:
    """Hand one finished Claude run to Claude Desktop once you're not using the app, or right away when you asked.
    Returns its run id."""
    if sys.platform != "darwin" or not cfg["runs"]["showInApps"]:
        return None
    waiting = conn.execute("SELECT * FROM runs WHERE engine = 'claude' AND status = 'done' AND thread_id IS NOT NULL "
                           "AND (handoff IS NULL OR handoff = 'requested') ORDER BY handoff = 'requested' DESC, id"
                           ).fetchall()
    if not waiting:
        return None
    if not CLAUDE_DESKTOP.exists():
        conn.execute("UPDATE runs SET handoff = 'no-desktop' WHERE engine = 'claude' AND status = 'done' "
                     "AND (handoff IS NULL OR handoff = 'requested')")
        return None
    run = waiting[0]
    if run["handoff"] != "requested" and not left_claude():
        return None
    if not Path(run["workdir"]).is_dir():
        conn.execute("UPDATE runs SET handoff = 'folder-gone' WHERE id = ?", (run["id"],))
        return None
    error = _open(link(run), background=True)
    conn.execute("UPDATE runs SET handoff = ?, handed_off_at = ? WHERE id = ?",
                 ("done" if error is None else f"failed: {error}"[:200], now(), run["id"]))
    return run["id"] if error is None else None
