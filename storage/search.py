"""Turn free text into a safe SQLite FTS5 query.

User text is never passed to FTS5 directly (its query syntax could raise errors
or be abused). Words are extracted, stop-words dropped, and each remaining
word becomes a quoted prefix term joined with OR. Ranking (bm25) then favours
records matching more and rarer words.
"""

from __future__ import annotations

import re

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could", "did", "do", "does", "for", "from",
    "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "please", "so", "tell", "that", "the", "their", "them", "then", "there", "these", "this", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your", "about",
    "remember", "know", "dumboo", "hey", "again", "any", "some", "called", "name", "named",
}


def keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", (text or "").lower())
    out: list[str] = []
    for word in words:
        word = word.strip("'_-")
        if len(word) < 2 or word in STOPWORDS or word in out:
            continue
        out.append(word)
    return out[:limit]


def fts_query(text: str, limit: int = 8) -> str | None:
    terms = keywords(text, limit)
    if not terms:
        return None
    return " OR ".join(f'"{t.replace(chr(34), "")}"*' for t in terms)
