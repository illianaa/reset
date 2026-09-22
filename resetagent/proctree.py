"""Process-tree snapshots and verified termination, via `ps` (macOS and Linux).

Cancellation must not depend on an agent cooperating, so runs are stopped by signalling
every process Reset has seen in the run's tree and then checking that none survived.
Processes are identified by (pid, start time) so a recycled pid is never signalled.
"""
from __future__ import annotations

from collections import namedtuple
import os
import signal
import subprocess
import time

Proc = namedtuple("Proc", "ppid pgid state lstart command")


def snapshot() -> dict:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,comm="],
                         capture_output=True, text=True, timeout=15).stdout
    procs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = Proc(ppid, pgid, parts[3], " ".join(parts[4:9]), " ".join(parts[9:]))
    return procs


def running(proc: Proc | None) -> bool:
    # Zombies are already dead; they only wait for their parent to reap them.
    return proc is not None and not proc.state.startswith("Z")


def descendants(root: int, procs: dict) -> dict:
    """The root, everything below it by parent chain, and its process group if it leads one."""
    children: dict = {}
    for pid, proc in procs.items():
        children.setdefault(proc.ppid, []).append(pid)
    found = {}
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in found or pid not in procs:
            continue
        found[pid] = procs[pid]
        stack.extend(children.get(pid, []))
    if root in procs and procs[root].pgid == root:
        for pid, proc in procs.items():
            if proc.pgid == root:
                found.setdefault(pid, proc)
    return found


def alive(pid: int | None, lstart: str | None, procs: dict | None = None) -> bool:
    if not pid:
        return False
    procs = snapshot() if procs is None else procs
    proc = procs.get(pid)
    return running(proc) and (lstart is None or proc.lstart == lstart)


def terminate(targets: dict, grace: float) -> list:
    """SIGTERM every live target, wait up to `grace`, SIGKILL the rest, then verify.

    targets maps pid -> start time. Returns [(pid, command)] still running afterwards.
    """
    def living():
        procs = snapshot()
        return {pid: procs[pid] for pid, start in targets.items()
                if running(procs.get(pid)) and procs[pid].lstart == start}

    current = living()
    for pid in current:
        _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while current and time.monotonic() < deadline:
        time.sleep(0.2)
        reap()
        current = living()
    for pid in current:
        _signal(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while current and time.monotonic() < deadline:
        time.sleep(0.1)
        reap()
        current = living()
    return [(pid, proc.command) for pid, proc in current.items()]


def reap() -> None:
    """Collect exited children so they don't linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
