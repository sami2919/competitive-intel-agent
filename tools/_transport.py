"""Shared HTTP transport — the single seam all 9 tools route through (eng review D1/D3).

LiveTransport  -> real httpx calls with timeout + one retry + backoff, then raises.
ReplayTransport -> fixture lookup by request key (offline demo mode + recorded-fixture evals).

Retry/timeout/backoff lives HERE, once — not per tool. No per-vendor SDKs: the Apify
fallback also goes through this transport as raw httpx. ReplayTransport wraps the same
seam, so `make demo` replays recorded data-API responses with one surface to mock.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] | None = None
    json_body: dict | None = None
    # Per-request timeout override (seconds). None -> use the transport's default.
    # Used by long calls that exceed the 30s default, e.g. the Apify sync-actor-run
    # endpoint (run-sync-get-dataset-items blocks until the actor finishes). Not part
    # of key() — timeout is not request identity for fixture lookup.
    timeout: float | None = None

    def key(self) -> str:
        """Stable key for fixture lookup (method + url + sorted params + json body).

        Includes the JSON body so endpoints that share a URL but differ by body
        (e.g. Firecrawl /scrape with different target urls) get distinct fixtures.
        """
        parts = [f"{self.method} {self.url}"]
        if self.params:
            parts.append("?" + "&".join(f"{k}={v}" for k, v in sorted(self.params.items())))
        if self.json_body:
            parts.append("body=" + json.dumps(self.json_body, sort_keys=True))
        return " ".join(parts)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    async def request(self, req: HttpRequest) -> HttpResponse: ...


class TransportError(Exception):
    """Raised after retries are exhausted. Tools catch this and return ToolFailure."""


class LiveTransport:
    """Real HTTP with timeout, one retry + backoff, then raise TransportError."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        retries: int = 1,
        backoff: float = 0.5,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._owns_client = client is None

    async def request(self, req: HttpRequest) -> HttpResponse:
        last_exc: Exception | None = None
        # Per-request timeout override (e.g. Apify sync actor runs); else client default.
        timeout = req.timeout if req.timeout is not None else self._timeout
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.request(
                    req.method,
                    req.url,
                    headers=req.headers or None,
                    params=req.params or None,
                    json=req.json_body or None,
                    timeout=timeout,
                )
                return HttpResponse(
                    status=resp.status_code,
                    body=resp.text,
                    headers=dict(resp.headers),
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                last_exc = exc
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (attempt + 1))
        raise TransportError(
            f"{req.method} {req.url} failed after {self._retries + 1} attempts: {last_exc}"
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ReplayTransport:
    """Offline replay — looks up a recorded response by request key.

    Used by `make demo` (D3) and recorded-fixture evals. Missing fixture raises
    a typed error so demo failures are clear, not silent.
    """

    def __init__(self, fixtures: dict[str, HttpResponse]) -> None:
        self._fixtures = fixtures

    async def request(self, req: HttpRequest) -> HttpResponse:
        key = req.key()
        if key not in self._fixtures:
            raise TransportError(f"no replay fixture for {key}")
        return self._fixtures[key]
