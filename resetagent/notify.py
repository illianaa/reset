"""Durable notification outbox: deduplicated, retried with backoff, delivered by channel fallback."""
from __future__ import annotations

import json

from resetagent import channels
from resetagent.channels.base import ChannelError
from resetagent.timeutil import now

MAX_ATTEMPTS = 8
VERBATIM = {"question"}  # messages quoting what a run wrote (a command it wants to run) are sent exactly as is


def enqueue(conn, kind: str, text: str, dedupe_key: str | None = None, channel: str | None = None,
            expires_at: float | None = None, buttons=None) -> int | None:
    """Queue a message. A repeated dedupe_key is ignored, so rules can re-fire safely.

    buttons: rows of {"text", "data"}; channels without buttons rely on the text alone.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO notifications(dedupe_key, kind, text, channel, created_at, expires_at, buttons) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (dedupe_key, kind, text, channel, now(), expires_at, json.dumps(buttons) if buttons else None))
    return cursor.lastrowid if cursor.rowcount else None


def flush(conn, cfg: dict, built: dict | None = None, at: float | None = None) -> list:
    at = now() if at is None else at
    built = built or channels.build(cfg)
    results = []
    rows = conn.execute("SELECT * FROM notifications WHERE sent_at IS NULL AND attempts < ? ORDER BY id",
                        (MAX_ATTEMPTS,)).fetchall()
    for row in rows:
        if row["expires_at"] and at > row["expires_at"]:
            conn.execute("UPDATE notifications SET attempts = ?, last_error = 'expired before delivery' "
                         "WHERE id = ?", (MAX_ATTEMPTS, row["id"]))
            continue
        if row["last_attempt_at"] and at - row["last_attempt_at"] < 30 * 2 ** max(0, row["attempts"] - 1):
            continue
        try:
            via = channels.deliver(conn, cfg, row["text"], preferred=row["channel"], built=built,
                                   buttons=json.loads(row["buttons"]) if row["buttons"] else None,
                                   verbatim=row["kind"] in VERBATIM)
        except ChannelError as exc:
            conn.execute("UPDATE notifications SET attempts = attempts + 1, last_attempt_at = ?, "
                         "last_error = ? WHERE id = ?", (at, str(exc)[:300], row["id"]))
            results.append((row["id"], None, str(exc)))
            continue
        conn.execute("UPDATE notifications SET attempts = attempts + 1, last_attempt_at = ?, sent_at = ?, "
                     "sent_via = ?, message_ref = ? WHERE id = ?",
                     (at, at, via, getattr(built[via], "last_ref", None), row["id"]))
        results.append((row["id"], via, None))
    return results
