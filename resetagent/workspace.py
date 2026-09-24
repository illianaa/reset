"""Where a run works, how that place is prepared, and what the agent is told about it.

- scratch: a fresh folder under ~/Reset/runs (when it's unclear where the work belongs)
- new:     a new project folder, e.g. ~/Projects/agent-social
- repo:    a folder in a git project; the run gets a new branch in its own worktree, never your checkout
- folder:  an existing non-git folder; changes happen in place
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import time

from resetagent import config
from resetagent.timeutil import local, now


class WorkspaceError(ValueError):
    """The requested project can't be used; the message says why."""


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:limit].strip("-") or "idea"


def short(path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


PROJECT_FOLDERS = ("Projects", "projects", "Developer", "code", "Code", "dev", "src", "repos", "git", "GitHub",
                   "workspace")


def projects_root(cfg: dict) -> Path:
    """The configured projects folder, else the first common one that exists (~/Projects if none do)."""
    if cfg["runs"].get("projectsRoot"):
        return Path(cfg["runs"]["projectsRoot"]).expanduser()
    home = Path.home()
    return next((home / name for name in PROJECT_FOLDERS if (home / name).is_dir()), home / "Projects")


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def repo_root(path: Path) -> Path | None:
    result = _git(["rev-parse", "--show-toplevel"], path)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def projects(cfg: dict, limit: int = 40) -> list:
    """Folders in the projects folder, most recently changed first."""
    root = projects_root(cfg)
    if not root.is_dir():
        return []
    found = [c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")]
    found.sort(key=lambda c: c.stat().st_mtime, reverse=True)
    return [{"name": c.name, "path": short(c), "git": (c / ".git").exists(), "changed": local(c.stat().st_mtime)}
            for c in found[:limit]]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def allowed(cfg: dict, path: Path) -> bool:
    """Runs may work in a project folder or elsewhere in your home folder, never in system or hidden places, and
    never where Reset keeps its own settings (wherever the projects folder is)."""
    path = path.expanduser().resolve()  # resolves ".." and symlinks before any check
    home, root, reset = Path.home().resolve(), projects_root(cfg).resolve(), config.home().resolve()
    if path in (home, root) or _inside(path, reset) or _inside(reset, path):
        return False
    if _inside(path, root):
        return True
    if not _inside(path, home):
        return False
    first = path.relative_to(home).parts[0]
    return not first.startswith(".") and first != "Library"


def resolve(cfg: dict, project) -> dict:
    """{"kind", "path", "label"} for a requested project. Empty, "new" or "scratch" means a fresh scratch folder."""
    raw = str(project or "").strip()
    if raw.lower() in ("", "new", "scratch", "none"):
        return {"kind": "scratch", "path": None, "label": "a fresh folder of its own"}
    root = projects_root(cfg)
    if raw.startswith(("/", "~")):
        path = Path(raw).expanduser()
    else:
        path = root / raw
        if not path.exists() and root.is_dir():  # "recipe box" finds recipe-box or RecipeBox
            key = re.sub(r"[^a-z0-9]", "", raw.lower())
            matches = [c for c in root.iterdir() if c.is_dir() and re.sub(r"[^a-z0-9]", "", c.name.lower()) == key]
            path = matches[0] if len(matches) == 1 else root / slug(raw)
    path = path.expanduser().resolve()
    if not allowed(cfg, path):
        raise WorkspaceError(f"A run can't work in {short(path)}. Use a folder in {short(root)} or elsewhere in "
                             "your home folder.")
    if path.exists():
        if not path.is_dir():
            raise WorkspaceError(f"{short(path)} isn't a folder.")
        top = repo_root(path)
        if top:
            if _git(["rev-parse", "--verify", "-q", "HEAD"], top).returncode != 0:
                raise WorkspaceError(f"{short(top)} has no commits yet, so a run can't get its own branch there. "
                                     "Commit once first, or pick another folder.")
            return {"kind": "repo", "path": path, "repo": top,
                    "label": f"{short(path)}, on a new branch from its last commit, in a separate worktree"}
        return {"kind": "folder", "path": path, "label": f"{short(path)} (changes happen in place)"}
    return {"kind": "new", "path": path, "label": f"new project folder {short(path)}"}


def _fresh(path: Path) -> Path:
    """path, or path-2, path-3… when it's taken (runs can start in the same second)."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    return candidate


def _fresh_branch(repo: Path, branch: str) -> str:
    candidate, n = branch, 1
    while _git(["rev-parse", "--verify", "-q", f"refs/heads/{candidate}"], repo).returncode == 0:
        n += 1
        candidate = f"{branch}-{n}"
    return candidate


def prepare(cfg: dict, idea, target: dict) -> dict:
    """Create the run's working folder. Returns {"workdir", "branch", "kind", "project"} (plus "repo" and "tree" for
    a worktree)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kind, project = target["kind"], target["path"]
    runs_root = Path(cfg["runs"]["root"]).expanduser()
    if kind == "scratch":
        workdir = _fresh(runs_root / f"{stamp}-idea{idea['id']}-{slug(idea['title'])}")
        workdir.mkdir(parents=True)
        (workdir / "IDEA.md").write_text(f"# Idea #{idea['id']}\n\n{idea['text']}\n")
        _git(["init", "-q"], workdir)
        return {"workdir": workdir, "branch": None, "kind": kind, "project": None}
    if kind == "new":
        project.mkdir(parents=True, exist_ok=True)
        if repo_root(project) is None:
            _git(["init", "-q"], project)
        return {"workdir": project, "branch": None, "kind": kind, "project": project}
    if kind == "repo":
        repo = target["repo"]
        branch = _fresh_branch(repo, f"reset/{slug(idea['title'], 30)}-{stamp}")
        tree = _fresh(runs_root.parent / "worktrees" / f"{repo.name}-{stamp}")
        tree.parent.mkdir(parents=True, exist_ok=True)
        result = _git(["worktree", "add", "-q", "-b", branch, str(tree), "HEAD"], repo)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise WorkspaceError(f"Couldn't set up a branch in {short(repo)}: {detail[-1] if detail else 'git failed'}")
        inside = project.resolve().relative_to(repo.resolve())  # a subfolder keeps its place in the worktree
        return {"workdir": tree / inside, "branch": branch, "kind": kind, "project": project, "repo": repo,
                "tree": tree}
    return {"workdir": project, "branch": None, "kind": kind, "project": project}


def discard(prepared: dict) -> None:
    """Undo prepare() for a run that didn't start: its scratch folder, or its worktree and branch, go. A project
    folder stays, since another run may be working there."""
    if prepared["kind"] == "scratch":
        shutil.rmtree(prepared["workdir"], ignore_errors=True)
    elif prepared["kind"] == "repo":
        _git(["worktree", "remove", "--force", str(prepared["tree"])], prepared["repo"])
        _git(["branch", "-D", prepared["branch"]], prepared["repo"])


def branch_work(run) -> dict:
    """For a run on its own branch: its commits and anything left uncommitted."""
    tree = Path(run["workdir"])
    if not run["branch"] or not tree.is_dir():
        return {}
    # Commits only this branch has (everything else it contains is on the user's other branches).
    log = _git(["log", "--format=%h %s", "-n", "30", "HEAD", "--not", "--exclude=reset/*", "--branches"], tree)
    status = _git(["status", "--short", "--untracked-files=all"], tree)
    return {"branch": run["branch"], "commits": log.stdout.splitlines() if log.returncode == 0 else None,
            "uncommitted": status.stdout.splitlines()[:40] if status.returncode == 0 else None}


def brief(run, access: str = "full") -> str:
    """What the working agent is told about where it is and how an unattended run works."""
    workdir, kind, full = short(run["workdir"]), run["workspace"] or "scratch", access == "full"
    # Sandboxes protect .git (hooks could escape them), so only full-access runs can commit.
    commits = ("Commit working steps to this branch with clear messages." if full else
               "This run can't commit, so leave your changes uncommitted on this branch for the user to review.")
    where = {
        "scratch": f"You're in {workdir}, a fresh folder made for this idea. Build it here.",
        "new": f"You're starting a new project in {workdir}. Set it up the way a developer would expect, with a README.",
        "repo": (f"You're continuing an existing codebase ({short(run['project'])}) in a separate git worktree at "
                 f"{workdir}, on the new branch {run['branch']}. Read its README, AGENTS.md or CLAUDE.md first and "
                 f"follow its conventions. {commits} Don't push, merge "
                 "or touch other branches: the user will review this one. Files git ignores (such as .env or "
                 "installed dependencies) aren't copied here; you may read them in the original checkout, but "
                 "change nothing there."),
        "folder": (f"You're working in {workdir}, an existing folder. Look at what's there first, follow its "
                   "conventions, and change only what the idea needs."),
    }[kind]
    if not full:
        where += ("\n\nThis run is sandboxed: you can change files in this folder, but anything beyond that (the "
                  "network, other folders, git commits, and for some agents any command) will be refused. Work "
                  "within that, and list anything you couldn't do in your summary.")
    minutes = max(1, int((run["deadline_at"] - (run["started_at"] or now())) / 60))
    return (f"{where}\n\nThis is an unattended Reset run. Nobody is watching live, so don't wait for answers: make "
            "reasonable assumptions and list them in your final summary. You have about "
            f"{minutes} minutes and a budget of about {run['budget_tokens'] // 1000}k tokens, and the run can be "
            "stopped at any moment, so work in steps that each leave things working, and "
            + ("save or commit after each one." if full else "save after each one.")
            + " Avoid system-wide changes such as global installs or OS settings unless the idea truly needs "
            "them. Finish with a summary under 120 words: what you did, where it is, and a good next step. It will "
            "be texted to the user.")
