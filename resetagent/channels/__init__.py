"""Channel router: deliver through the first working channel, falling back in order."""
from __future__ import annotations

from resetagent import db
from resetagent.channels.base import ChannelError, ChannelUnavailable
from resetagent.channels.imessage import IMessage
from resetagent.channels.local import Local
from resetagent.channels.telegram import Telegram
from resetagent.timeutil import now

KINDS = {"imessage": IMessage, "telegram": Telegram, "local": Local}

__all__ = ["ChannelError", "ChannelUnavailable", "KINDS", "build", "order", "deliver"]


def build(cfg: dict) -> dict:
    return {name: kind(cfg) for name, kind in KINDS.items()}


def order(conn, cfg: dict, preferred: str | None = None) -> list:
    names = [n for n in cfg["channels"]["order"] if n in KINDS and n != "local"]
    names += [n for n in KINDS if n not in names and n != "local"]
    # If the iMessage test showed messages arrive without buzzing, alerts go to Telegram first.
    if db.kv_get(conn, "delivery:imessage") == "silent" and "telegram" in names:
        names.remove("telegram")
        names.insert(names.index("imessage") if "imessage" in names else 0, "telegram")
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    return names + ["local"]


def deliver(conn, cfg: dict, text: str, preferred: str | None = None, built: dict | None = None,
            buttons=None, verbatim: bool = False) -> str:
    """Send through the first working channel. Buttons are shown where supported (Telegram). verbatim: send the
    text exactly as it is (don't drop Markdown), for messages quoting what a run wrote, like a command."""
    built = built or build(cfg)
    errors = []
    for name in order(conn, cfg, preferred):
        channel = built[name]
        if not channel.configured():
            continue
        try:
            channel.send(text, buttons=buttons, verbatim=verbatim)
        except ChannelError as exc:
            errors.append(f"{name}: {exc}")
            continue
        conn.execute("INSERT INTO outbound(channel, text, sent_at) VALUES (?, ?, ?)", (name, text, now()))
        return name
    raise ChannelError("; ".join(errors) or "no channel is configured")
