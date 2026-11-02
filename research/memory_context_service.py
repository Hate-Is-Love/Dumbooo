"""Using the existing long-term memory for research — only when the request is personal.

"latest updates related to my DUMBOO project" looks up the user's own saved facts about
DUMBOO; "what's the latest AI news" looks up nothing. Retrieved memories are passed to
the AI as background (not evidence), sensitive memories are never used, and the user
id always comes from the session.
"""

from __future__ import annotations

import re

from app.event_bus import EventBus, Events
from research.evidence_extraction_service import terms
from services.auth_service import AuthError, Session
from services.memory_service import MemoryService

PERSONAL = re.compile(r"\b(my|our|mine|for me|i'm|i am|i have|i study|i work|my project|my course|my college|this "
                      r"course|these colleges|my team|my company)\b", re.I)


class MemoryContextService:
    def __init__(self, memory: MemoryService, auth, bus: EventBus) -> None:
        self._memory = memory
        self._auth = auth
        self._bus = bus

    @staticmethod
    def needed(text: str) -> bool:
        return bool(PERSONAL.search(text or ""))

    def lookup(self, session: Session, text: str, limit: int = 5) -> list[dict]:
        if not self.needed(text):
            return []
        try:
            if not self._auth.require(session).can("memory.use"):
                return []
        except AuthError:
            return []
        words = [t for t in terms(text) if t not in ("my", "our", "related", "updates")]
        self._bus.publish(Events.MEMORY_RETRIEVAL, {"seq": -1, "phase": "start", "memories": 0, "messages": 0})
        rows: list[dict] = []
        try:
            rows = self._memory.search(session, " ".join(words) or text, limit=limit * 2)
            if not rows:
                # full-text search ignores words like "dumboo" (the wake word); match the user's own memories directly
                specific = [w for w in words if w not in ("project", "latest", "news")]
                rows = [m for m in self._memory.list(session)
                        if any(w in (m["memory_key"] + " " + m["memory_value"]).lower() for w in specific)]
        except Exception:
            rows = []
        rows = [r for r in rows if r.get("sensitivity") != "sensitive"][:limit]
        self._bus.publish(Events.MEMORY_RETRIEVAL, {"seq": -1, "phase": "done", "memories": len(rows), "messages": 0})
        return rows

    @staticmethod
    def as_context(rows: list[dict]) -> str:
        return "\n".join(f"- {r['memory_key']}: {r['memory_value']} (saved {r['updated_at'][:10]})" for r in rows)

    @staticmethod
    def labels(rows: list[dict]) -> list[str]:
        return [f"{r['memory_key']}: {r['memory_value']}"[:120] for r in rows]

    def remember_findings(self, session: Session, key: str, value: str, category: str) -> dict:
        result = self._memory.remember(session, key[:80], value[:500], category, verification="user_confirmed")
        self._bus.publish(Events.MEMORY_CHANGED, {"action": "write", "count": 1})
        return result.memory
