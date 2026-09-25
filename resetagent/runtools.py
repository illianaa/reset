"""Which of the user's own tools runs may use: connectors (Gmail, Slack, GitHub…), plugins and MCP servers.

runs.tools is "all", or a list of names, empty by default: then runs have only their built-in tools (the shell,
files, the web) and Reset's question tool. A name covers both engines and matches loosely, so "google drive" is
claude.ai's "Google Drive" connector and "github" is Codex's GitHub connector and plugin.

Codex runs: just before the run's own app-server starts, a short-lived one reports the user's connectors, plugins and
MCP servers (no model is involved). Every one that isn't allowed is switched off for the run's Codex process only,
with -c overrides; the user's config isn't touched, so a run's chat continued in the Codex app has everything again.
Claude runs: with no tools allowed, Claude Code loads no MCP servers at all. Otherwise it loads the user's servers and
connectors, and Reset's hook refuses any MCP tool (or MCP resource) that isn't allowed, reading the setting at each
call, so an unknown one is refused too, and a narrower setting reaches runs already going. A server the run's own
folder defines (.mcp.json) is refused unless everything is allowed, so a repo can't pass one off as an allowed name.

This keeps a run's tools to what the user chose. A run with full access can still start other programs as the user,
so it guards against mistakes, not against a run set on getting around it: sandboxed access is the hard boundary.
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
# Claude Code asks Reset about every MCP tool call (a hook it registers when the session starts). Its resource
# tools reach any server by name, so they're asked about too.
RESOURCE_TOOLS = ("ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool")
CLAUDE_HOOK_ID = "reset-run-tools"
CLAUDE_HOOKS = {"PreToolUse": [{"matcher": "mcp__.*|" + "|".join(RESOURCE_TOOLS), "hookCallbackIds": [CLAUDE_HOOK_ID]}]}
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
        read = client.call("config/read", {"cwd": cwd} if cwd else {}, timeout=20)
        plugins = client.call("plugin/installed", {}, timeout=20)
        apps = client.call("app/installed", {}, timeout=20)
    finally:
        client.close()
    if not isinstance((read or {}).get("config"), dict) or not isinstance(plugins, dict) or not isinstance(apps, dict):
        raise ToolsError("Codex didn't report its tools")  # (so nothing is left on by mistake)
    config = read["config"]
    installed = [f"{p['name']}@{m['name']}" for m in plugins.get("marketplaces") or [] for p in m.get("plugins") or []
                 if p.get("name") and p.get("installed", True)]
    connectors = {a["id"]: a.get("runtimeName") or a["id"] for a in apps.get("apps") or [] if a.get("id")}
    for app in config.get("apps") or {}:  # a connector the config names is on unless switched off, even if unlisted
        if app != "_default":
            connectors.setdefault(app, app)
    return {"servers": sorted(name for name, server in (config.get("mcp_servers") or {}).items()
                              if (server or {}).get("enabled", True) is not False),
            # what's installed, and what the config names (the same plugin can appear under another marketplace)
            "plugins": sorted(set(installed) | set(config.get("plugins") or {})),
            "apps": [{"id": i, "name": n} for i, n in connectors.items()]}


def _toml(value) -> str:
    """A TOML value for a -c override. JSON strings are TOML strings, once non-ASCII is left as it is (JSON would
    escape an emoji as two halves, which TOML rejects)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)}={_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def codex_flags(inventory: dict, names) -> list:
    """app-server flags that switch off, for this process only, each of the user's tools runs may not use."""
    if names == ALL:
        return []
    flags = []  # (Reset's own question tool is switched on after these, by worker.run_tools_config)
    servers = {s: {"enabled": False} for s in inventory["servers"] if not matches(s, names)}
    if servers:
        flags += ["-c", "mcp_servers=" + _toml(servers)]
    plugins = [p for p in inventory["plugins"] if not matches(p.split("@")[0], names)]
    if len(plugins) == len(inventory["plugins"]):
        flags += ["--disable", "plugins"]
    elif plugins:  # (quoted keys like "github@openai-curated" only work in an inline table)
        flags += ["-c", "plugins=" + _toml({p: {"enabled": False} for p in plugins})]
    apps = {a["id"]: matches(a["name"], names) for a in inventory["apps"]}
    if any(apps.values()):  # each one named, since a config entry for a connector would switch it back on
        flags += ["-c", "apps=" + _toml({"_default": {"enabled": False},
                                         **{a: {"enabled": on} for a, on in apps.items()}})]
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


def project_servers(folder: str) -> frozenset:
    """Normalized names of the MCP servers a run's folder (or a folder above it) defines in .mcp.json."""
    names = set()
    for place in [Path(folder), *Path(folder).parents]:
        try:
            servers = json.loads((place / ".mcp.json").read_text()).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            continue
        names |= {normalize(name) for name in servers if isinstance(name, str)}
    return frozenset(names)


def claude_server(tool_name: str) -> str:
    """"mcp__claude_ai_Gmail__search" -> "claude_ai_Gmail"."""
    parts = str(tool_name).split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""


REFUSED = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "The user hasn't allowed Reset runs to use this tool. Do without it, and mention it in "
                                "your final summary."}}


def claude_decision(names, tool_name: str, tool_input=None, local=frozenset()) -> dict:
    """Reset's answer to Claude Code's hook: nothing to say (the call goes ahead as usual), or a refusal.
    local: servers the run's own folder defines, refused unless everything is allowed."""
    if tool_name in RESOURCE_TOOLS:
        server = str((tool_input or {}).get("server") or "")  # none: every server's resources
    elif str(tool_name).startswith("mcp__"):
        server = claude_server(tool_name)
    else:
        return {}  # the run's built-in tools aren't the user's to limit here
    if names == ALL or (server and matches(server, names) and normalize(server) not in local):
        return {}
    return REFUSED


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
