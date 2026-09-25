"""Reset's tools as a stdio MCP server (newline-delimited JSON-RPC), for AI brains in Claude Code and Codex.

Run as `python -m resetagent mcp`. A Codex run's own ask_user tool is served the same way
(`python -m resetagent ask-server <run>`, see asking.py). Stdlib only.
"""
from __future__ import annotations

import json
import sys

from resetagent import __version__, tools

PROTOCOL = "2025-06-18"


def handle(message: dict, toolset=tools) -> dict | None:
    """toolset: anything with definitions() and call(name, arguments), the brain's tools by default."""
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        return None  # notifications need no answer
    params = message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "reset", "version": __version__}}
    elif method == "tools/list":
        result = {"tools": toolset.definitions()}
    elif method == "tools/call":
        output = toolset.call(params.get("name"), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                  "isError": "error" in output}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(stdin=None, stdout=None, toolset=tools) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            reply = handle(message, toolset)
        except Exception as exc:  # never crash the host agent's session
            reply = {"jsonrpc": "2.0", "id": message.get("id"),
                     "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:200]}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0
