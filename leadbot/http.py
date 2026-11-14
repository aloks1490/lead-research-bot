"""One HTTP client for every outbound REST call (Slack, Clay, HubSpot).

It does three things the same way for all of them:
  - a timeout on every call, so a hung API cannot hang a job
  - retries on rate limits (429), server errors (5xx) and network failures: wait for Retry-After when the
    server sends it, otherwise exponential backoff with jitter; at most `max_tries` attempts
  - one JSON log line per attempt with the service, status code and duration

Claude is the one exception: it goes through the official Anthropic SDK, which applies the same policy itself
(see brief.py).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger("leadbot.http")

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 30.0  # never sleep longer than this on a server's say-so


class ApiError(RuntimeError):
    """An outbound call failed for good: a non-retryable status, or retries ran out."""

    def __init__(self, service: str, endpoint: str, status: int | None, detail: str):
        self.service, self.endpoint, self.status, self.detail = service, endpoint, status, detail
        super().__init__(f"{service} {endpoint} failed: {status or 'network error'}: {detail[:300]}")


class HttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_tries: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        self.client = client
        self.max_tries = max_tries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep
        self.rng = rng

    async def request(
        self,
        service: str,
        method: str,
        url: str,
        *,
        endpoint: str,
        timeout: float,
        ok: tuple[int, ...] = (200, 201, 204),
        **kwargs,
    ) -> httpx.Response:
        """`endpoint` is a short label for logs and errors. It is passed in because some URLs hold secrets."""
        for attempt in range(1, self.max_tries + 1):
            started = time.monotonic()
            status: int | None = None
            detail = ""
            retry_after: float | None = None
            try:
                response = await self.client.request(method, url, timeout=timeout, **kwargs)
                status = response.status_code
                if status in ok:
                    self._log(service, endpoint, attempt, status, started)
                    return response
                detail = response.text
                retry_after = _retry_after(response)
                retryable = status in RETRY_STATUSES
            except (httpx.TimeoutException, httpx.TransportError) as e:
                detail = f"{type(e).__name__}: {e}"
                retryable = True
            self._log(service, endpoint, attempt, status, started, failed=True)
            if not retryable or attempt == self.max_tries:
                raise ApiError(service, endpoint, status, detail)
            await self.sleep(self._wait(attempt, retry_after))
        raise AssertionError("unreachable")

    def _wait(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER)
        ceiling = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
        return ceiling * self.rng()  # "full jitter": spreads retries so parallel jobs do not retry in step

    @staticmethod
    def _log(service: str, endpoint: str, attempt: int, status: int | None, started: float, failed: bool = False):
        fields = {"service": service, "endpoint": endpoint, "attempt": attempt, "status_code": status,
                  "duration_ms": round((time.monotonic() - started) * 1000)}
        log.log(logging.WARNING if failed else logging.INFO, "api call", extra=fields)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(float(value), 0.0) if value else None
    except ValueError:  # an HTTP date instead of seconds: fall back to backoff
        return None
