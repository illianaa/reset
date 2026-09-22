"""The idea list. Saving an idea never authorizes running it."""
from __future__ import annotations

import sqlite3

from resetagent.timeutil import now

ENGINES = ("codex", "claude")


def title_for(text: str) -> str:
    first = " ".join(text.strip().split("\n", 1)[0].split())
    return first if len(first) <= 60 else first[:57].rstrip() + "…"


def add(conn, text: str, source: str, source_ref: str | None = None, engine: str | None = None) -> int:
    text = text.strip()
    if not text:
        raise ValueError("An idea needs some text.")
    if engine is not None and engine not in ENGINES:
        raise ValueError(f"Engine must be one of: {', '.join(ENGINES)}")
    stamp = now()
    try:
        cursor = conn.execute(
            "INSERT INTO ideas(text, title, engine, source, source_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (text, title_for(text), engine, source, source_ref, stamp, stamp))
    except sqlite3.IntegrityError:
        # The same inbound message was already saved (e.g. after a restart mid-handling).
        row = conn.execute("SELECT id FROM ideas WHERE source_ref = ?", (source_ref,)).fetchone()
        if row is None:
            raise
        return row["id"]
    return cursor.lastrowid


def get(conn, idea_id: int):
    return conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()


def listing(conn, include_all: bool = False) -> list:
    if include_all:
        return conn.execute("SELECT * FROM ideas ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM ideas WHERE status IN ('open', 'running') ORDER BY id").fetchall()


def set_status(conn, idea_id: int, status: str) -> None:
    conn.execute("UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?", (status, now(), idea_id))


def drop(conn, idea_id: int) -> bool:
    row = get(conn, idea_id)
    if row is None or row["status"] == "running":
        return False
    set_status(conn, idea_id, "archived")
    return True
