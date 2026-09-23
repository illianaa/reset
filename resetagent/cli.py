"""resetctl: Reset's command line, shared by you, the agent skills and the background service."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import secrets
import shutil
import sys
import time

from resetagent import __version__, brain, channels, config, db, ideas, models, monitor, notify, runs, status, workspace
from resetagent.channels.base import ChannelError, ChannelUnavailable
from resetagent.channels.imessage import IMessage
from resetagent.channels.telegram import Telegram
from resetagent.providers import claude, codex
from resetagent.providers.common import describe
from resetagent.timeutil import local, now, span

ROOT = Path(__file__).resolve().parents[1]


def out_json(value) -> None:
    print(json.dumps(value, indent=2, default=lambda o: dict(o) if hasattr(o, "keys") else str(o)))


def fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


# --- usage ------------------------------------------------------------------------------------

def cmd_status(args) -> int:
    conn = db.connect()
    snap = status.latest(conn) if args.cached else status.collect(config.load(), conn)
    if snap is None:
        return fail("No stored reading yet; run `resetctl status` without --cached.")
    if args.json:
        out_json(snap)
    else:
        print(status.render(snap), end="")
    return 0


def cmd_monitor(args) -> int:
    cfg = config.load()
    conn = db.connect()
    snap = status.collect(cfg, conn)
    queued = monitor.evaluate(conn, cfg, snap)
    for key in queued:
        row = conn.execute("SELECT text FROM notifications WHERE dedupe_key = ?", (key,)).fetchone()
        print(f"queued {key}\n  {row['text']}\n")
    if not queued:
        print("Nothing new to notify.")
    if args.send:
        for _, via, error in notify.flush(conn, cfg):
            print(f"delivered via {via}" if via else f"delivery failed: {error}")
    return 0


# --- ideas ------------------------------------------------------------------------------------

def cmd_idea(args) -> int:
    conn = db.connect()
    try:
        idea_id = ideas.add(conn, " ".join(args.text), source=args.source, engine=args.engine, project=args.project)
    except ValueError as exc:
        return fail(str(exc))
    print(f"Saved idea #{idea_id}: {ideas.title_for(' '.join(args.text))}")
    return 0


def cmd_ideas(args) -> int:
    rows = ideas.listing(db.connect(), include_all=args.all)
    if args.json:
        out_json([dict(r) for r in rows])
        return 0
    if not rows:
        print("No ideas yet. Add one with: resetctl idea \"...\"")
    for r in rows:
        extra = f" [{r['status']}]" if r["status"] != "open" else ""
        engine = f" ({r['engine']})" if r["engine"] else ""
        project = f" → {r['project']}" if r["project"] else ""
        print(f"#{r['id']:<4} {r['title']}{engine}{project}{extra}")
    return 0


def cmd_drop(args) -> int:
    if ideas.drop(db.connect(), args.id):
        print(f"Removed idea #{args.id}.")
        return 0
    return fail(f"Couldn't remove idea #{args.id} (missing or running).")


# --- runs -------------------------------------------------------------------------------------

def cmd_propose(args) -> int:
    conn = db.connect()
    cfg = config.load()
    try:
        ask = runs.propose(conn, cfg, args.idea, engine=args.engine, model=args.model, effort=args.effort,
                           project=args.project, budget_tokens=args.budget_tokens, max_minutes=args.minutes,
                           via=args.via)
    except runs.RunError as exc:
        return fail(str(exc))
    delivered = notify.flush(conn, cfg)
    via = next((v for _, v, _ in delivered if v), None)
    print(f"Requested approval for idea #{args.idea} (code {ask['code']}, expires {local(ask['expires_at'])}).")
    print(f"Sent via {via}." if via else "The request is queued; the background service will deliver it.")
    print("Only the user can approve: tap Start (or reply “yes CODE”) in Telegram, or run "
          "`resetctl approve CODE` in their own terminal.")
    return 0


def cmd_approve(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return fail("Approvals must come from you: tap Start (or reply “yes CODE”) in Telegram, or run "
                    "`resetctl approve CODE` in your own terminal.", 2)
    conn = db.connect()
    ask = conn.execute("SELECT * FROM asks WHERE code = ? AND status = 'pending'", (args.code,)).fetchone()
    if ask is None:
        return fail(f"No pending request with code {args.code}.")
    idea = ideas.get(conn, ask["idea_id"])
    print(f"Idea #{idea['id']}: {idea['title']}\nEngine: {ask['engine']} · up to {ask['budget_tokens'] / 1000:g}k "
          f"tokens · {ask['max_minutes']} min")
    if input("Type the code again to start: ").strip() != args.code:
        return fail("Not started.")
    try:
        run = runs.approve(conn, config.load(), args.code, via="terminal")
    except runs.RunError as exc:
        return fail(str(exc))
    print(f"Started run #{run['id']} (stops by {local(run['deadline_at'])}). Stop it with `resetctl stop`.")
    return 0


def cmd_decline(args) -> int:
    if runs.decline(db.connect(), args.code, via="cli"):
        print("Skipped.")
        return 0
    return fail(f"No pending request with code {args.code}.")


def cmd_stop(args) -> int:
    conn = db.connect()
    target = None if args.run in (None, "all") else int(args.run)
    results = runs.stop(conn, config.load(), run_id=target, reason="stop from terminal")
    print(runs.describe_stop(results) if results["runs"] or results["declined"] else "Nothing is running.")
    return 0


def cmd_runs(args) -> int:
    conn = db.connect()
    if args.json:
        out_json([dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20")])
        return 0
    print(runs.summary_text(conn))
    if args.all:
        for r in conn.execute("SELECT * FROM runs WHERE status = 'done' ORDER BY id DESC LIMIT 10"):
            took = span((r["ended_at"] or now()) - (r["started_at"] or r["created_at"]))
            cleanup = json.loads(r["cleanup"] or "{}")
            verified = "verified clean" if r["verified_at"] and not cleanup.get("survivors") else "unverified"
            print(f"\n#{r['id']} {r['engine']} idea #{r['idea_id']}: {r['outcome']} after {took} · "
                  f"{r['tokens_used'] / 1000:.1f}k tokens · {verified}"
                  + (f" · stop: {r['stop_reason']}" if r["stop_reason"] else "")
                  + (f"\n   error: {r['error']}" if r["error"] else "")
                  + f"\n   {r['workdir']}")
    return 0


# --- grants and mode -------------------------------------------------------------------------

def cmd_grant(args) -> int:
    conn = db.connect()
    if args.action == "add":
        ends = status.parse_iso(args.expires)
        if ends is None:
            return fail("Give the expiry as ISO 8601, e.g. 2030-01-31T12:00:00Z")
        grant_id = conn.execute("INSERT INTO manual_grants(provider, label, ends_at, created_at) VALUES (?, ?, ?, ?)",
                                (args.provider, args.label, ends, now())).lastrowid
        print(f"Recorded manual {args.provider} reset #{grant_id}, expiring {local(ends)}.")
    elif args.action == "remove":
        conn.execute("DELETE FROM manual_grants WHERE id = ?", (args.id,))
        print(f"Removed manual reset #{args.id}.")
    else:
        for row in conn.execute("SELECT * FROM manual_grants ORDER BY ends_at"):
            print(f"#{row['id']} {row['provider']}: {row['label']} · expires {local(row['ends_at'])}")
    return 0


def cmd_mode(args) -> int:
    if args.mode is None:
        print(config.load()["mode"])
        return 0
    if args.mode == "auto":
        return fail("Automatic execution isn't available yet. Reset asks before every run.")
    with config.editing() as cfg:
        cfg["mode"] = args.mode
    print(f"Mode: {args.mode}")
    return 0


# --- channels ---------------------------------------------------------------------------------

def cmd_channel(args) -> int:
    conn = db.connect()
    cfg = config.load()
    if args.action == "status":
        built = channels.build(cfg)
        for name in channels.order(conn, cfg):
            ch = built[name]
            line = f"{name:<9} {'ready' if ch.configured() else 'not configured'}"
            result = db.kv_get(conn, f"delivery:{name}")
            if result:
                line += f" · delivery test: {result}"
            problem = db.kv_get(conn, f"inbound:{name}")
            if problem:
                line += f" · inbound: {problem}"
            print(line)
        return 0
    if args.action == "imessage":
        with config.editing() as edit:
            settings = edit["channels"]["imessage"]
            if args.off:
                settings["enabled"] = False
            else:
                settings["enabled"] = True
                if args.handle:
                    settings["handle"] = args.handle
                if args.chat_guid:
                    settings["chatGuid"] = args.chat_guid
        cfg = config.load()
        settings = cfg["channels"]["imessage"]
        if settings["enabled"] and not (settings.get("handle") or settings.get("chatGuid")):
            return fail("Give the address Reset should text: `resetctl channel imessage --handle <your phone number or Apple ID email>`")
        try:
            chats = IMessage(cfg).self_chats()
            print(f"Found {len(chats)} self-chat(s) in Messages; replies from your devices will be read.")
        except ChannelUnavailable as exc:
            print(f"Sending works without extra access. To read your replies, {exc.args[0].split('; ', 1)[-1]} "
                  "(System Settings → Privacy & Security → Full Disk Access).")
        print("iMessage " + ("enabled." if settings["enabled"] else "disabled."))
        return 0
    if args.action == "telegram":
        if args.off:
            with config.editing() as edit:
                edit["channels"]["telegram"]["enabled"] = False
            print("Telegram disabled.")
            return 0
        if not args.token:
            return fail("Create a bot with @BotFather, then run `resetctl channel telegram --token <token>`.")
        probe = Telegram({"channels": {"telegram": {"botToken": args.token, "enabled": True}}})
        try:
            me = probe.call("getMe", {})
        except ChannelError as exc:
            return fail(f"That token didn't work: {exc}")
        code = f"{secrets.randbelow(1_000_000):06d}"
        with config.editing() as edit:
            edit["channels"]["telegram"].update({"enabled": True, "botToken": args.token, "chatId": None,
                                                 "botUsername": me.get("username"), "pairingCode": code})
        print(f"Open https://t.me/{me.get('username')} in Telegram and send:  /start {code}")
        if args.wait:
            end = time.time() + 300
            while time.time() < end:
                bot = Telegram(config.load())
                try:
                    bot.poll(conn)
                except ChannelError as exc:
                    print(f"poll failed: {exc}")
                if bot.settings.get("chatId"):
                    print("Paired.")
                    return 0
                time.sleep(2)
            return fail("Not paired yet; the background service will finish pairing when you send the code.")
        return 0
    if args.action == "test":
        channel = channels.build(cfg).get(args.name)
        if channel is None or not channel.configured():
            return fail(f"{args.name} isn't configured.")
        code = f"{secrets.randbelow(10000):04d}"
        text = (f"Delivery test {code}. If your phone buzzed, reply “ok {code}”. "
                f"If it arrived without a buzz, reply “quiet {code}”.")
        try:
            channel.send(text)
        except ChannelError as exc:
            db.kv_set(conn, f"delivery:{args.name}", "failed")
            return fail(f"Sending failed: {exc}")
        conn.execute("INSERT INTO outbound(channel, text, sent_at) VALUES (?, ?, ?)", (args.name, text, now()))
        db.kv_set(conn, f"delivery-test:{args.name}", {"code": code, "sentAt": now(), "result": None})
        print(f"Sent test {code} via {args.name}.")
        return 0
    if args.action == "result":
        db.kv_set(conn, f"delivery:{args.name}", args.result)
        print(f"Recorded: {args.name} delivery {args.result}.")
        return 0
    if args.action == "order":
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        unknown = [n for n in names if n not in channels.KINDS]
        if unknown:
            return fail(f"Unknown channel(s): {', '.join(unknown)}")
        with config.editing() as edit:
            edit["channels"]["order"] = names
        print("Order: " + " → ".join(channels.order(conn, config.load())))
        return 0
    return fail("Unknown channel action.")


# --- service, skill, health -------------------------------------------------------------------

def cmd_daemon(args) -> int:
    from resetagent import daemon

    if args.action == "run":
        return daemon.Daemon().run()
    if args.action == "install":
        try:
            python = daemon.install()
        except RuntimeError as exc:
            return fail(str(exc))
        print(f"Installed and started {daemon.LABEL}. Logs: {config.home() / 'logs' / 'daemon.log'}")
        print(f"To read iMessage replies, give Full Disk Access to: {python}")
        return 0
    if args.action == "uninstall":
        print("Removed." if daemon.uninstall() else "Not installed.")
        return 0
    info = daemon.service_status()
    if args.json:
        print(json.dumps(info))
    elif info.get("state") in ("running", "active"):
        print(f"running (pid {info.get('pid')})" if info.get("pid") else "running")
    else:
        print(f"installed, {info.get('state', 'not loaded')}" if info.get("installed") else "not installed")
    return 0


def cmd_install_skill(args) -> int:
    links = {Path.home() / ".claude/skills/reset": ROOT / "skills/reset",
             Path.home() / ".codex/skills/reset": ROOT / "skills/reset",
             Path.home() / ".local/bin/resetctl": ROOT / "bin/resetctl"}
    for link, target in links.items():
        if link.is_symlink() and link.resolve() == target.resolve():
            print(f"ok       {link}")
        elif link.exists() or link.is_symlink():
            print(f"skipped  {link} (something else is already there)")
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            print(f"linked   {link} -> {target}")
    return 0


def cmd_doctor(args) -> int:
    from resetagent import daemon

    cfg = config.load()
    conn = db.connect()
    codex_bin, claude_bin = codex.resolve_bin(cfg), claude.resolve_bin(cfg)
    print(f"Reset {__version__} · mode {cfg['mode']} · home {config.home()}")
    print(f"Codex CLI      {codex_bin or 'not found'}")
    print(f"Claude CLI     {claude_bin or 'not found'}")
    if claude_bin:
        try:
            auth = claude.auth_status(claude_bin)
            print(f"  signed in    {auth.get('loggedIn')} ({auth.get('subscriptionType')})")
        except Exception as exc:
            print(f"  auth check   {describe(exc)}")
    print(f"zstd           {shutil.which('zstd') or 'not found (cached Claude reset grants unavailable)'}")
    try:
        IMessage(cfg).open().close()
        print("Messages db    readable")
    except ChannelUnavailable as exc:
        print(f"Messages db    {exc}")
    for name in ("imessage", "telegram"):
        result = db.kv_get(conn, f"delivery:{name}")
        print(f"{name:<14} {'configured' if channels.build(cfg)[name].configured() else 'not configured'}"
              + (f" · delivery {result}" if result else ""))
    service = daemon.service_status()
    print(f"Service        {service.get('state') or ('installed' if service.get('installed') else 'not installed')}"
          + (f" (pid {service['pid']})" if service.get("pid") else ""))
    return 0


# --- setup (people and agents share these steps; see AGENTS.md) ------------------------------

def check_line(label: str, ok, detail: str) -> None:
    mark = {True: "ok ", False: "-- ", None: "?? "}[ok]
    print(f"  {mark} {label:<18}{detail}")


def setup_check(args=None) -> int:
    from resetagent import daemon

    cfg = config.load()
    print("Reset setup check")
    version = sys.version_info
    check_line("Python", version >= (3, 9), f"{version.major}.{version.minor}.{version.micro} (needs 3.9+)")
    check_line("Platform", sys.platform == "darwin" or sys.platform.startswith("linux"),
               {"darwin": "macOS"}.get(sys.platform, sys.platform))
    codex_bin, claude_bin = codex.resolve_bin(cfg), claude.resolve_bin(cfg)
    codex_live = status.codex_status(cfg) if codex_bin else None
    check_line("Codex", bool(codex_live and codex_live["state"] == "live"),
               "not installed" if not codex_bin else
               (f"signed in ({codex_live.get('planType')})" if codex_live["state"] == "live"
                else f"not usable: {codex_live.get('error')} (sign in with `codex login`)"))
    claude_auth = claude.auth_status(claude_bin) if claude_bin else None
    check_line("Claude Code", bool(claude_auth and claude_auth.get("loggedIn")),
               "not installed" if not claude_bin else
               (f"signed in ({claude_auth.get('subscriptionType')})" if claude_auth.get("loggedIn")
                else "not signed in (run `claude`, then /login)"))
    telegram = cfg["channels"]["telegram"]
    check_line("Telegram", bool(telegram.get("chatId")),
               f"paired with @{telegram.get('botUsername')}" if telegram.get("chatId") else
               "not set up: the user runs `resetctl setup telegram` in their own terminal")
    check_line("AI brain", brain.enabled(cfg), ", ".join(cfg["brain"]["order"]))
    check_line("Run access", True, f"{cfg['runs']['access']} (change with `resetctl setup access`)")
    root = workspace.projects_root(cfg)
    found = len(workspace.projects(cfg, limit=10000)) if root.is_dir() else 0
    check_line("Projects", root.is_dir(), f"{workspace.short(root)} ({found} folders)" if root.is_dir() else
               f"{workspace.short(root)} doesn't exist yet; new projects will be created there")
    skill = Path.home() / ".claude/skills/reset"
    check_line("Skill", skill.is_symlink(), "linked" if skill.is_symlink() else "not linked: `resetctl setup skill`")
    service = daemon.service_status()
    check_line("Service", service.get("state") in ("running", "active"),
               service.get("state") or ("installed" if service.get("installed") else
                                        "not installed: `resetctl setup service`"))
    if not (codex_live and codex_live["state"] == "live") and not (claude_auth and claude_auth.get("loggedIn")):
        print("\nSign in to Codex or Claude Code first: Reset works with the subscriptions those tools use.")
    return 0


def wait_for_pairing(conn, timeout: float) -> int:
    end, last_error = time.time() + timeout, None
    while time.time() < end:
        bot = Telegram(config.load())
        if bot.settings.get("chatId"):
            print("Paired. Reset just sent you a welcome message in Telegram.")
            return 0
        try:
            bot.poll(conn)
        except ChannelError as exc:
            if str(exc) != last_error:
                print(f"(still trying: {exc})")
                last_error = str(exc)
        time.sleep(2)
    return fail("Not paired yet. Tap the link (or send the /start code) and run `resetctl setup telegram` again.")


def setup_telegram(args) -> int:
    token = getattr(args, "token", None)
    from_env = not token and bool(config.env("RESET_TELEGRAM_BOT_TOKEN"))
    if from_env:
        token = config.env("RESET_TELEGRAM_BOT_TOKEN")
        print("Using the bot token from RESET_TELEGRAM_BOT_TOKEN (it stays there, not in config.json).")
    if not token:
        if not sys.stdin.isatty():
            return fail("This step asks for your bot token privately, so run it in your own terminal:\n"
                        "  resetctl setup telegram")
        print("Create your Reset bot in Telegram:\n  1. Open @BotFather and send /newbot\n"
              "  2. Choose a name, then a username ending in 'bot'\n  3. Copy the token BotFather sends you\n")
        token = getpass.getpass("Paste the bot token (input is hidden): ").strip()
    probe = Telegram({"channels": {"telegram": {"botToken": token, "enabled": True}}})
    try:
        me = probe.call("getMe", {})
    except ChannelError as exc:
        return fail(f"That token didn't work: {exc}")
    code = f"{secrets.randbelow(1_000_000):06d}"
    with config.editing() as cfg:
        cfg["channels"]["telegram"].update({"enabled": True, "botToken": None if from_env else token,
                                            "botUsername": me.get("username"), "chatId": None, "ownerId": None,
                                            "pairingCode": code})
        order = [n for n in cfg["channels"]["order"] if n != "telegram"]
        cfg["channels"]["order"] = ["telegram"] + order
    conn = db.connect()
    db.kv_delete(conn, "cursor:telegram")  # a new bot starts with a fresh update cursor
    username = me.get("username")
    print(f"\nNow open this link on your phone and tap Start:\n  https://t.me/{username}?start={code}\n"
          f"(or send “/start {code}” to @{username})\n\nWaiting for you…")
    return wait_for_pairing(conn, timeout=600)


def setup_brain(args) -> int:
    choice = getattr(args, "use", None)
    if choice is None and sys.stdin.isatty():
        print("Reset can use AI to answer questions and summarize runs, through the AI tools you're signed in to.\n"
              "  1) Auto: Claude Code, then Codex (recommended)\n  2) Claude Code only\n  3) Codex only\n"
              "  4) No AI: commands only")
        choice = {"1": "auto", "2": "claude-code", "3": "codex", "4": "none", "": "auto"}.get(
            input("Choose [1]: ").strip(), "auto")
    order = [name.strip() for name in (choice or "auto").split(",") if name.strip()]
    valid = {"auto", "none", *brain.BACKENDS}
    if not order or any(name not in valid for name in order):
        return fail(f"--use takes auto, none, or a comma list of: {', '.join(brain.BACKENDS)}")
    with config.editing() as cfg:
        cfg["brain"]["order"] = order
        for key, flag in (("claudeModel", "claude_model"), ("codexModel", "codex_model"),
                          ("codexEffort", "codex_effort")):
            if getattr(args, flag, None):
                cfg["brain"][key] = getattr(args, flag)
    print("AI brain: " + ("off (commands only)" if order == ["none"] else ", ".join(order)))
    if order != ["none"]:
        print('Try it: resetctl ask "how many one-time resets do I have?"')
    return 0


ACCESS_HELP = ("How much should runs be allowed to do on their own?\n"
               "  1) Full access (recommended): runs can run commands, install packages and use the network without\n"
               "     stopping to ask. Each run still needs your OK, has a budget and deadline, and existing codebases\n"
               "     get their own branch in a separate worktree.\n"
               "  2) Sandboxed: runs only edit files in their own folder; anything more is refused, and Reset tells\n"
               "     you when that happens.")


def setup_access(args) -> int:
    choice = getattr(args, "access", None)
    if choice is None and sys.stdin.isatty():
        print(ACCESS_HELP)
        choice = {"1": "full", "2": "sandboxed", "": "full"}.get(input("Choose [1]: ").strip(), "full")
    choice = choice or "full"
    with config.editing() as cfg:
        cfg["runs"]["access"] = choice
    print(f"Runs have {choice} access.")
    return 0


def setup_service(args=None) -> int:
    from resetagent import daemon

    try:
        python = daemon.install()
    except RuntimeError as exc:
        return fail(str(exc))
    print(f"Background service installed and started (Python: {python}).")
    print(f"Logs: {config.home() / 'logs' / 'daemon.log'}")
    if sys.platform.startswith("linux"):
        print("To keep it running after you log out: loginctl enable-linger $USER")
    return 0


def setup_test(args=None) -> int:
    conn = db.connect()
    cfg = config.load()
    try:
        via = channels.deliver(conn, cfg, "Reset is set up and listening. Try /status, or ask me anything about "
                                          "your usage and ideas.")
    except ChannelError as exc:
        return fail(f"Couldn't send a test message: {exc}")
    print(f"Sent a test message via {via}.")
    return 0


def cmd_setup(args) -> int:
    steps = {"check": setup_check, "telegram": setup_telegram, "brain": setup_brain, "access": setup_access,
             "skill": cmd_install_skill, "service": setup_service, "test": setup_test}
    if args.step:
        return steps[args.step](args)
    if not sys.stdin.isatty():
        return fail("Run `resetctl setup` in your own terminal, or run its steps one at a time (see AGENTS.md).")
    for name in ("check", "telegram", "brain", "access", "skill", "service", "test"):
        print(f"\n== {name} ==")
        if steps[name](args):
            return fail(f"Setup stopped at the {name} step; fix the issue above and run `resetctl setup` again.")
    print("\nAll set. Message your bot in Telegram.")
    return 0


def cmd_ask(args) -> int:
    conn = db.connect()
    cfg = config.load()
    if args.brain:
        cfg["brain"]["order"] = [args.brain]
    if not brain.enabled(cfg):
        return fail("The AI brain is off. Turn it on with: resetctl setup brain --use auto")
    reply, who = brain.answer(conn, cfg, brain.compose(" ".join(args.question), []), force=bool(args.brain))
    if reply is None:
        return fail(brain.fallback(who))
    print(f"{reply}\n\n(answered by {who})")
    return 0


def cmd_models(args) -> int:
    cfg = config.load()
    for engine in runs.ENGINES:
        catalog = models.catalog(cfg, engine)
        print(f"{engine.capitalize()}:" + ("" if catalog else " unavailable (is it installed and signed in?)"))
        for m in catalog or []:
            efforts = ", ".join(m["efforts"]) or "no effort levels"
            print(f"  {m['id']:<24} {m['name']:<24} {efforts}" + ("  (default)" if m["default"] else ""))
    print(f"\nDefault effort: {cfg['runs']['effort']} · run access: {cfg['runs']['access']}")
    return 0


def cmd_mcp(args) -> int:
    from resetagent import mcp

    return mcp.serve()


# --- parser -----------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resetctl", description="Reset: use spare AI subscription capacity on your ideas.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("status", help="usage limits and one-time resets for Codex and Claude")
    p.add_argument("--json", action="store_true")
    p.add_argument("--cached", action="store_true", help="show the last stored reading instead of reading live")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("idea", help="save an idea (saving never runs it)")
    p.add_argument("text", nargs="+")
    p.add_argument("--engine", choices=ideas.ENGINES)
    p.add_argument("--project", help="project folder name or path the idea belongs to")
    p.add_argument("--source", default="cli", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_idea)

    p = sub.add_parser("ideas", help="list ideas")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ideas)

    p = sub.add_parser("drop", help="remove an idea")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("propose", help="ask the user to approve a bounded run of an idea")
    p.add_argument("idea", type=int)
    p.add_argument("--engine", choices=runs.engines_allowed())
    p.add_argument("--model", help="model id or name (see `resetctl models`)")
    p.add_argument("--effort", choices=models.EFFORTS)
    p.add_argument("--project", help="project folder name or path; empty for a scratch folder")
    p.add_argument("--budget-tokens", type=int)
    p.add_argument("--minutes", type=int)
    p.add_argument("--via", default="cli", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("approve", help="approve a request (interactive terminal only)")
    p.add_argument("code")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("decline", help="decline a request")
    p.add_argument("code")
    p.set_defaults(fn=cmd_decline)

    p = sub.add_parser("stop", help="stop runs (all by default) and cancel pending requests")
    p.add_argument("run", nargs="?")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("runs", help="active runs and pending requests")
    p.add_argument("--all", action="store_true", help="include recent finished runs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("grant", help="manually record a one-time reset Reset can't see")
    p.add_argument("action", choices=["list", "add", "remove"], nargs="?", default="list")
    p.add_argument("--provider", choices=["claude", "codex"], default="claude")
    p.add_argument("--label", default="Usage reset")
    p.add_argument("--expires", help="ISO 8601 expiry")
    p.add_argument("--id", type=int)
    p.set_defaults(fn=cmd_grant)

    p = sub.add_parser("mode", help="show or set the mode (notify or ask)")
    p.add_argument("mode", nargs="?", choices=["notify", "ask", "auto"])
    p.set_defaults(fn=cmd_mode)

    p = sub.add_parser("channel", help="set up and test iMessage/Telegram")
    csub = p.add_subparsers(dest="action", required=True)
    csub.add_parser("status")
    c = csub.add_parser("imessage")
    c.add_argument("--handle", help="your own phone number or Apple ID email")
    c.add_argument("--chat-guid")
    c.add_argument("--off", action="store_true")
    c = csub.add_parser("telegram")
    c.add_argument("--token")
    c.add_argument("--wait", action="store_true")
    c.add_argument("--off", action="store_true")
    c = csub.add_parser("test")
    c.add_argument("name", choices=["imessage", "telegram", "local"])
    c = csub.add_parser("result")
    c.add_argument("name", choices=["imessage", "telegram"])
    c.add_argument("result", choices=["buzz", "silent", "failed"])
    c = csub.add_parser("order")
    c.add_argument("names", help="comma-separated, e.g. imessage,telegram")
    p.set_defaults(fn=cmd_channel)

    p = sub.add_parser("monitor", help="read usage once and queue any due notifications")
    p.add_argument("--send", action="store_true", help="also deliver queued notifications")
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("daemon", help="the background service")
    p.add_argument("action", choices=["run", "install", "uninstall", "status"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_daemon)

    p = sub.add_parser("install-skill", help="link the Reset skill into Claude Code and Codex, and resetctl into ~/.local/bin")
    p.set_defaults(fn=cmd_install_skill)

    p = sub.add_parser("setup", help="guided setup; run a single step to script it (see AGENTS.md)")
    p.add_argument("step", nargs="?", choices=["check", "telegram", "brain", "access", "skill", "service", "test"])
    p.add_argument("--access", choices=["full", "sandboxed"], help="how much runs may do (with the access step)")
    p.add_argument("--token", help="Telegram bot token (prefer the hidden prompt)")
    p.add_argument("--use", help="brain order: auto, none, or a comma list of claude-code,codex")
    p.add_argument("--claude-model")
    p.add_argument("--codex-model")
    p.add_argument("--codex-effort")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("ask", help="ask Reset's AI brain something from the terminal")
    p.add_argument("question", nargs="+")
    p.add_argument("--brain", choices=list(brain.BACKENDS))
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("models", help="models and effort levels each engine offers")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("mcp")  # internal: Reset's tools over MCP for AI brains
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("doctor", help="check setup")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("worker")  # internal: started by the supervisor for each approved run
    p.add_argument("run_id", type=int)
    p.set_defaults(fn=cmd_worker)
    return parser


def cmd_worker(args) -> int:
    from resetagent import worker

    return worker.main(args.run_id)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args) or 0
