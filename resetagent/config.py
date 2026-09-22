"""Reset's settings: ~/.reset/config.json, plus optional environment variables.

Nobody needs environment variables to use Reset: `resetctl setup` saves everything (including the
Telegram bot token) in config.json, readable only by you. Every supported variable is listed in
ENV_VARS and in .env.example. Read them only through env(). The real environment wins over a
git-ignored `.env` file in the repository root, which the background service reads too.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

ENV_VARS = {
    # Everyday overrides (all optional)
    "RESET_HOME": "Where Reset keeps its database, settings and logs (default ~/.reset).",
    "RESET_TELEGRAM_BOT_TOKEN": "Telegram bot token; overrides the one `resetctl setup telegram` saved.",
    "RESET_CODEX_BIN": "Path to the codex CLI (default: the newest one found).",
    "RESET_CLAUDE_BIN": "Path to the claude CLI (default: the one on PATH, else Claude Desktop's).",
    "CODEX_HOME": "Codex's own settings folder (default ~/.codex); Reset reads your chosen model there.",
    # Development and tests
    "RESET_DEBUG": "Set to 1 for full tracebacks in the background service log.",
    "RESET_PRIVATE_STRINGS": "Comma-separated personal values the local privacy check keeps out of commits.",
    "RESET_NO_BANNERS": "Set to 1 to suppress macOS notification banners.",
    "RESET_TELEGRAM_API": "Telegram API base URL (tests point it at a local fake).",
    "RESET_IMESSAGE_DB": "Path to a Messages database (tests).",
    "RESET_ALLOW_FAKE_ENGINE": "Set to 1 to enable the fake run engine (tests only).",
    "RESET_FAKE_SNAPSHOT": "Usage snapshot JSON used by the fake engine (tests only).",
}

# Settings an environment variable can override: (path in config.json, variable).
ENV_OVERRIDES = [(("channels", "telegram", "botToken"), "RESET_TELEGRAM_BOT_TOKEN"),
                 (("codexBin",), "RESET_CODEX_BIN"), (("claudeBin",), "RESET_CLAUDE_BIN")]


def parse_dotenv(text: str) -> dict:
    """KEY=VALUE lines. Supports comments, blank lines, `export`, quotes. Unknown keys are ignored."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in ENV_VARS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


_dotenv = None


def dotenv() -> dict:
    global _dotenv
    if _dotenv is None:
        try:
            _dotenv = parse_dotenv((ROOT / ".env").read_text())
        except OSError:
            _dotenv = {}
    return _dotenv


def env(name: str, default=None):
    """A registered environment variable: the real environment first, then the repo's .env file."""
    if name not in ENV_VARS:
        raise KeyError(f"{name} isn't a registered Reset environment variable (see config.ENV_VARS)")
    value = os.environ.get(name)
    if value is None:
        value = dotenv().get(name)
    return default if value in (None, "") else value


# "auto" is deliberately missing: automatic execution is not available in this version.
MODES = ("notify", "ask")

DEFAULTS = {
    "mode": "ask",
    "codexBin": None,
    "claudeBin": None,
    "channels": {
        # Notifications try these in order and fall back on failure. "local" is always the last resort.
        "order": ["telegram", "imessage", "local"],
        "imessage": {"enabled": False, "handle": None, "chatGuid": None, "replyToUnknown": False},
        "telegram": {"enabled": False, "botToken": None, "botUsername": None, "chatId": None, "ownerId": None,
                     "pairingCode": None, "replyToUnknown": True},
        "local": {"enabled": True},
    },
    # Brains answer anything that isn't an exact command. "auto" = the signed-in CLIs (Claude Code, then Codex);
    # "none" = commands only. Models default to each CLI's own default.
    "brain": {
        "order": ["auto"],
        "claudeModel": None,
        "codexModel": None,
        "codexEffort": None,
        "timeoutSeconds": 180,
        "historyMessages": 12,
        "minRemainingPercent": 2,
    },
    "monitor": {
        "statusMinutes": 15,
        "grantNoticeDays": [7],
        "weeklyHeadsUpHours": 24,
        "weeklyHeadsUpMinUnusedPercent": 25,
        "unavailableNoticeHours": 6,
    },
    "runs": {
        "root": "~/Reset/runs",
        "budgetTokens": 150000,
        "maxMinutes": 20,
        "askMinutes": 30,
        "stopGraceSeconds": 10,
        "resetMarginMinutes": 30,
        "minRemainingPercent": 10,
        "codexEffort": "medium",
        "claudeMaxBudgetUsd": 2.0,
    },
    "daemon": {"inboxSeconds": 3},
}


def home() -> Path:
    return Path(env("RESET_HOME", "~/.reset")).expanduser()


def path() -> Path:
    return home() / "config.json"


def merged(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


def load(apply_env: bool = True) -> dict:
    try:
        stored = json.loads(path().read_text())
    except FileNotFoundError:
        stored = {}
    cfg = merged(DEFAULTS, stored)
    if apply_env:
        for keys, name in ENV_OVERRIDES:
            value = env(name)
            if value:
                target = cfg
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
    return cfg


def save(cfg: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    # The file can hold a Telegram bot token, so keep it private.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(cfg, handle, indent=2)
        handle.write("\n")
    os.replace(temp, target)


@contextlib.contextmanager
def editing():
    """Load, yield for mutation, then save; serialized across processes."""
    home().mkdir(parents=True, exist_ok=True)
    with open(home() / "config.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load(apply_env=False)  # environment overrides are never written to disk
        yield cfg
        save(cfg)


def runs_root(cfg: dict) -> Path:
    return Path(cfg["runs"]["root"]).expanduser()
