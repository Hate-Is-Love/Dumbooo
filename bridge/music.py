"""“Play <song> on my phone”: find the song on YouTube so the phone plays it straight away (no results list).

Looks up the first video for the search on YouTube's public results page (no account or API key). Only the words
of the request are sent to YouTube. Returns None if it can't tell; the phone then uses its music app's search.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"')
_ANY_VIDEO = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


def youtube_video_id(query: str, timeout: float = 5.0) -> str | None:
    query = " ".join((query or "").split())[:150]
    if not query:
        return None
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.6"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read(2_000_000).decode("utf-8", "replace")
    except Exception as exc:
        log.info("Song lookup failed: %s", exc)
        return None
    match = _VIDEO.search(page) or _ANY_VIDEO.search(page)
    return match.group(1) if match else None
