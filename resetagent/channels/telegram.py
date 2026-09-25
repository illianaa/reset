"""Telegram through a bot you create with @BotFather. Outbound long polling; no server needed.

Only the paired private chat (and account) can talk to Reset. Keep one poller per bot token: don't also
run Claude Code's Telegram channel plugin on the same bot.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from resetagent import config, db
from resetagent.channels.base import ChannelError, persist_inbound
from resetagent.timeutil import now

API_BASE = "https://api.telegram.org"
LIMIT = 4000  # Telegram allows 4096 characters per message
COMMANDS = [("status", "Usage limits and one-time resets"), ("ideas", "Your idea list"),
            ("idea", "Save an idea: /idea <text>"), ("runs", "Active and recent runs"),
            ("stop", "Stop everything now"), ("floor", "Share of each limit runs leave alone"),
            ("help", "What I can do")]
CALLBACK = re.compile(r"ask:(\d+):([0-9a-f]{8}):([ynx])")
OPEN = re.compile(r"run:(\d+):open")  # "Open in Codex/Claude" under a finished run
UNDO = re.compile(r"undo:([0-9a-f]{8})")  # "Undo" under a setting Reset's AI changed
ANSWER = re.compile(r"qa:(\d+):([0-9a-f]{8}):(y|n|d|\d+\.\d+)")  # a tap on a run's question or permission request
CHOICES = {"y": "allow", "n": "deny", "d": "decide"}
ASKED = re.compile(r"\((?:question|permission) #(\d+)\)")  # how a question message names itself
TAPS = {"y": ("yes", "Starting…"), "n": ("no", "Skipped."), "x": ("switch", "Switching…")}


def plain(text: str) -> str:
    """Messages are sent as plain text, so drop Markdown emphasis a model might still produce."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)


def chunks(text: str, size: int = LIMIT) -> list:
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text]


class Telegram:
    name = "telegram"

    def __init__(self, cfg: dict):
        self.settings = cfg["channels"]["telegram"]
        self.base = config.env("RESET_TELEGRAM_API", API_BASE)

    def configured(self) -> bool:
        s = self.settings
        return bool(s.get("enabled") and s.get("botToken") and s.get("chatId"))

    def call(self, method: str, payload: dict, timeout: float = 20):
        token = self.settings.get("botToken")
        if not token:
            raise ChannelError("no Telegram bot token configured")
        request = urllib.request.Request(
            f"{self.base}/bot{token}/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        # Errors below never include the URL, which contains the token.
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode())
            except (ValueError, OSError):
                raise ChannelError(f"Telegram HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ChannelError(f"Telegram unreachable ({type(exc).__name__})") from None
        if not data.get("ok"):
            raise ChannelError(f"Telegram: {str(data.get('description') or 'request failed')[:160]}")
        return data.get("result")

    def send(self, text: str, buttons=None) -> None:
        parts = chunks(plain(text))
        for index, part in enumerate(parts):
            payload = {"chat_id": self.settings["chatId"], "text": part, "disable_web_page_preview": True}
            if buttons and index == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": b["text"], "callback_data": b["data"]} for b in row] for row in buttons]}
            self.call("sendMessage", payload)

    def typing(self) -> None:
        try:
            self.call("sendChatAction", {"chat_id": self.settings["chatId"], "action": "typing"}, timeout=5)
        except ChannelError:
            pass

    def set_commands(self) -> None:
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})

    def poll(self, conn, at=None) -> int:
        s = self.settings
        if not (s.get("enabled") and s.get("botToken")):
            return 0
        offset = db.kv_get(conn, "cursor:telegram", 0)
        updates = self.call("getUpdates", {"offset": offset, "timeout": 0,
                                           "allowed_updates": ["message", "callback_query"]})
        saved = 0
        for update in updates or []:
            if update.get("callback_query"):
                saved += self.callback(conn, update["callback_query"])
            else:
                saved += self.message(conn, update)
            # Advance only after the update is stored, so nothing is acknowledged before it's saved.
            db.kv_set(conn, "cursor:telegram", update["update_id"] + 1)
        return saved

    def owner(self, chat_id, user_id) -> bool:
        s = self.settings
        return (s.get("chatId") is not None and str(chat_id) == str(s["chatId"])
                and (s.get("ownerId") is None or str(user_id) == str(s["ownerId"])))

    def message(self, conn, update: dict) -> int:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text") or ""
        if self.settings.get("chatId") is None:
            code = self.settings.get("pairingCode")
            if code and chat.get("type") == "private" and code in text.split():
                self.pair(chat["id"], sender.get("id"))
            return 0
        if not self.owner(chat.get("id"), sender.get("id")) or not text.strip():
            return 0
        # A reply to a run's question is the answer to it, in the user's own words.
        asked = ASKED.search(((message.get("reply_to_message") or {}).get("text")) or "")
        if asked:
            text = f"answer {asked.group(1)}: {text.strip()}"
        return int(persist_inbound(conn, self.name, str(update["update_id"]), str(sender.get("id")),
                                   text.strip(), message.get("date")))

    def callback(self, conn, query: dict) -> int:
        """A tap on Start/Skip becomes the equivalent typed command, so it goes through the same checks."""
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        saved, reply = 0, "Only the paired account can do that."
        opening = OPEN.fullmatch(query.get("data") or "")
        undoing = UNDO.fullmatch(query.get("data") or "")
        answering = ANSWER.fullmatch(query.get("data") or "")
        if answering and self.owner(chat_id, (query.get("from") or {}).get("id")):
            asked = conn.execute("SELECT status FROM questions WHERE id = ? AND nonce = ?",
                                 (int(answering.group(1)), answering.group(2))).fetchone()
            reply = "That question is no longer open."
            if asked and asked["status"] == "waiting":
                choice = CHOICES.get(answering.group(3), answering.group(3))
                saved = int(persist_inbound(conn, self.name, f"cb:{query['id']}", str(query["from"]["id"]),
                                            f"answer {answering.group(1)} {choice}", now()))
                reply = "Sending…"
        elif opening and self.owner(chat_id, (query.get("from") or {}).get("id")):
            saved = int(persist_inbound(conn, self.name, f"cb:{query['id']}", str(query["from"]["id"]),
                                        f"open {opening.group(1)}", now()))
            reply = "Opening it on your Mac…"
        elif undoing and self.owner(chat_id, (query.get("from") or {}).get("id")):
            saved = int(persist_inbound(conn, self.name, f"cb:{query['id']}", str(query["from"]["id"]),
                                        f"undo {undoing.group(1)}", now()))
            reply = "Undoing…"
        elif self.owner(chat_id, (query.get("from") or {}).get("id")):
            reply = "That request is no longer valid."
            match = CALLBACK.fullmatch(query.get("data") or "")
            ask = match and conn.execute("SELECT code, status FROM asks WHERE id = ? AND nonce = ?",
                                         (int(match.group(1)), match.group(2))).fetchone()
            if ask and ask["status"] == "pending":
                verb, reply = TAPS[match.group(3)]
                saved = int(persist_inbound(conn, self.name, f"cb:{query['id']}", str(query["from"]["id"]),
                                            f"{verb} {ask['code']}", now()))
                try:  # one tap per request
                    self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message.get("message_id"),
                                                         "reply_markup": {"inline_keyboard": []}})
                except ChannelError:
                    pass
        try:
            self.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": reply})
        except ChannelError:
            pass
        return saved

    def pair(self, chat_id, user_id=None) -> None:
        with config.editing() as cfg:
            settings = cfg["channels"]["telegram"]
            settings.update({"chatId": chat_id, "ownerId": user_id, "pairingCode": None})
        self.settings.update({"chatId": chat_id, "ownerId": user_id, "pairingCode": None})
        try:
            self.set_commands()
        except ChannelError:
            pass
        try:
            self.call("sendMessage", {"chat_id": chat_id, "text": WELCOME})
        except ChannelError:
            pass


WELCOME = ("Hi, I'm Reset. I keep an eye on your AI subscription limits and help you use capacity that would "
           "otherwise expire on ideas you care about.\n\n"
           "Commands: /status · /ideas · /idea <text> · /runs · /stop\n"
           "Or just ask, like “how many one-time resets do I have?”")
