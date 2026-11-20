"""Alerts to the phone via ntfy (spec Section 9), rate-limited per key so
a flapping condition can't spam: the same ``key`` (e.g.
``sim/crypto/stale_data``) is sent at most once per ``min_interval``.

ntfy's publish API is a plain HTTP POST of the message body to
``{url}/{topic}``, with ``Title``/``Priority``/``Tags`` headers and an
optional ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)


class Alerter(Protocol):
    def send(self, title: str, message: str, key: str, priority: str = "default") -> bool:
        """True if actually sent (False when rate-limited or failed)."""
        ...


@dataclass
class RateLimiter:
    min_interval: float
    clock: Callable[[], float] = time.monotonic
    _last: dict[str, float] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.min_interval:
            return False
        self._last[key] = now
        return True


class NtfyAlerter:
    def __init__(
        self,
        url: str,
        topic: str,
        token: str | None = None,
        min_interval_seconds: float = 1800,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = f"{url.rstrip('/')}/{topic}"
        self._token = token
        self._limiter = RateLimiter(min_interval_seconds)
        self._client = client or httpx.Client(timeout=10)

    def send(self, title: str, message: str, key: str, priority: str = "default") -> bool:
        if not self._limiter.allow(key):
            log.info("alert_rate_limited", key=key, title=title)
            return False
        headers = {"Title": title, "Priority": priority, "Tags": "kestrel"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            r = self._client.post(self._endpoint, content=message.encode(), headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            # An alerting failure must never take the trading loop down.
            log.error("alert_failed", key=key, error=str(exc))
            return False
        log.info("alert_sent", key=key, title=title)
        return True


@dataclass
class RecordingAlerter:
    """Tests and runs without ntfy configured: records instead of sending."""

    min_interval_seconds: float = 0.0
    sent: list[tuple[str, str, str, str]] = field(default_factory=list)
    _limiter: RateLimiter | None = None

    def send(self, title: str, message: str, key: str, priority: str = "default") -> bool:
        if self._limiter is None:
            self._limiter = RateLimiter(self.min_interval_seconds)
        if not self._limiter.allow(key):
            return False
        self.sent.append((title, message, key, priority))
        log.warning("alert", key=key, title=title, message=message)
        return True
