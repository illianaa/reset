"""Deterministic handling of inbound messages. No LLM is involved, so capture, status and stop
keep working even when every model quota is exhausted. Message text is data: it is stored or
matched against fixed patterns, never executed.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from resetagent import apps, brain, config, db, ideas, notify, runs, settings, status
from resetagent.providers.common import describe
from resetagent.timeutil import local, now, span

HELP = ("Reset commands:\n"
        "idea <text> · save an idea (or start with +)\n"
        "ideas · list them · drop <#> · remove one\n"
        "status · usage and resets\n"
        "run <#> · I'll ask before starting it, on the subscription that expires soonest\n"
        "run <#> on codex with sol at xhigh · pick the engine, model or effort\n"
        "yes <code> / no <code> · answer a request\n"
        "runs · what's running · stop <#> · stop one run · stop · stop everything\n"
        "open <run#> · open a run's chat in the Codex or Claude app on your Mac\n"
        "floor <n>% · the share of each usage limit runs leave alone")


@dataclass
class Command:
    kind: str
    arg: str | None = None
    extra: str | None = None
    options: dict | None = None


PATTERNS = [
    (re.compile(r"^(?:idea|add)\s*[:\-–—]?\s+(.+)$", re.I | re.S), "idea"),
    (re.compile(r"^\+\s*(.+)$", re.S), "idea"),
    (re.compile(r"^(?:ideas|idea|list)\??$", re.I), "ideas"),
    (re.compile(r"^(?:status|usage|limits)\??$", re.I), "status"),
    (re.compile(r"^(?:yes|y|approve|go|start)\s+(\d{4})\.?$", re.I), "approve"),
    (re.compile(r"^(?:yes|y|approve|go|start)[.!]?$", re.I), "approve-missing-code"),
    (re.compile(r"^(?:no|n|skip|decline)\s+(\d{4})\.?$", re.I), "decline"),
    (re.compile(r"^(?:switch|swap)\s+(\d{4})$", re.I), "switch"),
    (re.compile(r"^stop(?:\s+(?:run\s+)?#?(\d+|all))?[.!]?$", re.I), "stop"),
    (re.compile(r"^(?:runs|jobs)\??$", re.I), "runs"),
    (re.compile(r"^(?:open|show)\s+(?:run\s+)?#?(\d+)(?:\s+in\s+(?:the\s+)?(?:codex|claude|app))?[.!]?$", re.I), "open"),
    (re.compile(r"^(?:floor|keep)(?:\s+(?:at\s+)?(\d+)\s*%?)?[.!?]?$", re.I), "floor"),
    (re.compile(r"^undo\s+([0-9a-f]{8})$", re.I), "undo"),
    (re.compile(r"^(?:drop|remove|delete)\s+(?:idea\s+)?#?(\d+)$", re.I), "drop"),
    (re.compile(r"^(?:ok|buzzed|got it)\s+(\d{4})$", re.I), "delivery-buzz"),
    (re.compile(r"^(?:quiet|silent)\s+(\d{4})$", re.I), "delivery-silent"),
    (re.compile(r"^(?:help|commands|\?)$", re.I), "help"),
]


SLASH = re.compile(r"/([A-Za-z_]+)(?:@\w+)?(?:\s+(.*))?", re.S)
# "run 3", "run 3 on codex", "run #3 with sol at xhigh", "run 3 on claude with fable at max effort"
RUN = re.compile(r"(?:run|propose)\s+#?(\d+)"
                 r"(?:\s+(?:(?:on|in|with|using)\s+)?(codex|claude))?"
                 r"(?:\s+(?:with|using)\s+(?!(?:low|medium|high|xhigh|extra|max|ultra)\b)([\w.\[\]-]+))?"
                 r"(?:\s+(?:(?:at|with)\s+)?(low|medium|high|xhigh|extra[\s-]?high|max|ultra)(?:\s+effort)?)?[.!]?",
                 re.I)


def parse(text: str) -> Command:
    body = text.strip()
    slash = SLASH.fullmatch(body)
    if slash:  # Telegram commands: "/status", "/idea@ResetBot build a thing"
        if slash.group(1).lower() == "start":
            return Command("help")
        body = f"{slash.group(1)} {slash.group(2) or ''}".strip()
    body = re.sub(r"^@?reset\b[\s,:]*", "", body, flags=re.I) or "help"
    run = RUN.fullmatch(body)
    if run:
        idea, engine, model, effort = run.groups()
        effort = effort and ("xhigh" if effort.lower().startswith("extra") else effort.lower())
        return Command("propose", idea, engine and engine.lower(), {"model": model, "effort": effort})
    for pattern, kind in PATTERNS:
        match = pattern.match(body)
        if match:
            groups = [g for g in match.groups() if g is not None]
            return Command(kind, groups[0].strip() if groups else None,
                           groups[1].lower() if len(groups) > 1 else None)
    return Command("unknown", body)


def process_pending(conn, cfg: dict) -> int:
    handled = 0
    for row in conn.execute("SELECT * FROM inbound WHERE handled_at IS NULL ORDER BY id").fetchall():
        command = parse(row["text"])
        try:
            reply = handle(conn, cfg, row, command)
        except Exception as exc:  # never lose the message or stall the queue
            reply = "Sorry, that failed on my side. Your message is saved."
            command.kind = f"error: {describe(exc)}"
        conn.execute("UPDATE inbound SET handled_at = ?, result = ? WHERE id = ?",
                     (now(), command.kind[:200], row["id"]))
        if reply:
            notify.enqueue(conn, "reply", reply, dedupe_key=f"reply:{row['id']}",
                           channel=row["channel"], expires_at=now() + 3600)
        handled += 1
    return handled


def handle(conn, cfg: dict, row, command: Command) -> str | None:
    channel = row["channel"]
    kind = command.kind
    if kind == "idea":
        idea_id = ideas.add(conn, command.arg, source=channel, source_ref=f"{channel}:{row['transport_id']}")
        return f"Saved idea #{idea_id}: {ideas.title_for(command.arg)}. It won't run unless you approve it."
    if kind == "ideas":
        rows = ideas.listing(conn)
        if not rows:
            return "No ideas yet. Send “idea <text>” to save one."
        lines = [f"#{r['id']} {r['title']}" + (" (running)" if r["status"] == "running" else "") for r in rows[:10]]
        more = f"\n…and {len(rows) - 10} more" if len(rows) > 10 else ""
        return "Your ideas:\n" + "\n".join(lines) + more + "\nSend “run <#>” and I'll ask before starting one."
    if kind == "status":
        snap = status.latest(conn)
        if snap is None:
            db.kv_set(conn, "status:refresh", True)
            return "I haven't read your usage yet; I'll send it once I have."
        age = now() - (status.parse_iso(snap["collectedAt"]) or now())
        return status.render_short(snap) + f"\n(checked {span(age)} ago)"
    if kind == "propose":
        try:
            runs.propose(conn, cfg, int(command.arg), engine=command.extra, via=channel, **(command.options or {}))
        except runs.RunError as exc:
            return str(exc)
        return None  # the request itself is sent as the reply
    if kind == "approve":
        try:
            run = runs.approve(conn, cfg, command.arg, via=channel)
        except runs.RunError as exc:
            return str(exc)
        where = ""
        if cfg["runs"]["showInApps"] and run["engine"] == "codex":
            where = " It's pinned in the Codex app."
        elif cfg["runs"]["showInApps"] and run["engine"] == "claude":
            where = " I'll send a link to watch it live in the Claude app."
        return (f"Started run #{run['id']} on {run['engine'].capitalize()}. It stops by "
                f"{local(run['deadline_at'])} or at {run['budget_tokens'] // 1000}k tokens.{where} "
                f"Send “stop {run['id']}” to stop it now, or “stop” to stop everything.")
    if kind == "approve-missing-code":
        return "Include the 4-digit code from the request, like “yes 1234”, so I start the right thing."
    if kind == "switch":
        try:
            runs.switch(conn, cfg, command.arg, via=channel)
        except runs.RunError as exc:
            return str(exc)
        return None  # the new request is the reply
    if kind == "decline":
        return "Skipped." if runs.decline(conn, command.arg, via=channel) else f"No pending request with code {command.arg}."
    if kind == "stop":
        target = None if command.arg in (None, "all") else int(command.arg)
        results = runs.stop(conn, cfg, run_id=target, reason=f"stop from {channel}")
        if not results["runs"] and not results["declined"]:
            return "Nothing is running." if target is None else f"Run #{target} isn't running."
        return runs.describe_stop(results)
    if kind == "runs":
        return runs.summary_text(conn)
    if kind == "open":
        run = runs.get(conn, int(command.arg))
        return apps.open_run(conn, run) if run else f"No run #{command.arg}."
    if kind == "floor":
        return floor(cfg, command.arg)
    if kind == "undo":  # the Undo button under a change Reset's AI made
        return settings.undo(conn, command.arg.lower())
    if kind == "drop":
        return f"Removed idea #{command.arg}." if ideas.drop(conn, int(command.arg)) else \
            f"Couldn't remove idea #{command.arg} (missing or running)."
    if kind in ("delivery-buzz", "delivery-silent"):
        return record_delivery(conn, channel, command.arg, "buzz" if kind == "delivery-buzz" else "silent")
    if kind == "help":
        return HELP + ("\nOr just ask me anything about your usage, ideas and runs." if brain.enabled(cfg) else "")
    # Free-form text: only conversational channels (not a self-chat full of personal notes) get an answer.
    if not cfg["channels"].get(channel, {}).get("replyToUnknown", False):
        return None
    if brain.enabled(cfg):
        conn.execute("INSERT INTO brain_tasks(kind, inbound_id, channel, created_at) VALUES ('reply', ?, ?, ?)",
                     (row["id"], channel, now()))
        command.kind = "brain"
        return None  # the brain thread replies
    return "I didn't catch that. " + HELP


def floor(cfg: dict, value: str | None) -> str:
    """Show or set the share of each usage limit runs leave alone. The user's own command, so no model is needed
    (the brain can change it too, with change_setting, and Reset announces that change with an Undo button)."""
    if value is None:
        return (f"Runs leave at least {cfg['runs']['keepPercent']}% of every usage limit untouched: none starts "
                "below that, and running ones stop if a subscription drops under it. To change it, send "
                "“floor 8” (any number from 0 to 100).")
    share = int(value)
    if share > 100:
        return "The floor is a percentage from 0 to 100."
    with config.editing() as saved:
        saved["runs"]["keepPercent"] = share
    cfg["runs"]["keepPercent"] = share
    if share == 0:
        return "Done: runs may now use everything that's left of your limits."
    return f"Done: runs now leave at least {share}% of every usage limit untouched."


def record_delivery(conn, channel: str, code: str, result: str) -> str:
    test = db.kv_get(conn, f"delivery-test:{channel}")
    if not test or test.get("code") != code:
        return f"No delivery test with code {code}."
    db.kv_set(conn, f"delivery:{channel}", result)
    test["result"] = result
    db.kv_set(conn, f"delivery-test:{channel}", test)
    if result == "buzz":
        return f"Delivery confirmed: {channel} notifications reach you."
    return (f"Noted: {channel} messages arrive silently. If Telegram is set up, alerts will go there first.")
