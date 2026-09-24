"""The background service: poll messages, handle commands, supervise runs, monitor usage,
deliver notifications. No step needs a model, so it keeps working when quotas are exhausted.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import threading
import time
import traceback

from resetagent import apps, brain, channels, commands, config, db, monitor, notify, proctree, runs, status
from resetagent.channels.base import ChannelError, ChannelUnavailable
from resetagent.providers.common import describe
from resetagent.timeutil import iso, now

LABEL = "com.reset.agent"
ROOT = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    print(f"{iso(now())} {message}", flush=True)


class Daemon:
    def __init__(self):
        self.stopping = False
        self.last_status = 0.0
        self.last_sweep = 0.0
        self.channel_problems: dict = {}

    def step(self, label: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # one failing step must not stop the others
            log(f"{label} failed: {describe(exc)}")
            if config.env("RESET_DEBUG"):
                traceback.print_exc()
            return None

    def poll_channels(self, conn, built: dict) -> None:
        for name, channel in built.items():
            try:
                channel.poll(conn)
                problem = None
            except ChannelUnavailable as exc:
                problem = str(exc)
            except ChannelError as exc:
                problem = str(exc)
            except Exception as exc:
                problem = describe(exc)
            if problem != self.channel_problems.get(name):
                log(f"{name} inbound: {problem or 'ok'}")
                self.channel_problems[name] = problem
                if problem:
                    db.kv_set(conn, f"inbound:{name}", problem)
                else:
                    db.kv_delete(conn, f"inbound:{name}")

    def run(self) -> int:
        home = config.home()
        home.mkdir(parents=True, exist_ok=True)
        lock = open(home / "daemon.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another Reset daemon is already running")
            return 1
        lock.write(str(os.getpid()))
        lock.flush()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.request_stop)
        conn = db.connect()
        log(f"daemon started (pid {os.getpid()})")
        for result in self.step("reconcile", runs.reconcile_on_start, conn, config.load()) or []:
            log(f"stopped run #{result['run']} left over from before the restart")
        startup = config.load()
        telegram = channels.build(startup)["telegram"]
        if telegram.configured():
            self.step("telegram commands", telegram.set_commands)
        # The brain answers on its own thread, so a slow model reply never delays "stop" or approvals.
        brain_stop = threading.Event()
        brain_thread = threading.Thread(target=brain.work, args=(brain_stop,), name="brain", daemon=True)
        brain_thread.start()
        while not self.stopping:
            cfg = config.load()
            built = channels.build(cfg)
            self.poll_channels(conn, built)
            self.step("commands", commands.process_pending, conn, cfg)
            self.step("asks", runs.expire_asks, conn)
            self.step("supervise", runs.supervise, conn, cfg)
            if time.time() - self.last_sweep >= 5:  # finished Claude runs move to Claude Desktop
                self.last_sweep = time.time()
                handed = self.step("apps", apps.sweep, conn, cfg)
                if handed:
                    log(f"run #{handed} handed to Claude Desktop")
            due = time.time() - self.last_status >= cfg["monitor"]["statusMinutes"] * 60
            if due or db.kv_get(conn, "status:refresh"):
                db.kv_delete(conn, "status:refresh")
                snap = self.step("status", status.collect, cfg, conn)
                if snap:
                    queued = self.step("monitor", monitor.evaluate, conn, cfg, snap) or []
                    if queued:
                        log(f"queued: {', '.join(queued)}")
                self.last_status = time.time()
            for _, via, error in self.step("deliver", notify.flush, conn, cfg, built) or []:
                if error:
                    log(f"delivery failed: {error}")
            if not brain_thread.is_alive():
                log("brain thread stopped unexpectedly; restarting it")
                brain_thread = threading.Thread(target=brain.work, args=(brain_stop,), name="brain", daemon=True)
                brain_thread.start()
            self.sleep(cfg["daemon"]["inboxSeconds"])
        brain_stop.set()
        log("daemon stopped")
        return 0

    def request_stop(self, *_):
        self.stopping = True

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            proctree.reap()
            time.sleep(0.25)


# --- background service: LaunchAgent on macOS, systemd user unit on Linux ----------------------

UNIT = "reset-agent.service"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / UNIT


def domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> str:
    """Install and start the service. Returns the Python executable it runs."""
    if sys.platform == "darwin":
        return install_launchd()
    if sys.platform.startswith("linux"):
        return install_systemd()
    raise RuntimeError("Automatic service setup supports macOS and Linux; elsewhere run "
                       "`resetctl daemon run` under your own process manager.")


def systemd_unit(python: str) -> str:
    env = {"PYTHONPATH": str(ROOT), "RESET_HOME": str(config.home()),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    lines = ["[Unit]", "Description=Reset agent", "After=network-online.target", "", "[Service]",
             f"ExecStart={python} -m resetagent daemon run", f"WorkingDirectory={ROOT}"]
    lines += [f'Environment="{key}={value}"' for key, value in env.items()]
    lines += ["Restart=always", "RestartSec=10", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def install_systemd() -> str:
    python = os.path.realpath(sys.executable)
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(systemd_unit(python))
    for args in (["daemon-reload"], ["enable", "--now", UNIT]):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return python


def install_launchd() -> str:
    logs = config.home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    python = os.path.realpath(sys.executable)
    spec = {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "resetagent", "daemon", "run"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT), "RESET_HOME": str(config.home()),
                                 "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        # "Background" jobs are spawned lazily and throttled; a chat agent should answer promptly.
        "ProcessType": "Standard",
        "StandardOutPath": str(logs / "daemon.log"),
        "StandardErrorPath": str(logs / "daemon.log"),
    }
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as handle:
        plistlib.dump(spec, handle)
    subprocess.run(["launchctl", "bootout", f"{domain()}/{LABEL}"], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain(), str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()[:200]}")
    subprocess.run(["launchctl", "kickstart", f"{domain()}/{LABEL}"], capture_output=True)  # start now
    return python


def uninstall() -> bool:
    if sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
        target = unit_path()
    else:
        subprocess.run(["launchctl", "bootout", f"{domain()}/{LABEL}"], capture_output=True)
        target = plist_path()
    if target.exists():
        target.unlink()
        return True
    return False


def service_status() -> dict:
    if sys.platform.startswith("linux"):
        active = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True)
        pid = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT],
                             capture_output=True, text=True).stdout.strip()
        state = active.stdout.strip()
        return {"installed": unit_path().exists(), "loaded": state != "", "state": state,
                **({"pid": int(pid)} if pid.isdigit() and pid != "0" else {})}
    result = subprocess.run(["launchctl", "print", f"{domain()}/{LABEL}"], capture_output=True, text=True)
    if result.returncode != 0:
        return {"installed": plist_path().exists(), "loaded": False}
    info = {"installed": True, "loaded": True}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("state = "):
            info["state"] = line.split("=", 1)[1].strip()
        elif line.startswith("pid = "):
            info["pid"] = int(line.split("=", 1)[1].strip())
    return info
