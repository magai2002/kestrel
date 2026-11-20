"""A small per-client token bucket for the public API.

Behind the host Nginx every request arrives from 127.0.0.1, so the real
client address comes from ``X-Forwarded-For`` — but **only** when the
direct peer is a trusted proxy. Otherwise anyone could set that header
and get a fresh bucket per request.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Loopback by default. In Docker the published port is forwarded from the
# bridge gateway (172.x), so compose adds that range via
# KESTREL_TRUSTED_PROXIES; the port itself is bound to 127.0.0.1, so only
# the host's own Nginx can reach it.
TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("::1/128"),
]


def set_trusted_proxies(cidrs: str) -> None:
    TRUSTED_PROXIES[:] = [ipaddress.ip_network(c.strip()) for c in cidrs.split(",") if c.strip()]


def _trusted(peer: str) -> bool:
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


@dataclass
class TokenBucket:
    rate_per_second: float
    burst: int
    clock: Callable[[], float] = time.monotonic
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, last)

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self._state.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate_per_second)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        if len(self._state) > 10_000:  # bound memory under a scan
            self._state.clear()
        return True


def client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _trusted(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()  # the address Nginx itself saw
    return peer


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, bucket: TokenBucket, prefix: str = "/api") -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.bucket = bucket
        self.prefix = prefix

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith(self.prefix) and not self.bucket.allow(client_key(request)):
            return JSONResponse(
                {"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "5"}
            )
        return await call_next(request)
