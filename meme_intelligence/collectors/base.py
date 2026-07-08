"""Shared HTTP collector behavior (Spec Part 2, Part 21, Part 32).

Every concrete collector gets, for free:

* rate limiting before each request (Rule 11)
* TTL caching of identical requests (Rule 10)
* retry with exponential backoff on transient failures (Rule 7)
* timeout, status-code, and payload error handling (Rule 6)
* proxy/CA-bundle awareness via ``trust_env`` (works in proxied environments)
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any, Mapping

import aiohttp

from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import (
    CollectorError,
    RateLimitedError,
    TransientCollectorError,
)
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.core.retry import retry_async

_ERROR_BODY_PREVIEW = 200  # max chars of an error response body to keep in messages


class BaseCollector:
    """Base class for HTTP-API data collectors.

    Subclasses call :meth:`_get_json` and normalize the payload into core
    models. Construction is cheap; the underlying HTTP session is created
    lazily and must be released with :meth:`close` (or use ``async with``).
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        rate_limiter: RateLimiter,
        cache: TTLCache | None = None,
        timeout_seconds: float = 10.0,
        retry_attempts: int = 4,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 8.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._cache = cache
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._session = session
        self._owns_session = session is None
        self._logger = get_logger(f"collectors.{name}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env picks up HTTPS_PROXY; the default SSL context honors
            # SSL_CERT_FILE, so custom CA bundles (proxied environments) work.
            connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, trust_env=True, connector=connector
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Release the HTTP session if this collector created it."""
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "BaseCollector":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
    ) -> Any:
        """GET ``base_url + path`` and return the parsed JSON payload.

        When ``cache_key`` is given, a fresh cached response short-circuits
        the request entirely — no rate-limit token is consumed.
        """
        if cache_key is not None and self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{self._base_url}/{path.lstrip('/')}"

        async def _request() -> Any:
            await self._rate_limiter.acquire()
            session = await self._get_session()
            try:
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        raise RateLimitedError(f"{self.name}: rate limited (429) on {url}")
                    if response.status >= 500:
                        raise TransientCollectorError(
                            f"{self.name}: server error {response.status} on {url}"
                        )
                    if response.status != 200:
                        body = (await response.text())[:_ERROR_BODY_PREVIEW]
                        raise CollectorError(
                            f"{self.name}: unexpected status {response.status} on {url}: {body}"
                        )
                    try:
                        return await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise CollectorError(f"{self.name}: invalid JSON from {url}: {exc}") from exc
            except asyncio.TimeoutError as exc:
                raise TransientCollectorError(f"{self.name}: timeout on {url}") from exc
            except aiohttp.ClientError as exc:
                raise TransientCollectorError(f"{self.name}: network error on {url}: {exc}") from exc

        data = await retry_async(
            _request,
            attempts=self._retry_attempts,
            base_delay=self._retry_base_delay,
            max_delay=self._retry_max_delay,
            logger=self._logger,
        )

        if cache_key is not None and self._cache is not None:
            await self._cache.set(cache_key, data, cache_ttl)
        return data
