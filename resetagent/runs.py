"""Run requests (Ask mode) and supervised runs with enforced cancellation.

Flow: propose -> the user approves with a code -> fresh usage check -> a worker process runs one
bounded agent turn -> it ends on completion, budget, deadline or stop. Stopping never depends on
the agent: the supervisor signals every process the run spawned and verifies none survived.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

from resetagent import config, db, ideas, models, notify, proctree, status, workspace
from resetagent.providers.common import describe
from resetagent.timeutil import iso, local, now, span

ACTIVE = ("starting", "running", "stopping")
ACTIVE_SQL = "('starting', 'running', 'stopping')"
ENGINES = ("codex", "claude")
ROOT = Path(__file__).resolve().parents[1]
CHANNEL_NAMES = ("imessage", "telegram")
_launched: dict = {}  # run id -> Popen, kept so detached workers aren't garbage-collected mid-run


class RunError(Exception):
    """A run can't be proposed, approved or started. The message is shown to the user."""


def engines_allowed() -> tuple:
    return ENGINES + (("fake",) if config.env("RESET_ALLOW_FAKE_ENGINE") == "1" else ())


def engine_name(engine: str) -> str:
    return status.NAMES.get(engine, engine.capitalize())


def get(conn, run_id: int):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def active_run(conn):
    return conn.execute(f"SELECT * FROM runs WHERE status IN {ACTIVE_SQL} ORDER BY id LIMIT 1").fetchone()


def snapshot_for(cfg: dict, engine: str) -> dict:
    """A fresh live reading for the provider this engine spends. Cached data never qualifies."""
    if engine == "codex":
        return status.codex_status(cfg)
    if engine == "claude":
        return status.claude_live(cfg)
    if engine == "fake" and "fake" in engines_allowed():
        return json.loads(config.env("RESET_FAKE_SNAPSHOT", "{}"))
    raise RunError(f"Unknown engine “{engine}”.")


def check_capacity(cfg: dict, engine: str, data: dict) -> float | None:
    """Refuse unless usage is live, fully known, can't trigger paid spend, and has headroom.

    Returns the earliest reset among the provider's windows (the run must end before it).
    """
    name = engine_name(engine)
    if data.get("state") != "live":
        raise RunError(f"I can't read your current {name} usage "
                       f"({data.get('error') or data.get('state') or 'unknown'}), so I won't start anything.")
    paid = data.get("paidUsage") or {}
    if engine == "claude" and paid.get("enabled") is not False:
        raise RunError("Claude extra usage (paid) is on or unknown, so a run could cost money. Not starting.")
    if engine == "codex" and paid.get("possible") is not False:
        raise RunError("Codex could spend purchased credits (or I can't tell), so I won't start a run.")
    windows = data.get("windows") or []
    if not windows:
        raise RunError(f"{name} reported no usage windows, so I can't bound a run. Not starting.")
    unknown = [w for w in windows if w.get("remainingPercent") is None]
    if unknown:
        raise RunError(f"{name} usage for {unknown[0].get('label') or unknown[0]['id']} is unknown. Not starting.")
    floor = cfg["runs"]["minRemainingPercent"]
    for w in windows:
        if w["remainingPercent"] < floor:
            raise RunError(f"{name} {w.get('label') or w['id']} has only {w['remainingPercent']:g}% left "
                           f"(minimum {floor}%). Not starting.")
    resets = [w["resetsAt"] for w in windows if w.get("resetsAt")]
    return min(resets) if resets else None


def lease_deadline(cfg: dict, start: float, minutes: int, earliest_reset: float | None) -> float:
    deadline = start + minutes * 60
    if earliest_reset:
        deadline = min(deadline, earliest_reset - cfg["runs"]["resetMarginMinutes"] * 60)
    return deadline


def new_code(conn) -> str:
    while True:
        code = f"{secrets.randbelow(10000):04d}"
        if not conn.execute("SELECT 1 FROM asks WHERE code = ? AND status = 'pending'", (code,)).fetchone():
            return code


def usage_line(data: dict, at: float) -> str:
    bits = [f"{w.get('label') or w['id']} {w['usedPercent']:g}% used" for w in data.get("windows") or []
            if w.get("usedPercent") is not None]
    resets = [w["resetsAt"] for w in status.weekly(data) if w.get("resetsAt")]
    line = ", ".join(bits)
    return line + (f"; week resets {local(min(resets), at)}" if resets else "")


def propose(conn, cfg: dict, idea_id: int, engine: str | None = None, budget_tokens: int | None = None,
            max_minutes: int | None = None, effort: str | None = None, via: str = "cli",
            at: float | None = None, model: str | None = None, project: str | None = None):
    """Ask the user to approve a run. engine/model/effort/project are optional; each falls back sensibly."""
    at = now() if at is None else at
    idea = ideas.get(conn, idea_id)
    if idea is None or idea["status"] != "open":
        raise RunError(f"Idea #{idea_id} isn't an open idea.")
    if effort and effort.lower() not in models.EFFORTS:
        raise RunError(f"Effort must be one of: {', '.join(models.EFFORTS)}.")
    if model and not engine:
        engine = models.engine_for(model, cfg)
        if engine is None:
            raise RunError(f"I don't know which engine runs “{model}”. Say “codex” or “claude” too.")
    try:
        target = workspace.resolve(cfg, project if project is not None else idea["project"])
    except workspace.WorkspaceError as exc:
        raise RunError(str(exc)) from None
    busy = active_run(conn)
    if busy:
        raise RunError(f"Run #{busy['id']} is still going. Send “stop” first.")
    limits = cfg["runs"]
    budget = int(budget_tokens or limits["budgetTokens"])
    minutes = int(max_minutes or limits["maxMinutes"])
    if budget <= 0 or minutes <= 0:
        raise RunError("Budget and time limit must be positive.")
    skipped = []
    for candidate, reason in engine_choices(conn, cfg, idea, requested=engine, at=at):
        if candidate not in engines_allowed():
            raise RunError(f"Engine must be one of: {', '.join(ENGINES)}.")
        data = snapshot_for(cfg, candidate)
        try:
            earliest = check_capacity(cfg, candidate, data)
            if lease_deadline(cfg, at, minutes, earliest) - at < 5 * 60:
                raise RunError(f"A {engine_name(candidate)} limit resets at {local(earliest, at)}, "
                               "too soon to run safely.")
            if candidate in ENGINES:
                picked = models.choose(cfg, candidate, model, effort)
            else:
                picked = {"model": model, "effort": effort or limits["effort"], "label": "test model", "note": None}
        except models.ModelError as exc:
            if engine:
                raise RunError(str(exc)) from None
            skipped.append((candidate, str(exc)))
            continue
        except RunError as exc:
            skipped.append((candidate, str(exc)))
            continue
        engine = candidate
        if skipped:  # the first choice couldn't take a run right now
            reason = f"{engine_name(skipped[0][0])} can't take a run right now ({skipped[0][1].rstrip('.')})"
        break
    else:
        raise RunError(" ".join(dict.fromkeys(message for _, message in skipped)))
    code = new_code(conn)
    nonce = secrets.token_hex(4)  # binds a button tap to this exact request
    expires = at + limits["askMinutes"] * 60
    snapshot = json.dumps({"observedAt": data.get("observedAt"), "windows": data.get("windows")})
    ask_id = conn.execute(
        "INSERT INTO asks(code, idea_id, engine, budget_tokens, max_minutes, effort, snapshot, created_at, "
        "expires_at, nonce, model, project) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (code, idea_id, engine, budget, minutes, picked["effort"], snapshot, at, expires, nonce, picked["model"],
         str(target["path"]) if target["path"] else None)).lastrowid
    other = next((e for e in ENGINES if e != engine), None) if engine in ENGINES else None
    text = (f"Run idea #{idea_id} with {engine_name(engine)}? “{idea['title']}”\n"
            + (f"Why {engine_name(engine)}: {reason}.\n" if reason else "")
            + f"Model: {picked['label']}" + (f" · effort {picked['effort']}" if picked["effort"] else "")
            + f" · {cfg['runs']['access']} access" + (f" ({picked['note']})" if picked["note"] else "") + "\n"
            + f"Where: {target['label']}\n"
            + f"Limits: up to {budget / 1000:g}k tokens and {minutes} min; "
            f"it stops early before any limit resets.\n"
            f"{engine_name(engine)} now: {usage_line(data, at)}.\n"
            f"To start, tap Start or reply “yes {code}”; to skip, reply “no {code}”"
            + (f"; for {engine_name(other)}, reply “run {idea_id} on {other}”" if other else "")
            + f". Expires {local(expires, at)}.")
    buttons = [[{"text": f"▶️ Start on {engine_name(engine)}", "data": f"ask:{ask_id}:{nonce}:y"}],
               ([{"text": f"Use {engine_name(other)} instead", "data": f"ask:{ask_id}:{nonce}:x"}] if other else [])
               + [{"text": "Skip", "data": f"ask:{ask_id}:{nonce}:n"}]]
    notify.enqueue(conn, "ask", text, dedupe_key=f"ask:{ask_id}",
                   channel=via if via in CHANNEL_NAMES else None, expires_at=expires, buttons=buttons)
    return conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()


def at_risk(data: dict, at: float):
    """How urgently a subscription's unused weekly capacity will expire: (score, reset time, unused %) or None."""
    weekly = [w for w in status.weekly(data) if w.get("resetsAt") and w["resetsAt"] > at
              and w.get("remainingPercent") is not None]
    if data.get("state") != "live" or not weekly:
        return None
    soonest = min(w["resetsAt"] for w in weekly)
    unused = max(w["remainingPercent"] for w in weekly if w["resetsAt"] - soonest < 120)
    return unused / max((soonest - at) / 3600, 0.5), soonest, unused


def engine_choices(conn, cfg: dict, idea, requested: str | None = None, at: float | None = None) -> list:
    """Engines to try, in order, each with the reason it was picked.

    An engine the user asked for wins, then the idea's own preference. Otherwise the subscription whose
    unused capacity expires soonest goes first, so the run spends usage that would otherwise be lost.
    """
    at = now() if at is None else at
    if requested:
        return [(requested.lower(), None)]
    if idea["engine"]:
        return [(idea["engine"], f"idea #{idea['id']} is set to use {engine_name(idea['engine'])}")]
    providers = (status.latest(conn) or {}).get("providers") or {}
    ranked = []
    for engine in ENGINES:
        risk = at_risk(providers.get(engine) or {}, at)
        if risk:
            score, reset, unused = risk
            ranked.append((score, engine, f"its week resets {local(reset, at)} with {unused:g}% unused"))
    ranked.sort(reverse=True)
    choices = [(engine, reason) for _, engine, reason in ranked]
    return choices + [(engine, None) for engine in ENGINES if engine not in {c[0] for c in choices}]


def switch(conn, cfg: dict, code: str, via: str):
    """Replace a pending request with the same idea on the other engine."""
    ask = conn.execute("SELECT * FROM asks WHERE code = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                       (code,)).fetchone()
    if ask is None:
        raise RunError(f"No pending request with code {code}.")
    other = next((e for e in ENGINES if e != ask["engine"]), None)
    if ask["engine"] not in ENGINES or other is None:
        raise RunError("There's no other engine to switch to.")
    # Create the new request first: if it can't be made, the original stays pending.
    replacement = propose(conn, cfg, ask["idea_id"], engine=other, budget_tokens=ask["budget_tokens"],
                          max_minutes=ask["max_minutes"], effort=ask["effort"], via=via,
                          project=ask["project"] or "")
    conn.execute("UPDATE asks SET status = 'declined', decided_at = ?, decided_via = ? WHERE id = ?",
                 (now(), f"switched to {other}", ask["id"]))
    return replacement


def decline(conn, code: str, via: str) -> bool:
    cursor = conn.execute("UPDATE asks SET status = 'declined', decided_at = ?, decided_via = ? "
                          "WHERE code = ? AND status = 'pending'", (now(), via, code))
    return cursor.rowcount > 0


def expire_asks(conn, at: float | None = None) -> None:
    conn.execute("UPDATE asks SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                 (now() if at is None else at,))


def _fail_ask(conn, ask_id: int) -> None:
    conn.execute("UPDATE asks SET status = 'failed', decided_at = ? WHERE id = ? AND status = 'pending'",
                 (now(), ask_id))


def approve(conn, cfg: dict, code: str, via: str, at: float | None = None):
    """Start the run a pending request describes. The caller must be the user (channel or TTY)."""
    at = now() if at is None else at
    ask = conn.execute("SELECT * FROM asks WHERE code = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                       (code,)).fetchone()
    if ask is None:
        raise RunError(f"No pending request with code {code}.")
    if at > ask["expires_at"]:
        conn.execute("UPDATE asks SET status = 'expired' WHERE id = ?", (ask["id"],))
        raise RunError(f"Request {code} expired at {local(ask['expires_at'], at)}. "
                       f"Send “run {ask['idea_id']}” to ask again.")
    idea = ideas.get(conn, ask["idea_id"])
    if idea is None or idea["status"] != "open":
        _fail_ask(conn, ask["id"])
        raise RunError(f"Idea #{ask['idea_id']} is no longer open.")
    data = snapshot_for(cfg, ask["engine"])
    try:
        earliest = check_capacity(cfg, ask["engine"], data)
    except RunError:
        _fail_ask(conn, ask["id"])
        raise
    # An approval covers the allowance window it was requested in, never a newly reset one.
    before = {w["id"]: w.get("resetsAt") for w in json.loads(ask["snapshot"]).get("windows") or []}
    for w in data.get("windows") or []:
        old = before.get(w["id"])
        if old and w.get("resetsAt") and abs(w["resetsAt"] - old) > 120:
            _fail_ask(conn, ask["id"])
            raise RunError("A usage limit reset after this request was made, so the approval no longer applies. "
                           f"Send “run {ask['idea_id']}” to ask again.")
    deadline = lease_deadline(cfg, at, ask["max_minutes"], earliest)
    if deadline - at < 5 * 60:
        _fail_ask(conn, ask["id"])
        raise RunError(f"A limit resets at {local(earliest, at)}, too soon to run safely.")
    try:
        prepared = workspace.prepare(cfg, idea, workspace.resolve(cfg, ask["project"] or ""))
    except workspace.WorkspaceError as exc:
        _fail_ask(conn, ask["id"])
        raise RunError(str(exc)) from None
    with db.transaction(conn):
        if conn.execute(f"SELECT 1 FROM runs WHERE status IN {ACTIVE_SQL}").fetchone():
            raise RunError("Another run is already going. Send “stop” first.")
        claimed = conn.execute("UPDATE asks SET status = 'approved', decided_at = ?, decided_via = ? "
                               "WHERE id = ? AND status = 'pending'", (at, via, ask["id"]))
        if claimed.rowcount != 1:
            raise RunError("That request was already answered.")
        run_id = conn.execute(
            "INSERT INTO runs(ask_id, idea_id, engine, workdir, budget_tokens, deadline_at, effort, status, "
            "created_at, model, project, workspace, branch) VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?)",
            (ask["id"], idea["id"], ask["engine"], str(prepared["workdir"]), ask["budget_tokens"], deadline,
             ask["effort"], at, ask["model"], str(prepared["project"]) if prepared["project"] else None,
             prepared["kind"], prepared["branch"])).lastrowid
        conn.execute("UPDATE asks SET run_id = ? WHERE id = ?", (run_id, ask["id"]))
        ideas.set_status(conn, idea["id"], "running")
    launch(conn, run_id)
    return get(conn, run_id)


def launch(conn, run_id: int) -> None:
    logs = config.home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, RESET_HOME=str(config.home()))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH")) if p)
    with open(logs / f"run-{run_id}.log", "ab") as log:
        try:
            # A new session makes the worker its own process-group leader.
            process = subprocess.Popen([sys.executable, "-m", "resetagent", "worker", str(run_id)],
                                       cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True, close_fds=True)
        except OSError as exc:
            conn.execute("UPDATE runs SET status = 'done', outcome = 'failed', error = ?, ended_at = ? "
                         "WHERE id = ?", (describe(exc), now(), run_id))
            raise RunError("Couldn't start the run.") from None
    for finished in [key for key, proc in _launched.items() if proc.poll() is not None]:
        del _launched[finished]
    _launched[run_id] = process
    start = None
    for _ in range(40):
        proc = proctree.snapshot().get(process.pid)
        if proc:
            start = proc.lstart
            break
        time.sleep(0.05)
    conn.execute("UPDATE runs SET worker_pid = ?, worker_lstart = ? WHERE id = ?", (process.pid, start, run_id))


def run_targets(conn, run_id: int, pid: int | None, start: str | None) -> dict:
    """Every process the run was seen to spawn, plus whatever is under the worker right now."""
    targets = {r["pid"]: r["lstart"] for r in conn.execute("SELECT pid, lstart FROM run_pids WHERE run_id = ?",
                                                            (run_id,))}
    procs = proctree.snapshot()
    if proctree.alive(pid, start, procs):
        for child, proc in proctree.descendants(pid, procs).items():
            targets.setdefault(child, proc.lstart)
        targets[pid] = start
    return targets


def enforce_stop(conn, cfg: dict, run, reason: str) -> dict:
    run_id = run["id"]
    conn.execute(f"UPDATE runs SET status = 'stopping', stop_requested_at = COALESCE(stop_requested_at, ?), "
                 f"stop_reason = COALESCE(stop_reason, ?) WHERE id = ? AND status IN {ACTIVE_SQL}",
                 (now(), reason, run_id))
    pid, start = run["worker_pid"], run["worker_lstart"]
    # 1. Let the worker wind down: it interrupts the agent's turn and stops its engine.
    if proctree.alive(pid, start):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + cfg["runs"]["stopGraceSeconds"]
        while time.monotonic() < deadline and proctree.alive(pid, start):
            proctree.reap()
            time.sleep(0.25)
    # 2. Enforce regardless of cooperation, then verify.
    targets = run_targets(conn, run_id, pid, start)
    survivors = proctree.terminate(targets, grace=2)
    return finalize(conn, run_id, survivors, len(targets), default_outcome="stopped")


def finalize(conn, run_id: int, survivors: list, tracked: int, default_outcome: str,
             error: str | None = None) -> dict:
    at = now()
    cleanup = {"verifiedAt": iso(at), "processesTracked": tracked, "survivors": survivors}
    conn.execute("UPDATE runs SET status = 'done', outcome = COALESCE(outcome, ?), error = COALESCE(error, ?), "
                 "ended_at = COALESCE(ended_at, ?), verified_at = ?, cleanup = ? WHERE id = ?",
                 (default_outcome, error, at, at, json.dumps(cleanup), run_id))
    launched = _launched.get(run_id)
    if launched is not None and launched.poll() is not None:
        del _launched[run_id]
    run = get(conn, run_id)
    idea = ideas.get(conn, run["idea_id"])
    if idea and idea["status"] == "running":
        ideas.set_status(conn, idea["id"], "done" if run["outcome"] == "completed" else "open")
    summarize_if_cut_short(conn, run)
    return {"run": run_id, "outcome": run["outcome"], "verified": not survivors, "survivors": survivors,
            "tokens": run["tokens_used"], "elapsed": (run["ended_at"] or at) - (run["started_at"] or run["created_at"])}


def summarize_if_cut_short(conn, run) -> None:
    """A run that finishes writes its own summary; one that was cut off gets one from the brain."""
    if run["outcome"] != "completed" and (run["tokens_used"] or 0) > 0:
        from resetagent import brain  # late import: brain -> tools -> runs

        brain.queue_summary(conn, config.load(), run["id"])


def stop(conn, cfg: dict, run_id: int | None = None, reason: str = "user") -> dict:
    """Stop one run, or every run plus every pending request."""
    declined = []
    if run_id is None:
        for ask in conn.execute("SELECT id, code FROM asks WHERE status = 'pending'").fetchall():
            conn.execute("UPDATE asks SET status = 'declined', decided_at = ?, decided_via = 'stop' WHERE id = ?",
                         (now(), ask["id"]))
            declined.append(ask["code"])
        targets = conn.execute(f"SELECT * FROM runs WHERE status IN {ACTIVE_SQL}").fetchall()
    else:
        run = get(conn, run_id)
        targets = [run] if run is not None and run["status"] in ACTIVE else []
    return {"runs": [enforce_stop(conn, cfg, run, reason) for run in targets], "declined": declined}


def describe_stop(results: dict) -> str:
    lines = []
    for r in results["runs"]:
        line = f"Stopped run #{r['run']} after {span(r['elapsed'])} · {r['tokens'] / 1000:.1f}k tokens used."
        if r["verified"]:
            line += " Verified: nothing from the run is still running."
        else:
            line += f" Warning: {len(r['survivors'])} process(es) survived; check `resetctl runs`."
        lines.append(line)
    if results["declined"]:
        lines.append("Cancelled pending request" + ("s " if len(results["declined"]) > 1 else " ")
                     + ", ".join(results["declined"]) + ".")
    return "\n".join(lines)


def supervise(conn, cfg: dict, at: float | None = None) -> None:
    """Independent enforcement: dead workers, overdue or over-budget runs, and unverified cleanups."""
    at = now() if at is None else at
    proctree.reap()
    procs = proctree.snapshot()
    grace = cfg["runs"]["stopGraceSeconds"]
    for run in conn.execute(f"SELECT * FROM runs WHERE status IN {ACTIVE_SQL}").fetchall():
        if not proctree.alive(run["worker_pid"], run["worker_lstart"], procs):
            if run["status"] == "starting" and at - run["created_at"] < 30:
                continue  # still launching
            targets = run_targets(conn, run["id"], None, None)
            survivors = proctree.terminate(targets, grace=2)
            if run["status"] == "stopping":
                finalize(conn, run["id"], survivors, len(targets), default_outcome="stopped")
                continue
            result = finalize(conn, run["id"], survivors, len(targets), default_outcome="failed",
                              error="worker exited unexpectedly")
            if result["outcome"] == "failed":
                notify.enqueue(conn, "run-end", f"Run #{run['id']} stopped unexpectedly. Nothing from it is "
                               "running now.", dedupe_key=f"run-end:{run['id']}")
            continue
        last = run["last_event_at"] or run["started_at"] or run["created_at"]
        if at > run["deadline_at"] + grace + 30:
            enforce_stop(conn, cfg, run, "deadline passed (supervisor)")
        elif run["tokens_used"] > run["budget_tokens"] * 1.25:
            enforce_stop(conn, cfg, run, "over budget (supervisor)")
        elif at - last > 300:
            enforce_stop(conn, cfg, run, "worker stopped reporting (supervisor)")
    for run in conn.execute("SELECT id FROM runs WHERE status = 'done' AND verified_at IS NULL").fetchall():
        targets = run_targets(conn, run["id"], None, None)
        survivors = proctree.terminate(targets, grace=2)
        finalize(conn, run["id"], survivors, len(targets), default_outcome="failed")


def reconcile_on_start(conn, cfg: dict) -> list:
    """After a restart, a run's lease can't be revalidated, so every active run is stopped."""
    return [enforce_stop(conn, cfg, run, "Reset restarted; runs default to stopped")
            for run in conn.execute(f"SELECT * FROM runs WHERE status IN {ACTIVE_SQL}").fetchall()]


def summary_text(conn, at: float | None = None) -> str:
    at = now() if at is None else at
    lines = []
    for run in conn.execute(f"SELECT * FROM runs WHERE status IN {ACTIVE_SQL} ORDER BY id").fetchall():
        started = run["started_at"] or run["created_at"]
        lines.append(f"Run #{run['id']} ({engine_name(run['engine'])}, idea #{run['idea_id']}) {run['status']} · "
                     f"{span(at - started)} · {run['tokens_used'] / 1000:.1f}k of {run['budget_tokens'] / 1000:g}k "
                     f"tokens · stops by {local(run['deadline_at'], at)}")
    for ask in conn.execute("SELECT * FROM asks WHERE status = 'pending' ORDER BY id").fetchall():
        lines.append(f"Request {ask['code']}: idea #{ask['idea_id']} with {engine_name(ask['engine'])}, "
                     f"expires {local(ask['expires_at'], at)}")
    recent = conn.execute("SELECT * FROM runs WHERE status = 'done' ORDER BY id DESC LIMIT 3").fetchall()
    if recent:
        lines.append("Recent: " + ", ".join(f"#{r['id']} {r['outcome']}" for r in recent))
    return "\n".join(lines) or "Nothing is running and there are no pending requests."
