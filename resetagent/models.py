"""What each engine can run: models and effort levels, read live from the CLIs and cached briefly."""
from __future__ import annotations

import re
import time

from resetagent.providers import claude, codex

EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_ALIASES = ("default", "fable", "opus", "sonnet", "haiku")
TTL = 600
_cache: dict = {}


class ModelError(ValueError):
    """The requested model or effort isn't available; the message lists what is."""


def catalog(cfg: dict, engine: str, refresh: bool = False) -> list | None:
    """[{id, name, efforts, default, description}] for an engine, or None when it can't be read."""
    hit = _cache.get(engine)
    if hit and not refresh and time.time() - hit[0] < TTL:
        return hit[1]
    models = _read(cfg, engine)
    if models:
        _cache[engine] = (time.time(), models)
    return models


def _read(cfg: dict, engine: str) -> list | None:
    reader = {"codex": (codex.resolve_bin, codex.list_models), "claude": (claude.resolve_bin, claude.list_models)}
    if engine not in reader:
        return None
    resolve, read = reader[engine]
    try:
        executable = resolve(cfg)
        return read(executable) if executable else None
    except Exception:
        return None


def engine_for(model: str, cfg: dict | None = None) -> str | None:
    """The engine that runs a model: by its name, else by which engine's model list has it ("sol")."""
    name = model.lower().strip()
    if name.startswith(("gpt", "codex", "o3", "o4")):
        return "codex"
    if name.startswith("claude") or name.split("[")[0] in CLAUDE_ALIASES:
        return "claude"
    if cfg is None:
        return None
    found = [engine for engine in ("codex", "claude") if find(catalog(cfg, engine) or [], model)]
    return found[0] if len(found) == 1 else None


def _key(text) -> str:
    return re.sub(r"[^a-z0-9.]", "", str(text or "").lower())


def find(models: list, model: str):
    """The entry for an id, alias or display name; a unique partial match works too ("sol", "fable")."""
    wanted = _key(model)
    for entry in models:
        if wanted in {_key(entry["id"]), _key(entry["id"].split("[")[0]), _key(entry.get("name"))}:
            return entry
    partial = [e for e in models if wanted and (wanted in _key(e["id"]) or wanted in _key(e.get("name")))]
    return partial[0] if len(partial) == 1 else None


def clamp(effort: str, supported: list) -> str | None:
    """The requested effort, or the strongest supported level below it."""
    if not supported:
        return None
    if effort in supported:
        return effort
    rank = {name: i for i, name in enumerate(EFFORTS)}
    below = [e for e in supported if rank.get(e, 0) <= rank.get(effort, len(EFFORTS))]
    if below:
        return max(below, key=lambda e: rank.get(e, 0))
    return min(supported, key=lambda e: rank.get(e, 0))


def label(entry: dict) -> str:
    if entry["id"] == "default" and entry.get("description"):
        return f"your default ({entry['description'].split(' with ')[0].split(' ·')[0]})"
    return entry.get("name") or entry["id"]


def choose(cfg: dict, engine: str, model: str | None = None, effort: str | None = None) -> dict:
    """Validate a model and effort for an engine. Returns {model, effort, label, note}.

    model None means the engine's default. An effort the model lacks steps down to the strongest one it has.
    """
    effort = (effort or cfg["runs"]["effort"]).lower()
    if effort not in EFFORTS:
        raise ModelError(f"Effort must be one of: {', '.join(EFFORTS)}.")
    models = catalog(cfg, engine)
    if not models:  # can't read the list: pass the choice through and let the engine decide
        return {"model": model, "effort": effort, "label": model or "the default model", "note": None}
    if model:
        entry = find(models, model)
        if entry is None:
            raise ModelError(f"{engine.capitalize()} models: " + ", ".join(e["id"] for e in models) + ".")
    else:
        entry = next((e for e in models if e.get("default")), None)
    if entry is None:
        return {"model": None, "effort": effort, "label": "the default model", "note": None}
    chosen = clamp(effort, entry.get("efforts") or [])
    note = None
    if chosen is None:
        note = f"{label(entry)} doesn't take an effort level"
    elif chosen != effort:
        note = f"{effort} isn't available for {label(entry)}, so it uses {chosen}"
    return {"model": entry["id"] if model else None, "effort": chosen, "label": label(entry), "note": note}
