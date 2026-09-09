"""Admission control for paid answers: rate limit, question validation,
client-IP handling, Cloudflare Turnstile. Persisted policy (kill switch,
daily ceiling) lives in :mod:`semigraph.serve.store`.

The deployment is deliberately ONE machine (fly.toml), so a process-local
sliding window is the correct scope for per-IP limits; anything that must
survive a restart (daily ceiling, kill switch) is in Neo4j instead.
"""

import hashlib
import logging
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

logger = logging.getLogger("semigraph.serve.guard")

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_BUCKETS = 20_000
STRATEGIES = ("hybrid", "vector")


class RateLimiter:
    """Sliding-window counter per key; ``max_events <= 0`` disables."""

    def __init__(self, max_events: int, window_seconds: int):
        self.max_events, self.window = max_events, window_seconds
        self._buckets: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.max_events <= 0:
            return True
        now = time.monotonic()
        bucket = self._buckets[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_events:
            return False
        bucket.append(now)
        if len(self._buckets) > MAX_BUCKETS:  # bound memory over long uptime
            for k in [k for k, b in list(self._buckets.items()) if not b]:
                self._buckets.pop(k, None)
        return True


def client_ip(request: Request, trusted_header: str = "") -> str:
    """The client address. A forwarding header is honoured ONLY when the
    deployment names it (``CLIENT_IP_HEADER=fly-client-ip`` on Fly, whose edge
    proxy sets it from the real connection); any other header is attacker
    controlled and would turn the per-address limiter into a no-op."""
    if trusted_header:
        value = request.headers.get(trusted_header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_hash(ip: str) -> str:
    """Stable, non-reversible key for logs and the ledger (no raw IPs stored)."""
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def validate_question(question: str, max_chars: int) -> str:
    q = " ".join((question or "").split())
    if len(q) < 8:
        raise HTTPException(status_code=400, detail="Ask a full question (at least 8 characters).")
    if len(q) > max_chars:
        raise HTTPException(status_code=400, detail=f"Questions are limited to {max_chars} characters.")
    return q


def validate_strategy(strategy: str) -> str:
    s = (strategy or "hybrid").lower()
    if s not in STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {STRATEGIES}")
    return s


async def verify_turnstile(token: str | None, ip: str, secret: str, is_production: bool) -> bool:
    """True when the Turnstile token is valid. Not configured -> allowed, but
    loudly logged in production (the daily ceiling + rate limit remain the
    hard cost controls; see docs/RUNBOOK.md to enable the bot gate)."""
    if not secret:
        if is_production:
            logger.warning("turnstile not configured in production — relying on rate limit + daily ceiling")
        return True
    if not token:
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(TURNSTILE_VERIFY_URL,
                                     data={"secret": secret, "response": token, "remoteip": ip})
        return resp.status_code == 200 and resp.json().get("success") is True
    except Exception as e:  # noqa: BLE001 — a verification outage must not 500
        logger.warning("turnstile verification failed: %s", e)
        return False
