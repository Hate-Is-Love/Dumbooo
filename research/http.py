"""Shared HTTP handling for search providers: timeouts, status codes -> SearchError kinds.

API keys always travel in headers, never in URLs, so they can't appear in logs.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from research.search_provider import SearchError

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; DUMBOO-Research/1.0; personal desktop assistant)"
ClientFactory = Callable[..., httpx.Client]


def default_client(timeout: float = 15.0, **kw: Any) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, **kw.pop("headers", {})}
    kw.setdefault("follow_redirects", True)
    return httpx.Client(timeout=timeout, headers=headers, **kw)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def check_response(response: httpx.Response, provider: str) -> None:
    code = response.status_code
    if code < 400:
        return
    if code in (401, 403):
        raise SearchError("auth", provider, f"HTTP {code}")
    if code == 429:
        raise SearchError("rate_limit", provider, "HTTP 429", _retry_after(response))
    if code in (402, 432, 433):
        raise SearchError("quota", provider, f"HTTP {code}")
    if code >= 500:
        raise SearchError("unavailable", provider, f"HTTP {code}")
    raise SearchError("bad_response", provider, f"HTTP {code}")


def request(client_factory: ClientFactory, provider: str, method: str, url: str, timeout: float,
            **kw: Any) -> httpx.Response:
    try:
        with client_factory(timeout=timeout) as client:
            response = client.request(method, url, **kw)
    except httpx.TimeoutException as exc:
        raise SearchError("timeout", provider) from exc
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        raise SearchError("network", provider, exc.__class__.__name__) from exc
    except httpx.HTTPError as exc:
        raise SearchError("network", provider, exc.__class__.__name__) from exc
    check_response(response, provider)
    return response
