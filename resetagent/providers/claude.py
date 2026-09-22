"""Claude Code usage through native interfaces. Reset never reads Claude credentials.

Live limits: Claude Code's stream-json control request `get_usage` (Claude Code makes the
authenticated call itself; no prompt is sent and no model runs).
Reset grants: not exposed by that request. Reset reads the response Claude Desktop cached
for its own usage page, labels it cached with its HTTP date, and never lets it authorize a run.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time

from resetagent.providers.common import percentage, window
from resetagent.timeutil import iso, now, parse_iso

DESKTOP = Path.home() / "Library/Application Support/Claude"
HTTP_CACHE = DESKTOP / "Cache/Cache_Data"
HISTORY = DESKTOP / "plan-usage-history.json"


def resolve_bin(cfg: dict) -> str | None:
    if cfg.get("claudeBin"):
        return cfg["claudeBin"]
    found = shutil.which("claude")
    if found:
        return found

    def version_key(path: Path):
        return tuple(int(p) if p.isdigit() else 0 for p in path.parts[-5].split("."))

    bundles = sorted(DESKTOP.glob("claude-code/*/claude.app/Contents/MacOS/claude"), key=version_key)
    return str(bundles[-1]) if bundles else None


def auth_status(executable: str) -> dict:
    result = subprocess.run([executable, "auth", "status"], capture_output=True, text=True, timeout=20)
    try:
        raw = json.loads(result.stdout)
    except ValueError:
        return {"loggedIn": None}
    org = raw.get("orgId")
    return {"loggedIn": raw.get("loggedIn"), "authMethod": raw.get("authMethod"),
            "subscriptionType": raw.get("subscriptionType"), "orgFingerprint": fingerprint(org)}


def fingerprint(value) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:16] if isinstance(value, str) and value else None


class ControlSession:
    """Claude Code's stream-json control channel. Only control requests are ever written."""

    def __init__(self, executable: str):
        self.cwd = tempfile.mkdtemp(prefix="reset-claude-")
        self.process = subprocess.Popen(
            [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--no-session-persistence", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.cwd, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.sequence = 0

    def request(self, subtype: str, timeout: float = 45, **fields) -> dict:
        self.sequence += 1
        request_id = f"reset-{self.sequence}"
        message = {"type": "control_request", "request_id": request_id,
                   "request": {"subtype": subtype, **fields}}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                body = reply.get("response") if reply.get("type") == "control_response" else None
                if isinstance(body, dict) and body.get("request_id") == request_id:
                    if body.get("subtype") == "error":
                        raise RuntimeError(f"{subtype}: {str(body.get('error'))[:120]}")
                    return body.get("response") or {}
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError(f"Claude Code {subtype} timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Claude Code exited before answering")
            self.buffer += chunk

    def close(self) -> None:
        self.selector.close()
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        shutil.rmtree(self.cwd, ignore_errors=True)


def read_live(executable: str) -> dict:
    session = ControlSession(executable)
    try:
        session.request("initialize")
        usage = session.request("get_usage", skip_behaviors=True)
    finally:
        session.close()
    return normalize_usage(usage, now())


LIMIT_LABELS = {"session": "5-hour", "weekly_all": "Weekly (all models)"}
LEGACY = {"five_hour": ("session", 300), "seven_day": ("weekly_all", 10080),
          "seven_day_opus": ("weekly_scoped:Opus", 10080),
          "seven_day_sonnet": ("weekly_scoped:Sonnet", 10080)}


def normalize_usage(usage: dict, observed_at: float) -> dict:
    limits = usage.get("rate_limits")
    if not usage.get("rate_limits_available") or not isinstance(limits, dict):
        return {"provider": "claude", "state": "unavailable", "source": "claude-code-get_usage",
                "error": "Claude Code reports no plan limits (not signed in to a Claude plan?)",
                "windows": []}
    windows = []
    rows = limits.get("limits")
    if isinstance(rows, list) and rows:
        for row in rows:
            kind = row.get("kind") or "unknown"
            model = ((row.get("scope") or {}).get("model") or {}).get("display_name")
            key = f"{kind}:{model}" if model else kind
            duration = {"session": 300, "weekly": 10080}.get(row.get("group"))
            label = LIMIT_LABELS.get(kind) or (f"Weekly ({model})" if model else kind)
            windows.append(window(key, row.get("percent"), parse_iso(row.get("resets_at")), duration, label))
    else:
        for name, (key, duration) in LEGACY.items():
            value = limits.get(name)
            if isinstance(value, dict):
                label = LIMIT_LABELS.get(key) or f"Weekly ({key.split(':', 1)[-1]})"
                windows.append(window(key, value.get("utilization"),
                                      parse_iso(value.get("resets_at")), duration, label))
    extra = limits.get("extra_usage") if isinstance(limits.get("extra_usage"), dict) else None
    paid = {"enabled": None if extra is None else bool(extra.get("is_enabled")),
            "disabledReason": (extra or {}).get("disabled_reason")}
    return {"provider": "claude", "state": "live", "source": "claude-code-get_usage",
            "observedAt": iso(observed_at), "subscriptionType": usage.get("subscription_type"),
            "windows": windows, "paidUsage": paid}


# --- Desktop history (from the feasibility probe) ---------------------------------------------

HISTORY_KEYS = {"fh": ("five_hour", 300), "sd": ("seven_day", 10080),
                "so": ("seven_day_opus", 10080), "sn": ("seven_day_sonnet", 10080),
                "oa": ("seven_day_oauth_apps", 10080), "cw": ("seven_day_cowork", 10080),
                "om": ("seven_day_omelette", 10080), "op": ("omelette_promotional", None)}


def normalize_history(data, now):
    if data.get("version") != 2:
        raise ValueError("Unsupported Claude Desktop history version")
    latest = {}
    for sample in data.get("samples", []):
        stamp = sample.get("t")
        if isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
            continue
        # Clock skew must not turn a future sample into a fresh authoritative reading.
        if stamp > now * 1000 or not isinstance(sample.get("u"), dict):
            continue
        org = sample.get("org")
        if org not in latest or stamp > latest[org]["t"]:
            latest[org] = sample
    observations = []
    for org, sample in latest.items():
        observations.append({
            "organizationFingerprint": fingerprint(org),
            "observedAt": iso(sample["t"] / 1000),
            "ageSeconds": round(now - sample["t"] / 1000),
            "windows": [window(name, sample["u"][short], duration=duration)
                        for short, (name, duration) in HISTORY_KEYS.items() if short in sample["u"]],
            "extraUsageUsedPercent": percentage(sample["u"].get("xu")),
        })
    return observations


# --- Reset grants ---------------------------------------------------------------------------

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
CACHE_KEY = re.compile(rb"https://claude\.ai/api/organizations/([0-9a-f-]{36})/usage\?cedar_ember=1")
HTTP_DATE = re.compile(rb"\x00date:([^\x00]{10,40})\x00", re.IGNORECASE)


def _decompress(frame: bytes, zstd: str) -> dict | None:
    try:
        result = subprocess.run([zstd, "-dcq"], input=frame, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.decode("utf-8", "ignore")
    end = text.rfind("}")
    try:
        return json.loads(text[: end + 1]) if end >= 0 else None
    except ValueError:
        return None


def cached_grants(cache_dir: Path = HTTP_CACHE, zstd: str | None = None, max_age_days: int = 45) -> dict | None:
    """Newest reset-grant block from Claude Desktop's HTTP cache, or None."""
    zstd = zstd or shutil.which("zstd")
    if not zstd or not Path(cache_dir).is_dir():
        return None
    from email.utils import parsedate_to_datetime

    best = None
    cutoff = time.time() - max_age_days * 86400
    for entry in Path(cache_dir).iterdir():
        try:
            info = entry.stat()
            if not entry.name.endswith("_0") or info.st_size > 4_000_000 or info.st_mtime < cutoff:
                continue
            with open(entry, "rb") as handle:
                head = handle.read(4096)
            key = CACHE_KEY.search(head)
            if not key:
                continue
            data = entry.read_bytes()
        except OSError:
            continue
        start = data.find(ZSTD_MAGIC)
        date = HTTP_DATE.search(data)
        if start < 0 or not date:
            continue
        try:
            stamp = parsedate_to_datetime(date.group(1).decode().strip()).timestamp()
        except (TypeError, ValueError):
            continue
        if best and stamp <= best[0]:
            continue
        body = _decompress(data[start:], zstd)
        if isinstance(body, dict) and isinstance(body.get("cedar_ember"), dict):
            best = (stamp, key.group(1).decode(), body["cedar_ember"])
    if not best:
        return None
    stamp, org, block = best
    items = []
    for grant in block.get("grants") or []:
        items.append({
            "id": fingerprint(f"{org}:{grant.get('id')}"),
            "label": grant.get("label") or "Claude usage reset",
            "resetsLeft": grant.get("resets_left"), "resetsTotal": grant.get("resets_total"),
            "startsAt": parse_iso(grant.get("starts_at")), "endsAt": parse_iso(grant.get("ends_at")),
            "clears": grant.get("clears") or [], "usableNow": grant.get("usable_now"),
            "useRequiresLimit": grant.get("use_requires_limit"), "paused": grant.get("paused"),
        })
    return {"state": "cached", "source": "claude-desktop-http-cache", "observedAt": iso(stamp),
            "orgFingerprint": fingerprint(org), "items": items,
            "weeklyResetsAt": parse_iso(block.get("weekly_resets_at"))}


def grants(conn=None, org_fingerprint: str | None = None) -> dict:
    """Cached Desktop grants for the signed-in organization, plus any entered manually."""
    found = cached_grants()
    items, sources = [], []
    note = None
    if found:
        if org_fingerprint and found["orgFingerprint"] != org_fingerprint:
            note = "Claude Desktop's cached grants belong to a different organization; ignored."
        else:
            items.extend(dict(item, source="cached") for item in found["items"])
            sources.append("cached")
    if conn is not None:
        for row in conn.execute("SELECT * FROM manual_grants WHERE provider = 'claude' ORDER BY ends_at"):
            items.append({"id": f"manual-{row['id']}", "label": row["label"], "resetsLeft": 1,
                          "endsAt": row["ends_at"], "clears": [], "source": "manual"})
            sources.append("manual")
    return {"state": "cached" if "cached" in sources else ("manual" if sources else "unavailable"),
            "observedAt": found["observedAt"] if found and "cached" in sources else None,
            "items": items, "note": note}
