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
import uuid

from resetagent import apps, config, db, ideas, notify, proctree, runs, workspace
from resetagent.providers import claude as claude_provider
from resetagent.providers import codex
from resetagent.providers.common import describe
from resetagent.timeutil import now, span

ROOT = Path(__file__).resolve().parents[1]
LIMIT_TEXT = re.compile(r"(usage|rate)[ _-]?limit|limit (reached|hit)|hit your (usage )?limit|quota", re.IGNORECASE)
# What a command's output looks like when a sandbox blocked it (files outside the folder, or the network).
SANDBOX_BLOCK = re.compile(r"operation not permitted|read-only file system|could not resolve host|network is "
                           r"unreachable|name resolution|nodename nor servname|ENOTFOUND|EAI_AGAIN", re.IGNORECASE)


def shown(command) -> str:
    """A command as the agent wrote it, without the shell wrapper Codex adds (`/bin/zsh -lc '…'`)."""
    command = " ".join(command) if isinstance(command, list) else str(command or "a command")
    match = re.fullmatch(r"\S*?(?:ba|z)?sh -l?c (['\"])(.*)\1", command.strip(), re.S)
    return (match.group(2) if match else command)[:200]


def task_for(idea) -> str:
    return f"Idea #{idea['id']} from my Reset idea list:\n\n{idea['text']}"


UNATTENDED_ANSWER = ("Nobody can answer right now. Make the most reasonable choice yourself, keep going, and mention "
                     "the choice in your final summary.")


def access_hint(cfg: dict) -> str:
    if cfg["runs"]["access"] == "full":
        return "It happened even with full access, so it was refused."
    return "To let runs do this without asking, switch to full access: resetctl setup access --access full"


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
    stopped_in_app = False  # someone pressed stop in the app the run was showing in

    def __init__(self, cfg: dict, run, idea):
        self.cfg, self.run, self.idea = cfg, run, idea
        self.tokens_used = 0   # uncached input + output: what the budget counts
        self.tokens_total = 0  # everything the provider reported, including cache reads
        self.summary = None
        self.finished = None   # completed | stopped | failed | budget
        self.error = None
        self.alerts = []       # things the user should hear about while the run goes on (e.g. it was blocked)
        self.reported = set()  # what was already alerted, so a retried command isn't reported twice
        self.notices = []      # other news for the user, like where to watch the run live
        self.noticed = 0
        self.live_url = None

    def who(self) -> str:
        return f"Run #{self.run['id']} ({self.run['engine'].capitalize()})"


class CodexEngine(Engine):
    """One turn through `codex app-server`.

    Full access: no sandbox and no approval requests. Sandboxed: Reset spots commands the sandbox blocked from
    their output, and declines at once when Codex asks to go further. Either way the user hears about it, and
    the run never waits.
    """

    def start(self) -> None:
        executable = codex.resolve_bin(self.cfg)
        if not executable:
            raise RuntimeError("Codex CLI not found")
        workdir = self.run["workdir"]
        self.client = codex.AppServer(executable, allowed=codex.RUN_METHODS, cwd=workdir)
        codex.initialize(self.client, "reset_run")
        access = self.cfg["runs"]["access"]
        full = access == "full"
        thread = {"cwd": workdir, "developerInstructions": workspace.brief(self.run, access),
                  "approvalPolicy": "never" if full else "on-request",
                  "sandbox": "danger-full-access" if full else "workspace-write"}
        if self.run["model"]:
            thread["model"] = self.run["model"]
        started = self.client.call("thread/start", thread)
        self.thread_id = started["thread"]["id"]
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": task_for(self.idea)}],
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
        self.show_in_app()

    named = pinned = False  # what show_in_app managed, so the finished message says only what's true

    def show_in_app(self) -> None:
        """Name the thread and pin it, so the run shows in the Codex app's sidebar. Never fails the run."""
        if not self.cfg["runs"]["showInApps"]:
            return
        try:
            self.client.call("thread/name/set", {"threadId": self.thread_id, "name": apps.title(self.run, self.idea)},
                             timeout=10)
            self.named = True
        except Exception as exc:  # an older Codex: the run goes on regardless
            print(f"thread/name/set: {describe(exc)}", file=sys.stderr, flush=True)
        try:
            sections = (self.client.call("threadSection/list", {}, timeout=10) or {}).get("data") or []
            section = next((s["id"] for s in sections if s.get("name") == apps.PINNED), None)
            if section:
                self.client.call("thread/section/move", {"threadId": self.thread_id, "sectionId": section,
                                                         "beforeThreadId": None}, timeout=10)
                self.pinned = True
        except Exception as exc:
            print(f"pinning: {describe(exc)}", file=sys.stderr, flush=True)

    def pump(self, timeout: float) -> None:
        for message in self.client.events(timeout):
            method, params = message.get("method"), message.get("params") or {}
            if "id" in message:
                self.answer(message["id"], method or "", params)
            elif method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total") or {}
                self.tokens_total = int(total.get("totalTokens") or 0)
                self.tokens_used = max(0, self.tokens_total - int(total.get("cachedInputTokens") or 0))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    self.summary = item["text"]
                elif (item.get("type") == "commandExecution" and self.cfg["runs"]["access"] != "full"
                      and SANDBOX_BLOCK.search(item.get("aggregatedOutput") or "")):
                    command = shown(item.get("command"))
                    if command not in self.reported:
                        self.reported.add(command)
                        self.alerts.append(f"{self.who()} was blocked by its sandbox running `{command}`. "
                                           f"{access_hint(self.cfg)}")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                self.finished = {"completed": "completed", "interrupted": "stopped"}.get(turn.get("status"), "failed")
                if turn.get("error"):
                    self.error = describe(RuntimeError(json.dumps(turn["error"])[:300]))
            elif method == "error" and not params.get("willRetry"):
                self.error = describe(RuntimeError(json.dumps(params.get("error"))[:300]))

    def answer(self, request_id, method: str, params: dict) -> None:
        """Requests from Codex to a person. Nobody is watching a run, so answer or refuse, and tell the user."""
        who, hint = self.who(), access_hint(self.cfg)
        why = f" ({params['reason'][:200]})" if params.get("reason") else ""
        # v2 approvals answer "decline"; the older v1 ones answer "denied".
        refuse = {"decision": "decline" if method.startswith("item/") else "denied"}
        if method == "item/tool/requestUserInput":
            questions = params.get("questions") or []
            self.client.respond(request_id, {"answers": {q.get("id"): {"answers": [UNATTENDED_ANSWER]}
                                                         for q in questions}})
            asked = " / ".join(q.get("question") or q.get("header") or "" for q in questions).strip()
            self.alerts.append(f"{who} asked: “{asked[:300]}”. Nobody could answer in time, so I told it to "
                               "decide itself; its choice will be in the summary.")
        elif method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            self.client.respond(request_id, refuse)
            command = shown(params.get("command"))
            self.reported.add(command)
            self.alerts.append(f"{who} asked to run `{command}`{why} and was refused. {hint}")
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            self.client.respond(request_id, refuse)
            self.alerts.append(f"{who} asked to change files{why} and was refused. {hint}")
        elif method == "item/permissions/requestApproval":
            self.client.respond(request_id, {"permissions": {}})  # grants nothing extra
            self.alerts.append(f"{who} asked for extra permissions{why} and was refused. {hint}")
        elif method == "mcpServer/elicitation/request":
            self.client.respond(request_id, {"action": "decline"})
            self.alerts.append(f"{who}: the {params.get('serverName') or 'connected'} tool asked for input, "
                               "which a run can't give, so it was declined.")
        else:
            self.client.respond(request_id, error={"code": -32601, "message": "Reset runs are unattended"})
            self.alerts.append(f"{who} sent a request Reset doesn't handle ({method}), so it was refused.")

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

    def spawn(self, args: list, cwd: str, stdin=subprocess.DEVNULL) -> None:
        self.process = subprocess.Popen(args, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE,
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

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, ValueError):
            pass  # the engine has gone; reading its output reports that

    input_closed = False

    def end_input(self) -> None:
        self.input_closed = True
        stdin = getattr(getattr(self, "process", None), "stdin", None)
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process and process.stdin is not None and not self.input_closed:
            self.end_input()  # an engine that reads its input exits on its own once the input ends
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


REFUSED = re.compile(r"requires approval|requested permissions? to use|haven't granted", re.IGNORECASE)
# Tools that start work outside the run (scheduled, remote), which stopping the run couldn't reach.
OUTLIVES_RUN = "RemoteTrigger,CronCreate,ScheduleWakeup"


class ClaudeEngine(StreamEngine):
    """One Claude Code turn over stream-json. Full access skips permission prompts; sandboxed pre-approves file
    tools only. When runs show in the apps, Remote Control is on, so the run can be watched live in the Claude
    app and on the phone."""

    RECONNECT_AFTER = 15  # seconds a dropped live view gets to come back by itself before Reset steps in
    RECONNECT_TRIES = 3

    def __init__(self, *args):
        super().__init__(*args)
        self.usage, self.tool_uses, self.waiting, self.requests = {}, {}, {}, 0
        # The session id, chosen up front. It becomes the run's chat (thread_id) once Claude Code has started it,
        # so a run that never got going has no chat to open or hand over.
        self.session_id = str(uuid.uuid4())
        self.bridge_id = None    # the live session in the Claude apps, reattached if the connection drops
        self.dropped_at = None   # when the live view dropped, while it's down
        self.reconnects = 0
        self.last_event_at = time.monotonic()  # when the chat last did something

    def args(self, executable: str) -> list:
        args = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--session-id", self.session_id, "--name", apps.title(self.run, self.idea),
                "--append-system-prompt", workspace.brief(self.run, self.cfg["runs"]["access"]),
                "--max-budget-usd", str(self.cfg["runs"]["claudeMaxBudgetUsd"]),
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disallowedTools", OUTLIVES_RUN]
        if self.cfg["runs"]["access"] == "full":
            args += ["--permission-mode", "bypassPermissions"]
        else:
            args += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Write,Edit,Glob,Grep"]
        if self.run["model"]:
            args += ["--model", self.run["model"]]
        if self.run["effort"]:
            args += ["--effort", self.run["effort"]]
        return args

    def start(self) -> None:
        executable = claude_provider.resolve_bin(self.cfg)
        if not executable:
            raise RuntimeError("Claude Code CLI not found")
        self.spawn(self.args(executable), self.run["workdir"], stdin=subprocess.PIPE)
        self.control("initialize")
        if self.cfg["runs"]["showInApps"]:
            # The live view ends with the run; the finished chat moves to Claude Desktop (apps.sweep).
            self.control("remote_control", enabled=True, name=apps.title(self.run, self.idea))
            end = time.monotonic() + 45  # the live link should go out before the work starts
            while "remote_control" in self.waiting.values() and time.monotonic() < end and not self.finished:
                self.pump(0.5)
        self.send({"type": "user", "message": {"role": "user", "content": task_for(self.idea)}})

    def control(self, subtype: str, **fields) -> None:
        self.requests += 1
        request_id = f"reset-{self.requests}"
        self.waiting[request_id] = subtype
        self.send({"type": "control_request", "request_id": request_id, "request": {"subtype": subtype, **fields}})

    def answered(self, body: dict) -> None:
        if self.waiting.pop(body.get("request_id"), None) != "remote_control":
            return
        response = body.get("response") or {}
        url = response.get("session_url")
        if self.reconnects:  # Reset turned Remote Control back on after the live view dropped
            if body.get("subtype") == "success" and url and self.dropped_at is not None:
                self.back(url)
            return
        if body.get("subtype") == "success" and url:
            self.live_url, self.bridge_id = url, response.get("bridge_session_id")
            self.notices.append(f"Watch run #{self.run['id']} live in the Claude app (Code tab), or on your "
                                f"phone: {url}")
        else:
            reason = str(body.get("error") or "no link came back")[:160]
            self.notices.append(f"{self.who()} can't be watched live because Remote Control didn't turn on "
                                f"({reason})." + (" When it's done, tap Open in Claude to see it." if apps.desktop()
                                                  else ""))

    def refused(self, tool_use_id, name: str, data: dict) -> None:
        if tool_use_id is not None and tool_use_id in self.reported:
            return
        self.reported.add(tool_use_id)
        detail = data.get("command") or data.get("file_path") or data.get("url")
        what = f"{name} (`{str(detail)[:150]}`)" if detail else name
        self.alerts.append(f"{self.who()} wasn't allowed to use {what}. {access_hint(self.cfg)}")

    def pump(self, timeout: float) -> None:
        for event in self.lines(timeout):
            kind = event.get("type")
            if kind == "keep_alive":  # a heartbeat, which the protocol says to ignore
                continue
            if kind != "control_response" and event.get("subtype") != "bridge_state":
                self.last_event_at = time.monotonic()  # the chat is active (connection news doesn't count)
            if kind == "system" and event.get("subtype") == "init":
                self.thread_id = event.get("session_id") or self.session_id
            elif kind == "system" and event.get("subtype") == "bridge_state":
                self.bridge(event.get("state"), event.get("detail"))
            elif kind == "control_response":
                self.answered(event.get("response") or {})
            elif kind == "control_request":  # Claude Code asking its host something; nobody is there to answer
                self.send({"type": "control_response", "response": {
                    "subtype": "error", "request_id": event.get("request_id"), "error": "Reset runs are unattended"}})
            elif kind == "assistant":
                message = event.get("message") or {}
                self.usage[message.get("id")] = message.get("usage") or {}
                blocks = message.get("content") or []
                self.tool_uses.update({b.get("id"): (b.get("name") or "a tool", b.get("input") or {})
                                       for b in blocks if b.get("type") == "tool_use"})
                texts = [b.get("text") for b in blocks if b.get("type") == "text"]
                if any(texts):
                    self.summary = "\n".join(t for t in texts if t)
            elif kind == "user":  # tool results: a refusal shows up here as it happens
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") \
                            and REFUSED.search(str(block.get("content"))):
                        self.refused(block.get("tool_use_id"), *self.tool_uses.get(block.get("tool_use_id"),
                                                                                  ("a tool", {})))
            elif kind == "result":
                subtype = event.get("subtype") or ""
                # someone pressed stop in the Claude app while watching the run live
                self.stopped_in_app = str(event.get("terminal_reason") or "").startswith("aborted")
                self.finished = "budget" if "budget" in subtype else (
                    "completed" if subtype == "success" and not event.get("is_error") else
                    "stopped" if self.stopped_in_app else "failed")
                self.summary = event.get("result") or self.summary
                for denial in event.get("permission_denials") or []:
                    self.refused(denial.get("tool_use_id"), denial.get("tool_name") or "a tool",
                                 denial.get("tool_input") or {})
        fresh = sum(int(u.get(k) or 0) for u in self.usage.values()
                    for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
        self.tokens_used = fresh
        self.tokens_total = fresh + sum(int(u.get("cache_read_input_tokens") or 0) for u in self.usage.values())
        self.reconnect()

    def bridge(self, state, detail) -> None:
        """Claude Code reports its Remote Control connection as it changes; keep the live view up."""
        if self.input_closed or not self.live_url:  # the session is closing, or it was never live
            return
        if state == "policy_disabled":
            # Nothing to retry, and the chat isn't live in the app any more (so it isn't held open either).
            self.dropped_at, self.live_url = None, None
            self.notices.append(f"Remote Control was turned off for your account, so run #{self.run['id']} can't be "
                                "watched live any more. It keeps going, and its result will come here.")
        elif self.dropped_at is None and self.reconnects >= self.RECONNECT_TRIES:
            return  # Reset's reconnect tries are used up: from here on the live view is left as it is
        elif state == "failed" and self.dropped_at is None:
            self.dropped_at = time.monotonic()
            self.notices.append(f"The live view of run #{self.run['id']} dropped ({detail or 'no reason given'}). "
                                "The run keeps going; I'm reconnecting it.")
        elif state == "connected" and self.dropped_at is not None:
            self.back(self.live_url)

    def back(self, url: str) -> None:
        self.dropped_at, self.live_url = None, url
        self.notices.append(f"The live view of run #{self.run['id']} is back: {url}")

    def reconnect(self) -> None:
        """If the live view hasn't come back by itself, turn Remote Control on again for the same chat."""
        if self.dropped_at is None or self.input_closed or "remote_control" in self.waiting.values():
            return
        if time.monotonic() - self.dropped_at < self.RECONNECT_AFTER:
            return
        if self.reconnects >= self.RECONNECT_TRIES:
            self.dropped_at = None
            self.notices.append(f"I couldn't reconnect the live view of run #{self.run['id']}. The run keeps going, "
                                "and its result will come here.")
            return
        self.reconnects += 1
        self.dropped_at = time.monotonic()  # the next try waits again
        self.control("remote_control", enabled=True, name=apps.title(self.run, self.idea),
                     **({"reattach_session_id": self.bridge_id} if self.bridge_id else {}))


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


ALERTS_PER_RUN = 3
HOLD_CHECK_SECONDS = 2   # how often a held-open chat checks whether you've left the Claude app
HOLD_QUIET_SECONDS = 30  # a chat you're still talking to stays open until it has been quiet this long


def announce(conn, run_id: int, idea, engine: Engine) -> None:
    """The work is done: record it and send the finished message, even though the chat stays open a while."""
    conn.execute("UPDATE runs SET outcome = 'completed', summary = ?, tokens_used = ?, tokens_total = ?, "
                 "thread_id = COALESCE(thread_id, ?) WHERE id = ?",
                 ((engine.summary or "").strip()[:4000] or None, engine.tokens_used, engine.tokens_total,
                  engine.thread_id, run_id))
    ideas.set_status(conn, idea["id"], "done")
    latest = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    notify.enqueue(conn, "run-end", completion_text(latest, idea, "completed", engine, None, now(), held=True),
                   dedupe_key=f"run-end:{run_id}", buttons=apps.button(latest))


def hold(conn, run_id: int, engine: Engine, stop: threading.Event, tracker: PidTracker) -> str | None:
    """Keep a finished run's live chat open while you're in the Claude app, so it never vanishes in front of you.

    It closes (and then moves to Claude Desktop) once you've left the app and the chat has gone quiet, or when you
    ask for it, stop it, start another run, or its time or token limit is reached. Anything said in it meanwhile
    runs in this same process, under the same limits. Returns "stopped", "deadline" or "budget" when that's why
    it closed.
    """
    checked = 0.0
    while engine.process.poll() is None:
        if stop.is_set():
            return "stopped"
        engine.pump(0.5)
        raise_alerts(conn, run_id, engine)
        t = time.time()
        if t - checked < HOLD_CHECK_SECONDS:
            continue
        checked = t
        tracker.track()
        conn.execute("UPDATE runs SET tokens_used = ?, tokens_total = ?, last_event_at = ?, live_url = ? WHERE id = ?",
                     (engine.tokens_used, engine.tokens_total, t, engine.live_url, run_id))
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run["stop_requested_at"]:
            return "stopped"
        if t >= run["deadline_at"]:
            return "deadline"
        if engine.tokens_used >= run["budget_tokens"]:
            return "budget"
        if run["handoff"] == "requested" or not engine.live_url:  # asked for, or no longer live (policy)
            return None
        if time.monotonic() - engine.last_event_at >= HOLD_QUIET_SECONDS and apps.left_claude():
            return None
    return None


def raise_alerts(conn, run_id: int, engine: Engine) -> None:
    """Tell the user each time a run is refused something or asks a question, up to a few messages per run."""
    while engine.alerts:
        text = engine.alerts.pop(0)
        conn.execute("UPDATE runs SET blocked = blocked + 1 WHERE id = ?", (run_id,))
        count = conn.execute("SELECT blocked FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        if count > ALERTS_PER_RUN:
            continue
        if count == ALERTS_PER_RUN:
            text += " I won't message about more of these for this run; its final message will count them."
        notify.enqueue(conn, "run-blocked", text, dedupe_key=f"run-blocked:{run_id}:{count}")
    while engine.notices:
        engine.noticed += 1
        notify.enqueue(conn, "run-note", engine.notices.pop(0), dedupe_key=f"run-note:{run_id}:{engine.noticed}")


def completion_text(run, idea, outcome: str, engine: Engine, error: str | None, at: float,
                    held: bool = False) -> str:
    """The finished message. held: the run's live chat stays open for now (worker.hold)."""
    run_id = run["id"]
    elapsed = span(at - (run["started_at"] or run["created_at"]))
    heads = {"completed": f"Run #{run_id} finished",
             "budget": f"Run #{run_id} hit its {run['budget_tokens'] / 1000:g}k-token limit and was stopped",
             "deadline": f"Run #{run_id} reached its time limit and was stopped",
             "limit": f"Run #{run_id} stopped because your {run['engine'].capitalize()} usage limit was reached",
             "stopped": f"Run #{run_id} was stopped" + (" from the Claude app" if engine.stopped_in_app else ""),
             "failed": f"Run #{run_id} failed"}
    head = heads.get(outcome, f"Run #{run_id} ended ({outcome})")
    text = (f"{head} after {elapsed} · {engine.tokens_used / 1000:.1f}k tokens "
            f"(idea #{idea['id']}: {idea['title']}).")
    if outcome == "failed" and error:
        text += f"\nError: {error}"
    if run["blocked"]:
        text += (f"\nIt was refused something or asked a question {run['blocked']} time"
                 + ("s." if run["blocked"] > 1 else ".") + " See the alerts above.")
    if engine.summary:
        summary = engine.summary.strip()
        text += "\n\n" + (summary if len(summary) <= 700 else summary[:697] + "…")
    if run["branch"]:
        text += (f"\n\nBranch {run['branch']} of {workspace.short(run['project'])}, "
                 f"checked out at {workspace.short(run['workdir'])}")
    else:
        text += f"\n\nFiles: {workspace.short(run['workdir'])}"
    if run["engine"] == "codex" and run["thread_id"] and engine.cfg["runs"]["showInApps"]:
        text += ("\nIt's " + ("pinned " if engine.pinned else "") + "in the Codex app"
                 + (f" as “{apps.title(run, idea)}”" if engine.named else "") + ", ready to continue any time.")
    elif run["engine"] == "claude" and run["thread_id"] and apps.desktop():  # (Claude Desktop is Mac-only here)
        if not engine.cfg["runs"]["showInApps"]:
            text += "\nTap Open in Claude to continue it in Claude Desktop."
        elif held:
            text += ("\nIts chat stays live in the Claude app while you're there. When you leave the app, it moves "
                     f"into Claude Desktop's Code tab as “{apps.title(run, idea)}”, ready to continue any time.")
        else:
            text += (f"\nIt moves into Claude Desktop's Code tab as “{apps.title(run, idea)}” once you're not using "
                     "the Claude app, ready to continue any time. Tap Open in Claude to move it now.")
    return text


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
    announced = False
    try:
        engine.start()
        conn.execute("UPDATE runs SET thread_id = ?, turn_id = ?, live_url = ? WHERE id = ?",
                     (engine.thread_id, engine.turn_id, engine.live_url, run_id))
        raise_alerts(conn, run_id, engine)  # the live link goes out right away
        beat = 0.0
        while outcome is None:
            engine.pump(0.5)
            t = time.time()
            if t - beat >= 2:
                beat = t
                tracker.track()
                conn.execute("UPDATE runs SET tokens_used = ?, tokens_total = ?, last_event_at = ?, "
                             "thread_id = COALESCE(thread_id, ?), live_url = ? WHERE id = ?",
                             (engine.tokens_used, engine.tokens_total, t, engine.thread_id, engine.live_url, run_id))
                # Limits can be tightened while running; a stop request is honored within ~2s.
                run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                if run["stop_requested_at"]:
                    stop.set()
            raise_alerts(conn, run_id, engine)
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
        elif (outcome == "completed" and engine.live_url and not stop.is_set()
              and engine.tokens_used < run["budget_tokens"] and time.time() < run["deadline_at"]):
            # Watched live, with time and tokens left: keep it open while you're in the app.
            announce(conn, run_id, idea, engine)
            announced = True
            cut = hold(conn, run_id, engine, stop, tracker)
            if cut:  # a stop or a limit, not you leaving: end whatever the chat is doing right away
                engine.interrupt()
            if cut in ("deadline", "budget"):
                limit = "time limit" if cut == "deadline" else f"{run['budget_tokens'] / 1000:g}k-token limit"
                notify.enqueue(conn, "run-note", f"I closed run #{run_id}'s chat: it reached its {limit}.",
                               dedupe_key=f"run-note:{run_id}:closed")
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
        raise_alerts(conn, run_id, engine)
        engine.close()
        survivors = proctree.terminate(tracker.targets(), grace=3)
        at = now()
        conn.execute("UPDATE runs SET status = 'done', outcome = COALESCE(outcome, ?), summary = COALESCE(summary, ?), "
                     "error = COALESCE(error, ?), tokens_used = ?, tokens_total = ?, ended_at = ?, cleanup = ?, "
                     "thread_id = COALESCE(thread_id, ?) WHERE id = ?",
                     (outcome or "failed", (engine.summary or "").strip()[:4000] or None, error or engine.error,
                      engine.tokens_used, engine.tokens_total, at, json.dumps({"workerSurvivors": survivors}),
                      engine.thread_id, run_id))
        ideas.set_status(conn, idea["id"], "done" if outcome == "completed" else "open")
        latest = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if latest["stop_requested_at"] is None and not announced:  # user stops get their own confirmation
            notify.enqueue(conn, "run-end", completion_text(latest, idea, outcome or "failed", engine,
                                                            error or engine.error, at),
                           dedupe_key=f"run-end:{run_id}", buttons=apps.button(latest))
        runs.summarize_if_cut_short(conn, latest)
    return 0
