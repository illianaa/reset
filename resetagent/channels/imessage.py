"""iMessage through Messages.app: send with AppleScript, receive by reading chat.db (macOS only).

Only your self-chat is read (a conversation with your own address). Sending needs the
Automation permission for Messages; receiving needs Full Disk Access for the process that
reads chat.db. Approach adapted from Anthropic's official iMessage channel plugin.
"""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import struct
import subprocess

from resetagent import config, db
from resetagent.channels.base import ChannelError, ChannelUnavailable, echo_key, persist_inbound
from resetagent.timeutil import now

CHAT_DB = Path.home() / "Library/Messages/chat.db"
MARKER = "[Reset]"
APPLE_EPOCH = 978307200
CATCH_UP_SECONDS = 24 * 3600
ECHO_SECONDS = 600

SEND_TO_CHAT = """on run argv
  tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)
end run
"""
SEND_TO_HANDLE = """on run argv
  tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    send (item 1 of argv) to participant (item 2 of argv) of targetService
  end tell
end run
"""


def normalize_handle(value: str) -> str:
    value = value.strip()
    if re.match(r"^[A-Za-z]:", value):
        value = value[2:]
    if "@" in value:
        return value.lower()
    return re.sub(r"[^\d+]", "", value)


def parse_attributed_body(blob) -> str | None:
    """Extract the NSString payload from a typedstream NSAttributedString (newer macOS)."""
    if not blob:
        return None
    data = bytes(blob)
    i = data.find(b"NSString")
    if i < 0:
        return None
    i += len(b"NSString")
    while i < len(data) and data[i] != 0x2B:  # '+' marks the inline string
        i += 1
    i += 1
    if i >= len(data):
        return None
    lead = data[i]
    i += 1
    if lead == 0x81:
        length, i = data[i], i + 1
    elif lead == 0x82:
        length, i = struct.unpack_from("<H", data, i)[0], i + 2
    elif lead == 0x83:
        length, i = int.from_bytes(data[i:i + 3], "little"), i + 3
    else:
        length = lead
    if i + length > len(data):
        return None
    return data[i:i + length].decode("utf-8", "replace")


def apple_time(value) -> float | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return (value / 1e9 if value > 1e12 else value) + APPLE_EPOCH


class IMessage:
    name = "imessage"

    def __init__(self, cfg: dict, db_path: str | None = None, run=subprocess.run):
        self.settings = cfg["channels"]["imessage"]
        self.db_path = Path(db_path or config.env("RESET_IMESSAGE_DB") or CHAT_DB)
        self._run = run

    def configured(self) -> bool:
        return bool(self.settings.get("enabled") and (self.settings.get("chatGuid") or self.settings.get("handle")))

    def send(self, text: str, buttons=None) -> None:
        body = f"{MARKER} {text}"
        guid = self.settings.get("chatGuid")
        script, target = (SEND_TO_CHAT, guid) if guid else (SEND_TO_HANDLE, self.settings.get("handle"))
        if not target:
            raise ChannelError("no iMessage address configured")
        try:
            result = self._run(["osascript", "-", body, target], input=script,
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChannelError(f"osascript failed ({type(exc).__name__})") from None
        if result.returncode != 0:
            lines = (result.stderr or "").strip().splitlines()
            raise ChannelError(lines[-1][:200] if lines else f"osascript exit {result.returncode}")

    def open(self) -> sqlite3.Connection:
        try:
            chat = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            chat.execute("SELECT 1 FROM message LIMIT 1")
            return chat
        except sqlite3.Error:
            raise ChannelUnavailable(
                "can't read Messages; the process running Reset needs Full Disk Access") from None

    def self_addresses(self, chat: sqlite3.Connection) -> set:
        found = set()
        if self.settings.get("handle"):
            found.add(normalize_handle(self.settings["handle"]))
        rows = chat.execute("SELECT DISTINCT account FROM message WHERE is_from_me = 1 "
                            "AND account IS NOT NULL AND account != '' LIMIT 50").fetchall()
        found.update(normalize_handle(r[0]) for r in rows)
        found.discard("")
        return found

    def self_chats(self) -> list:
        """(chat guid, address) for conversations with your own addresses; needs Full Disk Access."""
        chat = self.open()
        try:
            selves = self.self_addresses(chat)
            if not selves:
                return []
            marks = ",".join("?" * len(selves))
            return chat.execute(
                f"SELECT DISTINCT c.guid, h.id FROM chat c JOIN chat_handle_join chj ON chj.chat_id = c.ROWID "
                f"JOIN handle h ON h.ROWID = chj.handle_id WHERE c.style = 45 AND lower(h.id) IN ({marks})",
                tuple(selves)).fetchall()
        finally:
            chat.close()

    def poll(self, conn, at: float | None = None) -> int:
        at = now() if at is None else at
        if not self.configured():
            return 0
        chat = self.open()
        try:
            cursor = db.kv_get(conn, "cursor:imessage")
            high = chat.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
            if cursor is None:
                # Start from now: never import old personal conversations.
                db.kv_set(conn, "cursor:imessage", high)
                return 0
            selves = self.self_addresses(chat)
            rows = []
            if selves:
                marks = ",".join("?" * len(selves))
                # Self-chat input arrives as is_from_me = 0 from your own address; our sends echo the same way.
                rows = chat.execute(
                    f"SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.date, h.id FROM message m "
                    f"JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
                    f"JOIN chat c ON c.ROWID = cmj.chat_id JOIN handle h ON h.ROWID = m.handle_id "
                    f"WHERE m.ROWID > ? AND m.ROWID <= ? AND m.is_from_me = 0 AND c.style = 45 "
                    f"AND m.service = 'iMessage' AND lower(h.id) IN ({marks}) ORDER BY m.ROWID",
                    (cursor, high, *selves)).fetchall()
        finally:
            chat.close()
        recent = {echo_key(r["text"]) for r in conn.execute(
            "SELECT text FROM outbound WHERE channel = 'imessage' AND sent_at > ?", (at - ECHO_SECONDS,))}
        saved = 0
        for _, guid, text, body, date, handle in rows:
            message = text if text is not None else parse_attributed_body(body)
            if not message or not message.strip():
                continue
            if message.lstrip().startswith(MARKER) or echo_key(message) in recent:
                continue  # our own notification coming back through the self-chat
            sent_at = apple_time(date)
            if sent_at and at - sent_at > CATCH_UP_SECONDS:
                continue  # too old to act on after downtime
            if persist_inbound(conn, self.name, guid, handle, message.strip(), sent_at):
                saved += 1
        db.kv_set(conn, "cursor:imessage", high)
        return saved
