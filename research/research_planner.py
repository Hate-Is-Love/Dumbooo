"""Breaking a complex question into sub-questions and search queries (AI, with a safe fallback)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from research.ai_json import AIUnavailable, Generate, ask_json, text

PLANNER_SYSTEM = """You plan web research for a desktop assistant. Today is {today}.
Return ONLY JSON: {{"sub_questions": [{{"question": "...", "queries": ["...", "..."]}}], "needs_news": true|false}}
Rules: 2 to {max_sub} sub-questions that together answer the user's question; 1-2 short search-engine queries each
(no quotes or operators); prefer queries that find official/primary sources (add "official" or the organisation
name when useful); set needs_news true only if recent events matter. User context, if given, is background only."""


@dataclass
class SubQuestion:
    question: str
    queries: list[str]
    source_ids: list[int] = field(default_factory=list)


@dataclass
class ResearchPlan:
    sub_questions: list[SubQuestion]
    needs_news: bool = False
    by_ai: bool = False


def fallback_plan(question: str) -> ResearchPlan:
    """Without the AI: split "compare A, B and C" into one search per item, otherwise one search."""
    q = question.strip().rstrip("?.")
    m = re.match(r"^(?:compare|comparison of|differences? between)\s+(?:the\s+)?(.+?)(?:\s+(?:using|with|based on)\s+.+)?$",
                 q, re.I)
    if m:
        items = [i.strip() for i in re.split(r",\s*|\s+(?:and|vs\.?|versus)\s+", m.group(1)) if len(i.strip()) > 1]
        if 2 <= len(items) <= 4:
            return ResearchPlan([SubQuestion(f"What about {i}?", [i]) for i in items] +
                                [SubQuestion(question, [q])])
    return ResearchPlan([SubQuestion(question, [q])])


class ResearchPlanner:
    def __init__(self, generate: Generate | None) -> None:
        self._generate = generate

    def plan(self, question: str, *, max_searches: int, context: str = "", today: datetime | None = None) -> ResearchPlan:
        max_sub = max(2, min(4, max_searches - 1))
        system = PLANNER_SYSTEM.format(today=(today or datetime.now()).strftime("%d %B %Y"), max_sub=max_sub)
        prompt = f"Question: {question}" + (f"\n\nUser context (background only):\n{context}" if context else "")
        try:
            data = ask_json(self._generate, system, prompt, max_tokens=1024)
        except AIUnavailable:
            return fallback_plan(question)
        subs: list[SubQuestion] = []
        budget = max_searches
        for item in (data.get("sub_questions") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict) or budget <= 0:
                continue
            sq = text(item.get("question"), 300)
            queries = [text(q, 200) for q in (item.get("queries") or []) if text(q, 200)][:2]
            queries = [q for q in queries if q][: max(1, min(2, budget))]
            if sq and queries:
                subs.append(SubQuestion(sq, queries))
                budget -= len(queries)
            if len(subs) >= max_sub:
                break
        if not subs:
            return fallback_plan(question)
        return ResearchPlan(subs, bool(data.get("needs_news")), by_ai=True)
