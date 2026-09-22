"""Read-only monitoring rules. They only queue notifications; they never start work.

Every rule uses a dedupe key, so evaluating the same snapshot twice sends nothing new.
"""
from __future__ import annotations

from resetagent import db, notify
from resetagent.status import NAMES, grant_rows, parse_iso, weekly
from resetagent.timeutil import local, now, span

REDEEM_HINT = {
    "codex": "Reset won't redeem it for you: when you're close to your limit, use it from Codex.",
    "claude": "Reset won't redeem it for you: run /limit-reset in Claude Code (or go to clau.de/reset) before it expires.",
}


def evaluate(conn, cfg: dict, snap: dict, at: float | None = None) -> list:
    at = now() if at is None else at
    rules = cfg["monitor"]
    queued = []
    for name, data in snap["providers"].items():
        queued += grant_notices(conn, name, data, at, rules)
        queued += weekly_heads_up(conn, name, data, at, rules)
        queued += availability(conn, name, data, at, rules)
    return queued


def _grants(name: str, data: dict):
    if name == "codex":
        for credit in (data.get("resetCredits") or {}).get("credits") or []:
            if credit.get("status") == "available" and isinstance(credit.get("expiresAt"), (int, float)):
                yield credit["id"], credit.get("title") or "Reset credit", credit["expiresAt"], None
    else:
        grants = data.get("grants") or {}
        seen = parse_iso(grants.get("observedAt"))
        for item in grants.get("items") or []:
            if (item.get("resetsLeft") or 0) > 0 and item.get("endsAt"):
                note = "entered manually" if item.get("source") == "manual" else (
                    f"per Claude Desktop's cached data from {local(seen)}" if seen else "cached")
                yield item["id"], item.get("label") or "Claude usage reset", item["endsAt"], note


def grant_notices(conn, name: str, data: dict, at: float, rules: dict) -> list:
    queued = []
    available = len([r for r in grant_rows(name, data) if r[1]])
    for grant_id, title, expires, note in _grants(name, data):
        for days in rules["grantNoticeDays"]:
            if not expires - days * 86400 <= at < expires:
                continue
            text = (f"Your {NAMES[name]} reset “{title}” expires in {span(expires - at)} "
                    f"({local(expires, at)})")
            text += f", {note}." if note else "."
            if available > 1:
                text += f" You have {available} unused."
            text += f" Use {NAMES[name]} freely until then. {REDEEM_HINT[name]}"
            key = f"grant:{name}:{grant_id}:{days}d"
            if notify.enqueue(conn, "grant-expiry", text, dedupe_key=key, expires_at=expires):
                queued.append(key)
    return queued


def weekly_heads_up(conn, name: str, data: dict, at: float, rules: dict) -> list:
    if data.get("state") != "live":
        return []
    horizon = rules["weeklyHeadsUpHours"] * 3600
    due = [w for w in weekly(data) if w.get("resetsAt") and w.get("remainingPercent") is not None
           and 0 < w["resetsAt"] - at <= horizon
           and w["remainingPercent"] >= rules["weeklyHeadsUpMinUnusedPercent"]]
    if not due:
        return []
    first = min(w["resetsAt"] for w in due)
    key = f"weekly:{name}:{int(first // 60)}"
    def scope(w):
        label = w.get("label") or ""
        return f" ({label[label.index('(') + 1:-1]})" if label.endswith(")") and "(" in label else ""

    unused = ", ".join(f"{w['remainingPercent']:g}% unused{scope(w)}" for w in due)
    text = (f"Your {NAMES[name]} week resets in {span(first - at)} ({local(first, at)}) with {unused}. "
            f"Reply “ideas” to see your list or “run <number>” to have me ask before starting one.")
    # Relative times go stale: drop the heads-up if it can't be delivered within the hour.
    return [key] if notify.enqueue(conn, "weekly", text, dedupe_key=key, expires_at=min(first, at + 3600)) else []


def availability(conn, name: str, data: dict, at: float, rules: dict) -> list:
    marker = f"unavailable-since:{name}"
    if data.get("state") == "live":
        db.kv_delete(conn, marker)
        return []
    since = db.kv_get(conn, marker)
    if since is None:
        db.kv_set(conn, marker, at)
        return []
    if at - since < rules["unavailableNoticeHours"] * 3600:
        return []
    key = f"unavailable:{name}:{int(since)}"
    text = (f"Reset hasn't been able to read your {NAMES[name]} usage since {local(since, at)} "
            f"({data.get('error') or 'unknown error'}). Check that you're still signed in.")
    return [key] if notify.enqueue(conn, "unavailable", text, dedupe_key=key) else []
