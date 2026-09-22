"""Codex through its local app-server (JSON-RPC over stdio). Codex owns authentication."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

from resetagent import __version__
from resetagent.providers.common import window
from resetagent.timeutil import iso, now

# Redemption stays manual in this version: no Reset client may call it.
FORBIDDEN = frozenset({"account/rateLimitResetCredit/consume"})
READ_METHODS = frozenset({"initialize", "account/read", "account/rateLimits/read"})
RUN_METHODS = READ_METHODS | frozenset({"thread/start", "turn/start", "turn/interrupt"})

DESKTOP_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"


_versions: dict = {}


def version_of(path: str) -> tuple:
    if path not in _versions:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        _versions[path] = tuple(int(n) for n in match.groups()) if match else (0, 0, 0)
    return _versions[path]


def resolve_bin(cfg: dict) -> str | None:
    """An explicit codexBin wins; otherwise the newest installed Codex (newer models need newer CLIs)."""
    if cfg.get("codexBin") and Path(cfg["codexBin"]).exists():
        return cfg["codexBin"]
    candidates = [c for c in (shutil.which("codex"), DESKTOP_BIN) if c and Path(c).exists()]
    return max(candidates, key=version_of) if candidates else None


class AppServer:
    """Minimal app-server client restricted to an allowlist of methods."""

    ALLOWED = READ_METHODS

    def __init__(self, executable: str, allowed=None, cwd: str | None = None):
        if allowed is not None:
            self.ALLOWED = frozenset(allowed) - FORBIDDEN
        self.process = subprocess.Popen(
            [executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, cwd=cwd)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.sequence = 0
        self.responses: dict = {}
        self.queue: list = []

    @property
    def pid(self) -> int:
        return self.process.pid

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method: str, params=None, timeout: float = 25):
        if method in FORBIDDEN or method not in self.ALLOWED:
            raise ValueError(f"{method} is blocked for this Codex client")
        self.sequence += 1
        request_id = self.sequence
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out")
            self._read(remaining)
        response = self.responses.pop(request_id)
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(f"{method}: {error.get('message') or 'RPC error'} ({error.get('code')})")
        return response.get("result")

    def events(self, timeout: float = 0.0) -> list:
        """Notifications and server-to-client requests received so far (waits up to timeout)."""
        if not self.queue:
            self._read(timeout)
        items, self.queue = self.queue, []
        return items

    def respond(self, request_id, result=None, error=None) -> None:
        message = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        self.send(message)

    def _read(self, timeout: float) -> None:
        if not self.selector.select(max(0.0, timeout)):
            return
        chunk = os.read(self.process.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError("Codex app-server closed its stream")
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message and "method" not in message and ("result" in message or "error" in message):
                self.responses[message["id"]] = message
            else:
                self.queue.append(message)

    def close(self) -> None:
        self.selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


class ReadOnlyCodex(AppServer):
    ALLOWED = READ_METHODS


def initialize(client: AppServer, name: str = "reset") -> dict:
    result = client.call("initialize", {"clientInfo": {"name": name, "version": __version__}})
    client.send({"method": "initialized"})
    return result or {}


def label_for(minutes) -> str | None:
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "Weekly"
    return f"{minutes}-minute" if isinstance(minutes, int) else None


def normalize_codex(data: dict) -> dict:
    buckets = data.get("rateLimitsByLimitId")
    if buckets is None:
        legacy = data.get("rateLimits")
        buckets = {legacy.get("limitId") or "codex": legacy} if legacy else {}
    windows = []
    for key, bucket in buckets.items():
        for slot in ("primary", "secondary"):
            value = bucket.get(slot)
            if value is not None:
                # Classify by returned duration; "primary" does not mean any particular length.
                duration = value.get("windowDurationMins")
                label = label_for(duration)
                if label and key != "codex":
                    label = f"{label} ({key})"
                windows.append(window(f"{key}:{slot}", value.get("usedPercent"),
                                      value.get("resetsAt"), duration, label))
    # Preserve null vs [] and count-vs-details. Details may be capped upstream.
    credits = data.get("rateLimitResetCredits")
    if credits is not None:
        details = credits.get("credits")
        credits = {"availableCount": credits.get("availableCount"),
                   "credits": None if details is None else [
                       {k: row.get(k) for k in
                        ("id", "resetType", "status", "grantedAt", "expiresAt", "title")}
                       for row in details]}
    return {"windows": windows, "resetCredits": credits,
            "creditBalancesByBucket": {k: v.get("credits") for k, v in buckets.items()}}


def paid_usage(balances: dict) -> dict:
    """Could a run spend purchased Codex credits once the plan allowance is used up?"""
    possible = None
    for balance in (balances or {}).values():
        if not isinstance(balance, dict):
            continue
        spendable = bool(balance.get("unlimited")) or bool(balance.get("hasCredits"))
        possible = spendable if possible is None else (possible or spendable)
    return {"possible": possible}


def read_status(executable: str) -> dict:
    client = ReadOnlyCodex(executable)
    try:
        info = initialize(client, "reset_status")
        account = (client.call("account/read", {"refreshToken": False}) or {}).get("account") or {}
        limits = client.call("account/rateLimits/read") or {}
    finally:
        client.close()
    normalized = normalize_codex(limits)
    return {"provider": "codex", "state": "live", "source": "codex-app-server",
            "observedAt": iso(now()), "authMode": account.get("type"),
            "planType": account.get("planType"), "serverVersion": info.get("userAgent"),
            "paidUsage": paid_usage(normalized["creditBalancesByBucket"]), **normalized}
