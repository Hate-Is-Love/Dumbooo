"""Picking the passages of a page that are relevant to the question (local, no AI needed).

Pages are split into paragraphs; each paragraph is scored by how many of the
question's words it contains (rarer words count more). Only the best passages are
sent to the AI, which keeps prompts small and keeps whole articles out of storage.
"""

from __future__ import annotations

import math
import re

STOP = {"the", "and", "for", "with", "that", "this", "what", "which", "about", "from", "are", "was", "were", "has",
        "have", "how", "why", "when", "where", "who", "latest", "recent", "news", "today", "please", "give", "tell",
        "find", "search", "show", "explain", "compare", "using", "current", "information", "official", "sources",
        "source", "research", "detailed", "report", "references", "best", "into", "your", "you", "can", "does"}


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.-]{1,}", (text or "").lower()) if w not in STOP]


def paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z0-9•\-*])", text or "") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in parts:  # merge very short lines (headings, list items) with what follows
        buf = f"{buf} {p}".strip() if len(buf) < 200 else buf
        if len(buf) >= 200:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def best_passages(text: str, question: str, max_chars: int = 1800, max_passages: int = 4) -> list[str]:
    q = set(terms(question))
    paras = paragraphs(text)
    if not paras:
        return []
    if not q:
        return [paras[0][:max_chars]]
    df = {t: sum(1 for p in paras if t in p.lower()) for t in q}
    scored = []
    for i, p in enumerate(paras):
        low = p.lower()
        s = sum(math.log(1 + len(paras) / (1 + df[t])) for t in q if t in low)
        if s > 0:
            scored.append((s - i * 0.002, i, p))  # earlier paragraphs win ties (leads, summaries)
    if not scored:
        return [paras[0][: max_chars // 2]]
    chosen = sorted(sorted(scored, reverse=True)[:max_passages], key=lambda x: x[1])
    out, used = [], 0
    for _s, _i, p in chosen:
        piece = p[: max(200, max_chars - used)]
        out.append(piece)
        used += len(piece)
        if used >= max_chars:
            break
    return out
