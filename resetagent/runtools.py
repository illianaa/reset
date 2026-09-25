"""Which of the user's own tools runs may use: connectors (Gmail, Slack, GitHub…), plugins and MCP servers.

runs.tools is "all", or a list of names, empty by default: then runs have only their built-in tools (the shell,
files, the web) and Reset's question tool. A name covers both engines and matches loosely, so "google drive" is
claude.ai's "Google Drive" connector and "github" is Codex's GitHub connector and plugin.

Codex runs: just before the run's own app-server starts, a short-lived one reports the user's connectors, plugins and
MCP servers (no model is involved). Every one that isn't allowed is switched off for the run's Codex process only,
with -c overrides; the user's config isn't touched, so a run's chat continued in the Codex app has everything again.
Claude runs: with no tools allowed, Claude Code loads no MCP servers at all. With names, it loads the user's servers
and connectors, and Reset's hook refuses any MCP tool that isn't allowed, so an unknown one is refused too.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from resetagent.providers import claude as claude_provider
from resetagent.providers import codex

ALL = "all"
SCOUT_METHODS = frozenset({"initialize", "config/read", "plugin/installed", "app/installed"})
# Claude Code asks Reset about every MCP tool call (a hook it registers when the session starts).
CLAUDE_HOOK_ID = "reset-run-tools"
CLAUDE_HOOKS = {"PreToolUse": [{"matcher": "mcp__.*", "hookCallbackIds": [CLAUDE_HOOK_ID]}]}
CLAUDE_CONNECTOR = "claude.ai "  # how Claude Code names the user's claude.ai connectors


class ToolsError(RuntimeError):
    """The user's tools couldn't be checked, so a run that must leave some out can't start."""


def normalize(name: str) -> str:
    """"Google Drive", "google-drive" and "google_drive" are the same name."""
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def allowed(cfg: dict):
    """ALL, or the set of normalized names runs may use (empty: none). Anything unexpected allows nothing."""
    value = cfg["runs"].get("tools")
    if value == ALL:
        return ALL
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, list):
        return set()
    return {normalize(v) for v in value if normalize(v)}


def matches(name: str, names) -> bool:
    if names == ALL:
        return True
    key = normalize(name)
    # claude.ai's connectors reach tool names as "claude_ai_<Name>"
    return key in names or (key.startswith("claudeai") and key[len("claudeai"):] in names)


def describe(names) -> str:
    """For the run's brief: what it may use of the user's tools."""
    if names == ALL:
        return ""
    if not names:
        return ("Of the user's connected tools (connectors like Gmail or Slack, plugins, MCP servers), this run has "
                "none: use your built-in tools.")
    return (f"Of the user's connected tools (connectors, plugins, MCP servers), this run may use only: "
            f"{', '.join(sorted(names))}. Others are switched off or refused.")


# --- Codex ---------------------------------------------------------------------------------------------------

def codex_inventory(executable: str, cwd: str | None = None) -> dict:
    """The user's MCP servers, plugins and connectors, as a Codex started in cwd would load them."""
    client = codex.AppServer(executable, allowed=SCOUT_METHODS, cwd=cwd)
    try:
        codex.initialize(client, "reset_tools")
        config = (client.call("config/read", {"cwd": cwd} if cwd else {}, timeout=20) or {}).get("config") or {}
        plugins = client.call("plugin/installed", {}, timeout=20) or {}
        apps = client.call("app/installed", {}, timeout=20) or {}
    finally:
        client.close()
    installed = [f"{p['name']}@{m['name']}" for m in plugins.get("marketplaces") or [] for p in m.get("plugins") or []
                 if p.get("name") and p.get("installed", True)]
    return {"servers": sorted(name for name, server in (config.get("mcp_servers") or {}).items()
                              if (server or {}).get("enabled", True) is not False),
            # what's installed, and what the config names (the same plugin can appear under another marketplace)
            "plugins": sorted(set(installed) | set(config.get("plugins") or {})),
            "apps": [{"id": a["id"], "name": a.get("runtimeName") or a["id"]} for a in apps.get("apps") or []
                     if a.get("id")]}


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}={_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def codex_flags(inventory: dict, names) -> list:
    """app-server flags that switch off, for this process only, each of the user's tools runs may not use."""
    if names == ALL:
        return []
    flags = []
    servers = {s: {"enabled": False} for s in inventory["servers"] if s != "reset" and not matches(s, names)}
    if servers:
        flags += ["-c", "mcp_servers=" + _toml(servers)]
    plugins = [p for p in inventory["plugins"] if not matches(p.split("@")[0], names)]
    if len(plugins) == len(inventory["plugins"]):
        flags += ["--disable", "plugins"]
    elif plugins:  # (quoted keys like "github@openai-curated" only work in an inline table)
        flags += ["-c", "plugins=" + _toml({p: {"enabled": False} for p in plugins})]
    apps = [a["id"] for a in inventory["apps"] if matches(a["name"], names)]
    if apps:
        flags += ["-c", "apps=" + _toml({"_default": {"enabled": False}, **{a: {"enabled": True} for a in apps}})]
    else:
        flags += ["--disable", "apps"]
    return flags


def codex_limits(executable: str, cfg: dict, cwd: str) -> list:
    names = allowed(cfg)
    if names == ALL:
        return []
    try:
        return codex_flags(codex_inventory(executable, cwd), names)
    except Exception as exc:  # fail closed: a run that can't leave the user's tools out doesn't start
        raise ToolsError(f"Reset couldn't check which of your Codex tools this run would get ({exc}), so it didn't "
                         "start. Updating Codex usually fixes this; or let runs use all your tools.") from None


# --- Claude --------------------------------------------------------------------------------------------------

def claude_args(cfg: dict) -> list:
    """No tools allowed: Claude Code loads no MCP servers at all. Otherwise they load (the hook guards names)."""
    return [] if allowed(cfg) else ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


def claude_hooks(cfg: dict):
    return None if allowed(cfg) == ALL else CLAUDE_HOOKS


def claude_server(tool_name: str) -> str:
    """"mcp__claude_ai_Gmail__search" -> "claude_ai_Gmail"."""
    parts = str(tool_name).split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""


def claude_decision(names, tool_name: str) -> dict:
    """Reset's answer to Claude Code's hook: nothing to say (the call goes ahead as usual), or a refusal."""
    if not str(tool_name).startswith("mcp__") or matches(claude_server(tool_name), names):
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "The user hasn't allowed Reset runs to use this tool. Do without it, and "
                                    "mention it in your final summary."}}


def claude_inventory(executable: str) -> list:
    """The user's MCP servers and claude.ai connectors (`claude mcp list`, which checks each one, no model)."""
    result = subprocess.run([executable, "mcp", "list"], capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL, cwd=str(Path.home()))
    servers = []
    for line in result.stdout.splitlines():
        match = re.match(r"^(.+?): .* - (.+)$", line.strip())  # "claude.ai Slack: https://… - ✔ Connected"
        if match:
            name = match.group(1).strip()
            servers.append({"name": name[len(CLAUDE_CONNECTOR):] if name.startswith(CLAUDE_CONNECTOR) else name,
                            "kind": "connector" if name.startswith(CLAUDE_CONNECTOR) else "MCP server",
                            "status": re.sub(r"^\W+", "", match.group(2)).strip()})
    return servers


# --- For Reset's AI: what there is, and what runs may use ---------------------------------------------------

def inventory(cfg: dict) -> dict:
    names = allowed(cfg)
    found, notes = {}, []

    def add(name, engine, kind):
        entry = found.setdefault(normalize(name), {"name": name, "codex": [], "claude": []})
        if kind not in entry[engine]:
            entry[engine].append(kind)

    executable = codex.resolve_bin(cfg)
    if executable:
        try:
            tools = codex_inventory(executable)
            for app in tools["apps"]:  # first, so a name is shown as the service writes it ("GitHub")
                add(app["name"], "codex", "connector")
            for server in tools["servers"]:
                add(server, "codex", "MCP server")
            for plugin in tools["plugins"]:
                add(plugin.split("@")[0], "codex", "plugin")
        except Exception as exc:
            notes.append(f"Couldn't read Codex's tools: {exc}")
    executable = claude_provider.resolve_bin(cfg)
    if executable:
        try:
            for server in claude_inventory(executable):
                add(server["name"], "claude", server["kind"])
        except Exception as exc:
            notes.append(f"Couldn't read Claude Code's tools: {exc}")
    listed = [dict(entry, allowed=matches(entry["name"], names)) for _, entry in sorted(found.items())]
    return {"tools": listed, "notes": notes}
