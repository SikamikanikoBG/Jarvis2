"""Shared HTTP streaming plumbing for the Ollama and OpenAI-compatible adapters.

Retry policy: connection errors and 5xx *before the first byte* are retried with backoff
(max 2 retries). Once a byte has arrived we never retry — that would duplicate output.
4xx is never retried. Cancellation is checked between chunks and closes the response.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis_core.models.base import ModelCancelled, ModelError

log = logging.getLogger(__name__)

_RETRIES = 2
_BACKOFF_S = (0.5, 2.0)


async def stream_lines(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    cancel: asyncio.Event,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    """POST ``payload`` and yield response lines. Handles retry-before-first-byte and cancel."""
    attempt = 0
    while True:
        first_byte = False
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    retryable = resp.status_code >= 500
                    if retryable and attempt < _RETRIES:
                        raise _Retry(f"HTTP {resp.status_code}: {body}")
                    raise ModelError(f"HTTP {resp.status_code}: {body}", retryable=retryable, status=resp.status_code)
                async for line in resp.aiter_lines():
                    first_byte = True
                    if cancel.is_set():
                        raise ModelCancelled
                    if line:
                        yield line
                return
        except _Retry as exc:
            attempt += 1
            log.warning("model call retry %d/%d: %s", attempt, _RETRIES, exc)
            await asyncio.sleep(_BACKOFF_S[min(attempt - 1, len(_BACKOFF_S) - 1)])
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout) as exc:
            if first_byte or attempt >= _RETRIES:
                raise ModelError(f"{type(exc).__name__}: {exc}", retryable=not first_byte) from exc
            attempt += 1
            log.warning("model call retry %d/%d after %s", attempt, _RETRIES, type(exc).__name__)
            await asyncio.sleep(_BACKOFF_S[min(attempt - 1, len(_BACKOFF_S) - 1)])


class _Retry(Exception):
    pass


def make_client(timeout_s: int) -> httpx.AsyncClient:
    # Generous read timeout: a 27B model can pause several seconds between tokens under load.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=float(timeout_s), write=30.0, pool=10.0),
        http2=False,
    )


class Stopwatch:
    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.first: float | None = None

    def mark_first(self) -> None:
        if self.first is None:
            self.first = time.perf_counter()

    @property
    def ttft_ms(self) -> int | None:
        return None if self.first is None else int((self.first - self.start) * 1000)

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
