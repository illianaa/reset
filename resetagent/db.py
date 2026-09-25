"""SQLite state: ideas, usage samples, messaging inbox/outbox, run requests and runs.

One transactional store keeps the daemon, the CLI and agent skills from racing each other.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3

from resetagent import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    title TEXT NOT NULL,
    engine TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    source TEXT NOT NULL,
    source_ref TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ideas_by_source_ref ON ideas(source_ref) WHERE source_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    collected_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_by_provider ON samples(provider, collected_at);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    channel TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    last_error TEXT,
    sent_at REAL,
    sent_via TEXT
);

CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    transport_id TEXT NOT NULL,
    sender TEXT,
    text TEXT NOT NULL,
    sent_at REAL,
    received_at REAL NOT NULL,
    handled_at REAL,
    result TEXT,
    UNIQUE (channel, transport_id)
);

CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_grants (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    ends_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    engine TEXT NOT NULL,
    budget_tokens INTEGER NOT NULL,
    max_minutes INTEGER NOT NULL,
    effort TEXT,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_via TEXT,
    run_id INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    ask_id INTEGER REFERENCES asks(id),
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    engine TEXT NOT NULL,
    workdir TEXT NOT NULL,
    budget_tokens INTEGER NOT NULL,
    deadline_at REAL NOT NULL,
    effort TEXT,
    status TEXT NOT NULL DEFAULT 'starting',
    outcome TEXT,
    worker_pid INTEGER,
    worker_lstart TEXT,
    thread_id TEXT,
    turn_id TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    last_event_at REAL,
    ended_at REAL,
    stop_requested_at REAL,
    stop_reason TEXT,
    summary TEXT,
    error TEXT,
    verified_at REAL,
    cleanup TEXT
);

CREATE TABLE IF NOT EXISTS run_pids (
    run_id INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    lstart TEXT NOT NULL,
    command TEXT,
    PRIMARY KEY (run_id, pid, lstart)
);

CREATE TABLE IF NOT EXISTS brain_tasks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    inbound_id INTEGER,
    run_id INTEGER,
    channel TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    done_at REAL,
    brain TEXT,
    result TEXT
);

-- A run asking the user something (a question, or permission for an action) and waiting for the answer.
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                      -- question | permission
    nonce TEXT NOT NULL,                     -- binds button taps to this question
    body TEXT NOT NULL,                      -- JSON: what was asked, with its options
    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting | answered | expired | closed
    answer TEXT,                             -- JSON
    answered_via TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX IF NOT EXISTS questions_by_run ON questions(run_id, status);
"""

# Columns added after the first release; connect() adds any that an older database lacks, then runs the
# statement that fills it in for the rows already there, if one is given.
ADDED_COLUMNS = [
    ("notifications", "buttons", "TEXT"),
    ("asks", "nonce", "TEXT"),
    ("ideas", "project", "TEXT"),
    ("asks", "model", "TEXT"),
    ("asks", "project", "TEXT"),
    ("runs", "model", "TEXT"),
    ("runs", "project", "TEXT"),
    ("runs", "workspace", "TEXT"),
    ("runs", "branch", "TEXT"),
    ("runs", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "live_url", "TEXT"),
    # Claude runs: handed to Claude Desktop ("done") or why not. Runs from before stay out of the apps.
    ("runs", "handoff", "TEXT", "UPDATE runs SET handoff = 'not-shown'"),
    ("runs", "handed_off_at", "REAL"),
    ("runs", "latest_end", "REAL"),  # the latest a run may end (before a usage limit resets), however long it waits
    ("notifications", "message_ref", "TEXT"),  # the channel's id for the sent message (a Telegram message_id)
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else config.home() / "reset.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, column, kind, *fill in ADDED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            for statement in fill:
                conn.execute(statement)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def kv_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))
