"""Channel router: deliver through the first working channel, falling back in order."""
from __future__ import annotations

from resetagent import db
from resetagent.channels.base import ChannelError, ChannelUnavailable
from resetagent.channels.imessage import IMessage
from resetagent.channels.local import Local
from resetagent.channels.telegram import Telegram
from resetagent.timeutil import now

KINDS = {"imessage": IMessage, "telegram": Telegram, "local": Local}
PEOPLE = ("telegram", "imessage")  # where the user reads and can answer (local is only a log and a banner)

__all__ = ["ChannelError", "ChannelUnavailable", "KINDS", "build", "order", "deliver"]


def build(cfg: dict) -> dict:
    return {name: kind(cfg) for name, kind in KINDS.items()}


def reachable(cfg: dict) -> bool:
    """Whether a message can reach the user somewhere they can answer it."""
    built = build(cfg)
    return any(built[name].configured() for name in PEOPLE)


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
            buttons=None, verbatim: bool = False, to_person: bool = False) -> str:
    """Send through the first working channel. Buttons are shown where supported (Telegram). verbatim: send the
    text exactly as it is (don't drop Markdown), for messages quoting what a run wrote, like a command. to_person:
    only where the user can answer it, never just the local log."""
    built = built or build(cfg)
    errors = []
    for name in order(conn, cfg, preferred):
        channel = built[name]
        if not channel.configured() or (to_person and name not in PEOPLE):
            continue
        try:
            channel.send(text, buttons=buttons, verbatim=verbatim)
        except ChannelError as exc:
            errors.append(f"{name}: {exc}")
            continue
        conn.execute("INSERT INTO outbound(channel, text, sent_at) VALUES (?, ?, ?)", (name, text, now()))
        return name
    raise ChannelError("; ".join(errors) or "no channel is configured")
