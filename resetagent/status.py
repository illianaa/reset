"""Collect, store and render usage across providers. Freshness is always explicit."""
from __future__ import annotations

import json

from resetagent.providers import claude, codex
from resetagent.providers.common import describe, unavailable
from resetagent.timeutil import iso, local, now, parse_iso, span

NAMES = {"codex": "Codex", "claude": "Claude"}


def codex_status(cfg: dict) -> dict:
    executable = codex.resolve_bin(cfg)
    if not executable:
        return unavailable("codex", "Codex CLI not found")
    try:
        return codex.read_status(executable)
    except Exception as exc:  # a broken provider must never break monitoring
        return unavailable("codex", describe(exc))


def claude_live(cfg: dict) -> dict:
    executable = claude.resolve_bin(cfg)
    if not executable:
        return unavailable("claude", "Claude Code CLI not found")
    try:
        return claude.read_live(executable)
    except Exception as exc:
        return unavailable("claude", describe(exc))


def claude_status(cfg: dict, conn=None) -> dict:
    data = claude_live(cfg)
    org = None
    executable = claude.resolve_bin(cfg)
    if executable:
        try:
            auth = claude.auth_status(executable)
            org = auth.get("orgFingerprint")
            data["account"] = {"loggedIn": auth.get("loggedIn"), "orgFingerprint": org}
        except Exception as exc:
            data["account"] = {"error": describe(exc)}
    try:
        data["grants"] = claude.grants(conn, org)
    except Exception as exc:
        data["grants"] = {"state": "unavailable", "items": [], "note": describe(exc)}
    return data


def collect(cfg: dict, conn=None) -> dict:
    snap = {"collectedAt": iso(now()),
            "providers": {"codex": codex_status(cfg), "claude": claude_status(cfg, conn)}}
    if conn is not None:
        store(conn, snap)
    return snap


def store(conn, snap: dict) -> None:
    stamp = parse_iso(snap["collectedAt"]) or now()
    for name, data in snap["providers"].items():
        conn.execute("INSERT INTO samples(provider, state, collected_at, payload) VALUES (?, ?, ?, ?)",
                     (name, data.get("state", "unavailable"), stamp, json.dumps(data)))


def latest(conn) -> dict | None:
    providers, newest = {}, None
    for name in NAMES:
        row = conn.execute("SELECT collected_at, payload FROM samples WHERE provider = ? "
                           "ORDER BY collected_at DESC LIMIT 1", (name,)).fetchone()
        if row:
            providers[name] = json.loads(row["payload"])
            newest = max(newest or 0, row["collected_at"])
    return {"collectedAt": iso(newest), "providers": providers} if providers else None


def weekly(data: dict) -> list:
    return [w for w in data.get("windows") or [] if w.get("windowDurationMins") == 10080]


def pct(value) -> str:
    return "?" if value is None else f"{value:g}%"


def grant_rows(name: str, data: dict) -> list:
    """(label, expires_at, note) for every usable one-time reset."""
    rows = []
    if name == "codex":
        for credit in (data.get("resetCredits") or {}).get("credits") or []:
            if credit.get("status") == "available":
                rows.append((credit.get("title") or "Reset credit", credit.get("expiresAt"), None))
    else:
        grants = data.get("grants") or {}
        for item in grants.get("items") or []:
            if (item.get("resetsLeft") or 0) > 0:
                left = item.get("resetsLeft")
                rows.append((item.get("label"), item.get("endsAt"),
                             f"{left} left" + (" · manual" if item.get("source") == "manual" else "")))
    return rows


def render(snap: dict, at: float | None = None) -> str:
    at = now() if at is None else at
    lines = []
    for name, data in snap["providers"].items():
        state = data.get("state")
        observed = parse_iso(data.get("observedAt"))
        header = f"{NAMES[name]}"
        plan = data.get("planType") or data.get("subscriptionType")
        if plan:
            header += f" · {plan.capitalize()}"
        header += f" · {state}" + (f" ({local(observed, at)})" if observed else "")
        lines.append(header)
        if state == "unavailable":
            lines.append(f"  {data.get('error')}")
        for w in data.get("windows") or []:
            reset = w.get("resetsAt")
            when = f"resets {local(reset, at)} (in {span(reset - at)})" if reset else "reset time unknown"
            lines.append(f"  {(w.get('label') or w['id']):<22}{pct(w.get('usedPercent')):>5} used   {when}")
        paid = data.get("paidUsage") or {}
        if name == "claude" and paid.get("enabled") is not None:
            lines.append(f"  {'Extra usage (paid)':<22}{'on' if paid['enabled'] else 'off':>5}")
        if name == "codex" and paid.get("possible"):
            lines.append(f"  {'Purchased credits':<22}{'yes':>5}   runs are blocked while credits could be spent")
        rows = grant_rows(name, data)
        if name == "codex":
            credits = data.get("resetCredits")
            count = "unknown" if credits is None else f"{credits.get('availableCount')} available"
            lines.append(f"  {'Reset credits':<22}{count:>5}")
        else:
            grants = data.get("grants") or {}
            seen = parse_iso(grants.get("observedAt"))
            source = {"cached": f"cached from Claude Desktop at {local(seen, at)}" if seen else "cached",
                      "manual": "entered manually",
                      "unavailable": "unknown (not exposed by Claude Code)"}[grants.get("state", "unavailable")]
            lines.append(f"  {'Reset grants':<22}{source}")
            if grants.get("note"):
                lines.append(f"    {grants['note']}")
        for label, expires, note in rows:
            when = f"expires {local(expires, at)} (in {span(expires - at)})" if expires else "expiry unknown"
            lines.append(f"    {label}: {when}" + (f" · {note}" if note else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_short(snap: dict, at: float | None = None) -> str:
    """A compact version for text messages."""
    at = now() if at is None else at
    parts = []
    for name, data in snap["providers"].items():
        if data.get("state") == "unavailable":
            parts.append(f"{NAMES[name]}: unavailable ({data.get('error')})")
            continue
        bits = []
        for w in data.get("windows") or []:
            label = (w.get("label") or w["id"]).replace("Weekly (all models)", "Week").replace("Weekly", "Week")
            bits.append(f"{label} {pct(w.get('usedPercent'))}")
        resets = [w["resetsAt"] for w in weekly(data) if w.get("resetsAt")]
        line = f"{NAMES[name]}: " + ", ".join(bits) + " used"
        if resets:
            line += f"; week resets {local(min(resets), at)}"
        rows = [r for r in grant_rows(name, data) if r[1]]
        if rows:
            soonest = min(rows, key=lambda r: r[1])
            line += f". {len(rows)} reset{'s' if len(rows) != 1 else ''} saved, next expires {local(soonest[1], at)}"
            if name == "claude" and (data.get("grants") or {}).get("state") == "cached":
                line += " (cached)"
        parts.append(line)
    return "\n".join(parts)
