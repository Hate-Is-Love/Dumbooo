"""Feature 8: memory-aware personalisation on top of the existing long-term memory.

Automation knowledge is stored as ordinary Module 3 memories of the signed-in
user, with predictable keys, so it shows up (and can be edited or deleted) on
the MEMORY page and the AUTOMATION → MEMORY tab:

=====================================  ==========================================
key                                    value
=====================================  ==========================================
``<name> project path``                full folder path (category ``project``)
``usual <purpose> application``        an approved app name, e.g. "VS Code"
``<name> website``                     a URL
``<name> folder``                      full folder path
``workflow <name>``                    readable description of a saved task
=====================================  ==========================================

Rules followed here: memories are only read with the user's own session (the
user id never comes from the AI or the UI); only the few records needed for
the current request are retrieved; a remembered path or URL is checked
*now* before it is used; nothing is guessed — when nothing (or more than one
thing) matches, the caller asks the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.event_bus import Events
from automation.actions.common import resolve_app
from automation.config import AppEntry
from automation.registry import ExecContext
from automation.safety import SafetyError, check_url

PURPOSE_WORDS = {"coding": ("coding", "code", "programming", "development", "dev", "editor", "ide"),
                 "browser": ("browser", "browsing", "web"), "writing": ("writing", "notes", "text", "document"),
                 "music": ("music",), "email": ("email", "mail")}


@dataclass
class Found:
    value: str
    memory: dict | None = None
    problem: str = ""          # why it can't be used now (missing, not permitted …)
    options: list[dict] | None = None


def _retrieval(ctx: ExecContext, phase: str, count: int = 0) -> None:
    ctx.services.bus.publish(Events.MEMORY_RETRIEVAL, {"seq": -1, "phase": phase, "memories": count, "messages": 0})


def _memories(ctx: ExecContext, query: str, limit: int = 8) -> list[dict]:
    if not ctx.user.can("memory.use"):
        return []
    _retrieval(ctx, "start")
    rows: list[dict] = []
    try:
        rows = ctx.services.memory.search(ctx.session, query, limit=limit)
    finally:
        _retrieval(ctx, "done", len(rows))
    return rows


def all_of_kind(ctx: ExecContext, suffix: str) -> list[dict]:
    if not ctx.user.can("memory.use"):
        return []
    return [m for m in ctx.services.memory.list(ctx.session) if m["key_norm"].endswith(suffix)]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


# ------------------------------------------------------------------ projects
def project_key(name: str) -> str:
    return f"{_norm(name)} project path"


def _path_status(ctx: ExecContext, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        return f"the saved location “{value}” is not a full folder path"
    if not path.exists():
        return f"the saved folder {value} no longer exists"
    try:
        ctx.services.guard.check(value)
    except SafetyError as exc:
        return str(exc)
    return ""


def find_project(ctx: ExecContext, name: str | None) -> Found | None:
    """The user's saved project folder (verified to exist now). ``None`` = nothing saved."""
    candidates = all_of_kind(ctx, "project path")
    if not candidates:
        _retrieval(ctx, "start")
        _retrieval(ctx, "done", 0)
        return None
    _retrieval(ctx, "start")
    _retrieval(ctx, "done", len(candidates))
    if name:
        wanted = _norm(name)
        exact = [m for m in candidates if m["key_norm"] == project_key(wanted)]
        loose = [m for m in candidates if wanted and wanted in m["key_norm"]]
        matches = exact or loose
    else:
        matches = candidates
    if not matches:
        return None
    if len(matches) > 1:
        return Found("", options=matches, problem="more than one saved project matches")
    memory = matches[0]
    return Found(memory["memory_value"], memory, _path_status(ctx, memory["memory_value"]))


def project_name(memory: dict) -> str:
    return memory["memory_key"][: -len(" project path")] if memory["memory_key"].lower().endswith(" project path") \
        else memory["memory_key"]


# ------------------------------------------------------------------ apps
def usual_app(ctx: ExecContext, purpose: str) -> tuple[AppEntry | None, Found | None]:
    """The user's preferred application for a purpose ("coding" …), resolved against the approved list."""
    words = PURPOSE_WORDS.get(purpose, (purpose,))
    rows = _memories(ctx, " ".join(words) + " application app editor", limit=10)
    relevant = [m for m in rows if any(w in m["key_norm"].split() for w in words)
                and re.search(r"\b(app|application|editor|ide|browser|program)\b", m["key_norm"])]
    if not relevant:
        return None, None
    memory = relevant[0]
    app = resolve_app(ctx, memory["memory_value"])
    if app is None:
        return None, Found(memory["memory_value"], memory,
                           f"your saved {memory['memory_key']} is “{memory['memory_value']}”, which is not an approved "
                           "application")
    if app.resolve() is None and not app.uri:
        return None, Found(memory["memory_value"], memory, f"{app.name} is not installed")
    return app, Found(memory["memory_value"], memory)


def installed_editors(ctx: ExecContext) -> list[AppEntry]:
    return [a for a in ctx.services.config.applications.values()
            if a.kind == "editor" and "folder" in a.opens and a.resolve() is not None]


# ------------------------------------------------------------------ websites / folders / workflows
def saved_website(ctx: ExecContext, name: str | None) -> Found | None:
    rows = all_of_kind(ctx, "website")
    _retrieval(ctx, "start")
    _retrieval(ctx, "done", len(rows))
    if name:
        wanted = _norm(name)
        rows = [m for m in rows if wanted in m["key_norm"]]
    if not rows:
        return None
    if len(rows) > 1:
        return Found("", options=rows, problem="more than one saved website matches")
    memory = rows[0]
    try:
        check_url(memory["memory_value"], ctx.services.config.browser.get("blocked_domains"))
        problem = ""
    except SafetyError as exc:
        problem = str(exc)
    return Found(memory["memory_value"], memory, problem)


def saved_folder(ctx: ExecContext, name: str) -> Found | None:
    wanted = f"{_norm(name)} folder"
    rows = [m for m in all_of_kind(ctx, "folder") if m["key_norm"] == wanted]
    if not rows:
        return None
    memory = rows[0]
    return Found(memory["memory_value"], memory, _path_status(ctx, memory["memory_value"]))


def saved_workflow(ctx: ExecContext, name: str | None) -> Found | None:
    rows = []
    if ctx.user.can("memory.use"):
        rows = [m for m in ctx.services.memory.list(ctx.session) if m["key_norm"].startswith("workflow ")]
    _retrieval(ctx, "start")
    _retrieval(ctx, "done", len(rows))
    if name:
        wanted = _norm(name)
        rows = [m for m in rows if wanted in m["key_norm"]] or [m for m in rows if any(
            w in m["key_norm"] for w in wanted.split() if len(w) > 3)]
    if not rows:
        return None
    if len(rows) > 1:
        return Found("", options=rows, problem="more than one saved workflow matches")
    return Found(rows[0]["memory_value"], rows[0])


def remember(ctx: ExecContext, key: str, value: str, category: str) -> dict:
    """Save (or update) an automation memory for the signed-in user; returns the stored row."""
    result = ctx.services.memory.remember(ctx.session, key, value, category, verification="user_confirmed")
    ctx.services.bus.publish(Events.MEMORY_CHANGED, {"action": "unchanged" if result.action == "unchanged" else "write",
                                                     "count": 1})
    return result.memory
