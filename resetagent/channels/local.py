"""Last-resort delivery: a log file plus a macOS notification banner."""
from __future__ import annotations

import subprocess
import sys

from resetagent import config
from resetagent.timeutil import iso, now

BANNER = """on run argv
  display notification (item 1 of argv) with title "Reset"
end run
"""


class Local:
    name = "local"

    def __init__(self, cfg: dict, run=subprocess.run):
        self.settings = cfg["channels"]["local"]
        self._run = run

    def configured(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def send(self, text: str, buttons=None) -> None:
        logs = config.home() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "notifications.log", "a") as handle:
            handle.write(f"{iso(now())} {text}\n\n")
        if sys.platform == "darwin" and not config.env("RESET_NO_BANNERS"):
            try:
                self._run(["osascript", "-", text[:240]], input=BANNER, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def poll(self, conn, at=None) -> int:
        return 0
