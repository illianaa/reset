"""Reset's tools for AI brains. One implementation, reached over MCP by Claude Code and Codex.

Brains can read everything and take a few safe actions (save/remove ideas, request a run, stop).
They can change only the settings listed in settings.py, and Reset announces each change with an Undo button.
They cannot approve runs or redeem resets: those stay with the user.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from resetagent import apps, config, db, ideas, models, runs, settings, status, workspace
from resetagent.providers.common import describe
from resetagent.timeutil import local, now, parse_iso, span


@contextlib.contextmanager
def connection():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _when(ts, at: float):
    if not ts:
        return None
    return f"{local(ts, at)} (in {span(ts - at)})" if ts > at else f"{local(ts, at)} (passed)"


def get_usage(conn, refresh: bool = False) -> dict:
    snap = None if refresh else status.latest(conn)
    if snap is None:
        snap = status.collect(config.load(), conn)
    at = now()
    report = {"checked": f"{span(at - (parse_iso(snap['collectedAt']) or at))} ago", "subscriptions": {}}
    for name, data in snap["providers"].items():
        entry = {"state": data.get("state"), "plan": data.get("planType") or data.get("subscriptionType")}
        if data.get("error"):
            entry["error"] = data["error"]
        entry["limits"] = [{"name": w.get("label") or w["id"], "usedPercent": w.get("usedPercent"),
                            "remainingPercent": w.get("remainingPercent"), "resets": _when(w.get("resetsAt"), at)}
                           for w in data.get("windows") or []]
        paid = data.get("paidUsage") or {}
        if "enabled" in paid:
            entry["paidExtraUsage"] = {True: "on", False: "off"}.get(paid["enabled"], "unknown")
        entry["oneTimeResets"] = [{"title": label, "expires": _when(expires, at), "note": note}
                                  for label, expires, note in status.grant_rows(name, data)]
        if name == "codex":
            credits = data.get("resetCredits")
            entry["resetCreditsAvailable"] = None if credits is None else credits.get("availableCount")
            entry["howToRedeem"] = "In Codex, when a limit is nearly used up. Reset never redeems."
        else:
            grants = data.get("grants") or {}
            seen = parse_iso(grants.get("observedAt"))
            entry["oneTimeResetsSource"] = {
                "cached": f"cached from Claude Desktop {span(at - seen)} ago" if seen else "cached",
                "manual": "entered manually"}.get(grants.get("state"), "not visible to Reset")
            entry["howToRedeem"] = "/limit-reset in Claude Code, or clau.de/reset. Reset never redeems."
        report["subscriptions"][status.NAMES[name]] = entry
    return report


def list_ideas(conn, include_finished: bool = False) -> dict:
    rows = ideas.listing(conn, include_all=include_finished)
    return {"ideas": [{"number": r["id"], "title": r["title"], "text": r["text"][:400], "status": r["status"],
                       "engine": r["engine"], "project": r["project"], "saved": local(r["created_at"])}
                      for r in rows]}


def add_idea(conn, text: str, engine: str | None = None, project: str | None = None) -> dict:
    idea_id = ideas.add(conn, text, source="assistant", engine=engine, project=project)
    return {"saved": True, "number": idea_id, "title": ideas.title_for(text),
            "note": "Saved only. It runs only if the user approves a run request."}


def update_idea(conn, number: int, text: str | None = None, engine: str | None = None,
                project: str | None = None) -> dict:
    return {"updated": ideas.update(conn, int(number), text=text, engine=engine, project=project)}


def remove_idea(conn, number: int) -> dict:
    return {"removed": ideas.drop(conn, int(number))}


def list_runs(conn, limit: int = 10) -> dict:
    at = now()
    items = []
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 50)),)):
        started = r["started_at"] or r["created_at"]
        items.append({"run": r["id"], "idea": r["idea_id"], "engine": r["engine"], "status": r["status"],
                      "outcome": r["outcome"], "started": local(started, at),
                      "duration": span((r["ended_at"] or at) - started), "tokensUsed": r["tokens_used"],
                      "stopReason": r["stop_reason"]})
    pending = [{"code": a["code"], "idea": a["idea_id"], "engine": a["engine"], "expires": local(a["expires_at"], at)}
               for a in conn.execute("SELECT * FROM asks WHERE status = 'pending' ORDER BY id")]
    return {"runs": items, "pendingRequests": pending}


def _files(root: Path, limit: int = 40) -> list:
    found = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        found.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size})
        if len(found) >= limit:
            break
    return found


def get_run(conn, run: int) -> dict:
    r = runs.get(conn, int(run))
    if r is None:
        return {"error": f"No run #{run}."}
    idea = ideas.get(conn, r["idea_id"])
    root = Path(r["workdir"])
    notes = {}
    for name in ("NOTES.md", "README.md"):
        if (root / name).is_file():
            notes[name] = (root / name).read_text(errors="replace")[:2000]
    at = now()
    started = r["started_at"] or r["created_at"]
    report = {"run": r["id"], "idea": {"number": idea["id"], "title": idea["title"], "text": idea["text"][:600]},
              "engine": r["engine"], "model": r["model"] or "default", "effort": r["effort"], "status": r["status"],
              "outcome": r["outcome"], "stopReason": r["stop_reason"], "started": local(started, at),
              "duration": span((r["ended_at"] or at) - started), "tokensUsed": r["tokens_used"],
              "tokenBudget": r["budget_tokens"], "timesBlocked": r["blocked"],
              "agentLastMessage": (r["summary"] or "")[:3000] or None, "error": r["error"],
              "folder": workspace.short(root), "notes": notes, "watchLive": r["live_url"],
              "inTheApp": {"codex": "pinned in the Codex app, to continue any time; open_run opens it",
                           "claude": "moves to Claude Desktop's Code tab once done (while the user is away), to "
                                     "continue any time; open_run opens it now" if apps.desktop() else None
                           }.get(r["engine"])}
    if r["branch"]:  # an existing codebase: what's on the run's branch is its work
        report.update(project=workspace.short(r["project"]), **workspace.branch_work(r))
    else:
        report["files"] = _files(root) if root.is_dir() else []
    return report


def run_options(conn) -> dict:
    """What a run can be set to: engines with their models and efforts, defaults, and project folders."""
    cfg = config.load()
    engines = {}
    for engine in runs.ENGINES:
        catalog = models.catalog(cfg, engine)
        engines[engine] = {"available": bool(catalog), "models": [
            {"model": m["id"], "name": m["name"], "efforts": m["efforts"], "isDefault": m["default"],
             "about": m.get("description")} for m in catalog or []]}
    return {"engines": engines,
            "defaults": {"engine": "the subscription whose unused capacity expires soonest",
                         "model": "the user's default for that engine", "effort": cfg["runs"]["effort"],
                         "project": "the idea's project if it has one, else a fresh scratch folder"},
            "access": cfg["runs"]["access"], "keepPercent": cfg["runs"]["keepPercent"],
            "runsAtOncePerSubscription": cfg["runs"]["maxRunsPerEngine"],
            "projectsFolder": workspace.short(workspace.projects_root(cfg)),
            "projects": workspace.projects(cfg)}


def propose_run(conn, idea: int, engine: str | None = None, model: str | None = None, effort: str | None = None,
                project: str | None = None, budget_tokens: int | None = None, minutes: int | None = None) -> dict:
    try:
        ask = runs.propose(conn, config.load(), int(idea), engine=engine, model=model, effort=effort,
                           project=project, budget_tokens=budget_tokens, max_minutes=minutes, via="assistant")
    except runs.RunError as exc:
        return {"requested": False, "reason": str(exc)}
    return {"requested": True, "engine": ask["engine"], "model": ask["model"] or "default",
            "effort": ask["effort"], "project": ask["project"] or "scratch folder", "code": ask["code"],
            "expires": local(ask["expires_at"]),
            "note": "The user was sent the request (with the reason for this engine) and Start/Skip buttons. "
                    "Only they can start it."}


def open_run(conn, run: int) -> dict:
    r = runs.get(conn, int(run))
    return {"result": apps.open_run(conn, r) if r else f"No run #{run}."}


def get_settings(conn) -> dict:
    return {"settings": settings.listing(config.load()),
            "note": "change_setting changes one. Reset tells the user about each change, with an Undo button."}


def change_setting(conn, setting: str, value: str) -> dict:
    try:
        old, new = settings.change(conn, setting, value, by_ai=True)
    except settings.SettingError as exc:
        return {"changed": False, "reason": str(exc)}
    return {"changed": old != new, "setting": setting, "from": settings.show(setting, old),
            "to": settings.show(setting, new),
            "note": "Reset sent the user its own message about this change, with an Undo button."}


def stop_runs(conn, run: int | None = None) -> dict:
    results = runs.stop(conn, config.load(), run_id=None if run is None else int(run), reason="stop via assistant")
    if not results["runs"] and not results["declined"]:
        return {"stopped": [], "message": "Nothing was running." if run is None else f"Run #{run} wasn't running."}
    return {"stopped": [r["run"] for r in results["runs"]], "message": runs.describe_stop(results)}


def _schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


ENGINE = {"type": "string", "enum": list(ideas.ENGINES)}
PROJECT = {"type": "string", "description": "Where the work happens: a project folder name from run_options (an "
                                            "existing codebase runs on a new branch in its own worktree), a new "
                                            "folder name for a brand-new project, or empty for a scratch folder."}
TOOLS = {
    "get_usage": (get_usage, "Usage limits, reset times, one-time resets and paid-usage status for each connected AI "
                  "subscription, with how fresh the data is.",
                  _schema({"refresh": {"type": "boolean", "description": "Read live now (slower) instead of "
                                                                          "the last stored reading."}})),
    "list_ideas": (list_ideas, "The user's saved ideas, numbered.",
                   _schema({"include_finished": {"type": "boolean"}})),
    "add_idea": (add_idea, "Save a new idea in the user's words. Saving never runs it. Set project when the idea "
                 "belongs to an existing codebase or names its own new folder.",
                 _schema({"text": {"type": "string"}, "engine": ENGINE, "project": PROJECT}, ["text"])),
    "update_idea": (update_idea, "Change an idea's text, preferred engine or project. An empty string clears "
                    "engine or project.",
                    _schema({"number": {"type": "integer"}, "text": {"type": "string"}, "engine": {"type": "string"},
                             "project": {"type": "string"}}, ["number"])),
    "remove_idea": (remove_idea, "Remove (archive) an idea by number.",
                    _schema({"number": {"type": "integer"}}, ["number"])),
    "list_runs": (list_runs, "Recent runs (newest first) and pending run requests.",
                  _schema({"limit": {"type": "integer"}})),
    "get_run": (get_run, "Everything about one run: outcome, the working agent's last message, its notes and the "
                "files it made. Use this to summarize what a run got done.",
                _schema({"run": {"type": "integer"}}, ["run"])),
    "run_options": (run_options, "What a run can be set to: each engine's models and the effort levels each model "
                    "supports, the defaults, whether runs have full access, and the user's project folders. Check it "
                    "before choosing a model, effort or project.", _schema({})),
    "propose_run": (propose_run, "Ask the user to approve running an idea. They get Start/Skip buttons; you cannot "
                    "start runs yourself. Every setting is optional: leave engine out to let Reset pick the "
                    "subscription whose unused capacity expires soonest; model defaults to the user's default for "
                    "that engine; effort defaults to high (use xhigh or max for hard or long tasks, ultra only for "
                    "big coding jobs on models that support it); project defaults to the idea's project, else a "
                    "scratch folder.",
                    _schema({"idea": {"type": "integer"}, "engine": ENGINE,
                             "model": {"type": "string", "description": "A model id or name from run_options, "
                                                                        "e.g. gpt-5.6-sol or fable."},
                             "effort": {"type": "string", "enum": list(models.EFFORTS)}, "project": PROJECT,
                             "budget_tokens": {"type": "integer"}, "minutes": {"type": "integer"}}, ["idea"])),
    "open_run": (open_run, "Open a run's chat in its desktop app (Codex or Claude) on the user's Mac, so they can "
                 "read it or continue it there. A Claude run can only move to Claude Desktop once it's done.",
                 _schema({"run": {"type": "integer"}}, ["run"])),
    "get_settings": (get_settings, "Reset's settings the user can tune by chatting: each one's current value, what it "
                     "does and what it accepts.", _schema({})),
    "change_setting": (change_setting, "Change one of Reset's settings (see get_settings), only because the user "
                       "asked for it in this conversation. Reset also tells the user about the change, with an "
                       "Undo button.",
                       _schema({"setting": {"type": "string", "enum": list(settings.SETTINGS)},
                                "value": {"type": "string", "description": "The new value, e.g. \"3\" for 3%, "
                                                                           "\"on\", \"sandboxed\" or \"7, 1\"."}},
                               ["setting", "value"])),
    "stop_runs": (stop_runs, "Stop running work right away: one run by number, or everything (which also cancels "
                  "pending requests).", _schema({"run": {"type": "integer"}})),
}
NAMES = tuple(TOOLS)


def definitions() -> list:
    return [{"name": name, "description": spec[1], "inputSchema": spec[2]} for name, spec in TOOLS.items()]


def call(name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        return {"error": f"Unknown tool {name}"}
    function, _, schema = TOOLS[name]
    allowed = schema["properties"]
    unexpected = [k for k in arguments if k not in allowed]
    missing = [k for k in schema["required"] if k not in arguments]
    if unexpected or missing:
        return {"error": f"Bad arguments (unexpected {unexpected}, missing {missing})"}
    with connection() as conn:  # one connection per call, always closed
        try:
            return function(conn, **arguments)
        except Exception as exc:
            return {"error": describe(exc)}
