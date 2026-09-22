"""One supervised run: drive a single engine turn, meter tokens, enforce budget and deadline,
and on any stop interrupt the agent and then kill everything it spawned.

Started detached by runs.launch() as `python -m resetagent worker <run_id>`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

from resetagent import config, db, ideas, notify, proctree, runs
from resetagent.providers import claude as claude_provider
from resetagent.providers import codex
from resetagent.providers.common import describe
from resetagent.timeutil import now, span

ROOT = Path(__file__).resolve().parents[1]
LIMIT_TEXT = re.compile(r"(usage|rate)[ _-]?limit|limit (reached|hit)|hit your (usage )?limit|quota", re.IGNORECASE)


def instructions(workdir: str) -> str:
    return (f"This is an unattended Reset run with a fixed token budget and time limit; it may be stopped "
            f"at any moment. Work only inside {workdir}. Don't install global software or touch other "
            "folders. Nobody is watching live, so don't ask questions: make sensible assumptions and note "
            "them in NOTES.md. Prefer a small working result over a big unfinished one, and save progress "
            "to files as you go. End with a summary under 120 words of what you made and a good next "
            "step; it will be texted to the user.")


def prompt_for(idea, workdir: str) -> str:
    return f"Idea #{idea['id']} from my Reset idea list:\n\n{idea['text']}\n\n{instructions(workdir)}"


class PidTracker:
    """Records every process under the worker, so cancellation can reach ones that later detach."""

    def __init__(self, conn, run_id: int):
        self.conn, self.run_id = conn, run_id

    def track(self) -> None:
        procs = proctree.snapshot()
        me = os.getpid()
        for pid, proc in proctree.descendants(me, procs).items():
            if pid != me:
                self.conn.execute("INSERT OR IGNORE INTO run_pids(run_id, pid, lstart, command) "
                                  "VALUES (?, ?, ?, ?)", (self.run_id, pid, proc.lstart, proc.command[:200]))

    def targets(self) -> dict:
        me = os.getpid()
        return {r["pid"]: r["lstart"] for r in self.conn.execute(
            "SELECT pid, lstart FROM run_pids WHERE run_id = ?", (self.run_id,)) if r["pid"] != me}


class Engine:
    thread_id = None
    turn_id = None

    def __init__(self, cfg: dict, run, idea):
        self.cfg, self.run, self.idea = cfg, run, idea
        self.tokens_used = 0   # uncached input + output: what the budget counts
        self.tokens_total = 0  # everything the provider reported, including cache reads
        self.summary = None
        self.finished = None   # completed | stopped | failed | budget
        self.error = None


class CodexEngine(Engine):
    """One turn through `codex app-server`: sandboxed to the run folder, approvals never requested."""

    def start(self) -> None:
        executable = codex.resolve_bin(self.cfg)
        if not executable:
            raise RuntimeError("Codex CLI not found")
        workdir = self.run["workdir"]
        self.client = codex.AppServer(executable, allowed=codex.RUN_METHODS, cwd=workdir)
        codex.initialize(self.client, "reset_run")
        started = self.client.call("thread/start", {
            "cwd": workdir, "approvalPolicy": "never", "sandbox": "workspace-write",
            "developerInstructions": instructions(workdir)})
        self.thread_id = started["thread"]["id"]
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt_for(self.idea, workdir)}],
                  "serviceTierForTurn": "default"}
        if self.run["effort"]:
            params["effort"] = self.run["effort"]
        try:
            turn = self.client.call("turn/start", params)
        except RuntimeError:
            if "effort" not in params:
                raise
            # Effort is an optimization, not a limit: fall back to the model's default if it's rejected.
            del params["effort"]
            turn = self.client.call("turn/start", params)
        self.turn_id = turn["turn"]["id"]

    def pump(self, timeout: float) -> None:
        for message in self.client.events(timeout):
            method, params = message.get("method"), message.get("params") or {}
            if "id" in message:
                # Server-to-client requests: an unattended run approves nothing and answers nothing.
                if method and ("Approval" in method or method.endswith("requestApproval")):
                    self.client.respond(message["id"], {"decision": "decline"})
                else:
                    self.client.respond(message["id"], error={"code": -32601, "message": "Reset runs are unattended"})
            elif method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total") or {}
                self.tokens_total = int(total.get("totalTokens") or 0)
                self.tokens_used = max(0, self.tokens_total - int(total.get("cachedInputTokens") or 0))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    self.summary = item["text"]
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                self.finished = {"completed": "completed", "interrupted": "stopped"}.get(turn.get("status"), "failed")
                if turn.get("error"):
                    self.error = describe(RuntimeError(json.dumps(turn["error"])[:300]))
            elif method == "error" and not params.get("willRetry"):
                self.error = describe(RuntimeError(json.dumps(params.get("error"))[:300]))

    def interrupt(self, timeout: float = 5.0) -> None:
        if self.finished or not self.turn_id:
            return
        try:
            self.client.call("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=timeout)
        except Exception as exc:
            self.error = self.error or f"interrupt failed: {describe(exc)}"
        end = time.monotonic() + timeout
        while not self.finished and time.monotonic() < end:
            try:
                self.pump(0.2)
            except RuntimeError:
                break

    def close(self) -> None:
        client = getattr(self, "client", None)
        if client:
            client.close()


class StreamEngine(Engine):
    """Shared plumbing for engines that stream JSON lines on stdout."""

    def spawn(self, args: list, cwd: str) -> None:
        self.process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""

    def lines(self, timeout: float) -> list:
        out = []
        if self.selector.select(timeout):
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if chunk:
                self.buffer += chunk
            elif self.process.poll() is not None and not self.finished:
                self.finished = "failed"
                self.error = self.error or f"engine exited with code {self.process.returncode}"
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def interrupt(self, timeout: float = 5.0) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        if not self.finished:
            self.finished = "stopped"

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class ClaudeEngine(StreamEngine):
    """One `claude -p` run limited to file tools in the run folder, with a dollar cap as a second stop."""

    def start(self) -> None:
        executable = claude_provider.resolve_bin(self.cfg)
        if not executable:
            raise RuntimeError("Claude Code CLI not found")
        workdir = self.run["workdir"]
        self.usage = {}
        self.spawn([executable, "-p", prompt_for(self.idea, workdir), "--output-format", "stream-json",
                    "--verbose", "--permission-mode", "acceptEdits", "--allowedTools", "Read,Write,Edit,Glob,Grep",
                    "--max-budget-usd", str(self.cfg["runs"]["claudeMaxBudgetUsd"]),
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'], workdir)

    def pump(self, timeout: float) -> None:
        for event in self.lines(timeout):
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self.thread_id = event.get("session_id")
            elif kind == "assistant":
                message = event.get("message") or {}
                self.usage[message.get("id")] = message.get("usage") or {}
                texts = [b.get("text") for b in message.get("content") or [] if b.get("type") == "text"]
                if any(texts):
                    self.summary = "\n".join(t for t in texts if t)
            elif kind == "result":
                subtype = event.get("subtype") or ""
                self.finished = "budget" if "budget" in subtype else (
                    "completed" if subtype == "success" and not event.get("is_error") else "failed")
                self.summary = event.get("result") or self.summary
        fresh = sum(int(u.get(k) or 0) for u in self.usage.values()
                    for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
        self.tokens_used = fresh
        self.tokens_total = fresh + sum(int(u.get("cache_read_input_tokens") or 0) for u in self.usage.values())


class FakeEngine(StreamEngine):
    """Test-only engine (RESET_ALLOW_FAKE_ENGINE=1): emits token counts and spawns a detached child."""

    def start(self) -> None:
        if config.env("RESET_ALLOW_FAKE_ENGINE") != "1":
            raise RuntimeError("fake engine is disabled")
        self.spawn([sys.executable, str(ROOT / "tests" / "fake_engine.py")], self.run["workdir"])

    def pump(self, timeout: float) -> None:
        for event in self.lines(timeout):
            if "tokens" in event:
                self.tokens_used = self.tokens_total = int(event["tokens"])
            if event.get("done"):
                self.finished = "completed"
                self.summary = event.get("summary")


ENGINES = {"codex": CodexEngine, "claude": ClaudeEngine, "fake": FakeEngine}


def completion_text(run, idea, outcome: str, engine: Engine, error: str | None, at: float) -> str:
    run_id = run["id"]
    elapsed = span(at - (run["started_at"] or run["created_at"]))
    heads = {"completed": f"Run #{run_id} finished",
             "budget": f"Run #{run_id} hit its {run['budget_tokens'] / 1000:g}k-token limit and was stopped",
             "deadline": f"Run #{run_id} reached its time limit and was stopped",
             "limit": f"Run #{run_id} stopped because your {run['engine'].capitalize()} usage limit was reached",
             "failed": f"Run #{run_id} failed"}
    head = heads.get(outcome, f"Run #{run_id} ended ({outcome})")
    text = (f"{head} after {elapsed} · {engine.tokens_used / 1000:.1f}k tokens "
            f"(idea #{idea['id']}: {idea['title']}).")
    if outcome == "failed" and error:
        text += f"\nError: {error}"
    if engine.summary:
        summary = engine.summary.strip()
        text += "\n\n" + (summary if len(summary) <= 700 else summary[:697] + "…")
    workdir = run["workdir"].replace(str(Path.home()), "~", 1)
    return text + f"\n\nFiles: {workdir}"


def main(run_id: int) -> int:
    conn = db.connect()
    cfg = config.load()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or run["status"] != "starting":
        return 2  # a run is only ever started once
    idea = ideas.get(conn, run["idea_id"])
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    stamp = now()
    conn.execute("UPDATE runs SET status = 'running', started_at = ?, last_event_at = ? WHERE id = ?",
                 (stamp, stamp, run_id))
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    engine = ENGINES[run["engine"]](cfg, run, idea)
    tracker = PidTracker(conn, run_id)
    outcome = error = None
    try:
        engine.start()
        conn.execute("UPDATE runs SET thread_id = ?, turn_id = ? WHERE id = ?",
                     (engine.thread_id, engine.turn_id, run_id))
        beat = 0.0
        while outcome is None:
            engine.pump(0.5)
            t = time.time()
            if t - beat >= 2:
                beat = t
                tracker.track()
                conn.execute("UPDATE runs SET tokens_used = ?, tokens_total = ?, last_event_at = ?, "
                             "thread_id = COALESCE(thread_id, ?) WHERE id = ?",
                             (engine.tokens_used, engine.tokens_total, t, engine.thread_id, run_id))
                # Limits can be tightened while running; a stop request is honored within ~2s.
                run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if run["stop_requested_at"]:
                    stop.set()
            if engine.finished:
                outcome = engine.finished
            elif stop.is_set():
                outcome = "stopped"
            elif engine.tokens_used >= run["budget_tokens"]:
                outcome = "budget"
            elif t >= run["deadline_at"]:
                outcome = "deadline"
        if outcome in ("stopped", "budget", "deadline"):
            engine.interrupt()
    except Exception as exc:
        outcome = outcome or "failed"
        error = describe(exc)
    finally:
        if outcome in (None, "failed") and LIMIT_TEXT.search(f"{error} {engine.error}"):
            outcome = "limit"  # the provider's usage limit ended the run
        try:
            tracker.track()
        except Exception:
            pass
        engine.close()
        survivors = proctree.terminate(tracker.targets(), grace=3)
        at = now()
        conn.execute("UPDATE runs SET status = 'done', outcome = COALESCE(outcome, ?), summary = ?, "
                     "error = COALESCE(error, ?), tokens_used = ?, tokens_total = ?, ended_at = ?, cleanup = ? "
                     "WHERE id = ?", (outcome or "failed", (engine.summary or "").strip()[:4000] or None,
                                      error or engine.error, engine.tokens_used, engine.tokens_total, at,
                                      json.dumps({"workerSurvivors": survivors}), run_id))
        ideas.set_status(conn, idea["id"], "done" if outcome == "completed" else "open")
        latest = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if latest["stop_requested_at"] is None:  # user stops get their own confirmation
            notify.enqueue(conn, "run-end", completion_text(latest, idea, outcome or "failed", engine,
                                                            error or engine.error, at),
                           dedupe_key=f"run-end:{run_id}")
        runs.summarize_if_cut_short(conn, latest)
    return 0
