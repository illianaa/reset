"""Shared channel types."""
from __future__ import annotations

import re


class ChannelError(Exception):
    """Delivery or polling failed; another channel may still work."""


class ChannelUnavailable(ChannelError):
    """The channel can't work until the user changes something, such as a macOS permission."""


def echo_key(text: str) -> str:
    """Normalize text the way Messages may rewrite it, for matching our own sends."""
    text = re.sub(r"[‍︀-️]", "", text)
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())[:120]


def persist_inbound(conn, channel: str, transport_id: str, sender, text: str, sent_at=None) -> bool:
    """Store an inbound message before anything acts on it. Duplicate transport ids are ignored."""
    from resetagent.timeutil import now

    cursor = conn.execute(
        "INSERT OR IGNORE INTO inbound(channel, transport_id, sender, text, sent_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (channel, transport_id, sender, text, sent_at, now()))
    return cursor.rowcount == 1
