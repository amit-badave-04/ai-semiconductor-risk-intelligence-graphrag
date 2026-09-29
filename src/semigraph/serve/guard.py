"""Admission control for paid answers: rate limit, question validation,
client-IP handling, Cloudflare Turnstile. Persisted policy (kill switch,
daily ceiling) lives in :mod:`semigraph.serve.store`.

The deployment is deliberately ONE machine (fly.toml), so a process-local
sliding window is the correct scope for per-IP limits; anything that must
survive a restart (daily ceiling, kill switch) is in Neo4j instead.
"""

import hashlib
import logging
import re
import time
from collections import defaultdict, deque
from datetime import UTC, date, datetime

from fastapi import HTTPException, Request

from ..uploads import WORKSPACE_ID_RE

logger = logging.getLogger("semigraph.serve.guard")

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_BUCKETS = 20_000
STRATEGIES = ("hybrid", "vector")
AGENT_STRATEGY = "agent"
WORKSPACE_STRATEGIES = ("hybrid",)
_AS_OF_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_AS_OF_INSTANT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
AS_OF_MIN_YEAR, AS_OF_MAX_YEAR = 2000, 2100
MSG_AS_OF = "as_of must be a date (YYYY-MM-DD) or an instant with a UTC offset (YYYY-MM-DDTHH:MM:SSZ)"


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


def validate_strategy(strategy: str, *, agent_enabled: bool = False, workspace: bool = False) -> str:
    """``agent`` is an opt-in strategy: neither accepted nor advertised while the deployment has it off.

    With an upload workspace only ``hybrid`` answers (docs/v2/M4_PLAN.md D7): the agent planner must never be driven by
    user-uploaded text, so ``agent`` + a workspace is refused here, before Turnstile and the slot."""
    s = (strategy or "hybrid").lower()
    if workspace:
        if s == AGENT_STRATEGY and agent_enabled:
            raise HTTPException(status_code=400, detail="strategy=agent is not available with a workspace")
        if s not in WORKSPACE_STRATEGIES:
            raise HTTPException(status_code=400, detail=f"with a workspace, strategy must be one of {WORKSPACE_STRATEGIES}")
        return s
    allowed = STRATEGIES + (AGENT_STRATEGY,) if agent_enabled else STRATEGIES
    if s not in allowed:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {allowed}")
    return s


def validate_workspace_id(value: str) -> str:
    if not isinstance(value, str) or not WORKSPACE_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="malformed workspace id")
    return value


def validate_as_of(value: str | None) -> str | None:
    """None, a calendar date ``YYYY-MM-DD`` (returned as is), or an instant with an explicit UTC offset
    (``YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM)``, up to 9 fraction digits as Neo4j prints them) returned normalized to
    UTC ISO 8601. A workspace lives 24 h, so versions uploaded the same day are told apart only by an instant. Years
    outside 2000-2100 are refused (they would overflow the end-of-day cutoff and mean nothing here)."""
    if value is None:
        return None
    if not isinstance(value, str) or not (_AS_OF_RE.match(value) or _AS_OF_INSTANT_RE.match(value)):
        raise HTTPException(status_code=400, detail=MSG_AS_OF)
    try:
        if _AS_OF_RE.match(value):
            day = date.fromisoformat(value)
            canonical, year = value, day.year
        else:
            instant = datetime.fromisoformat(value).astimezone(UTC)
            canonical, year = instant.isoformat(), instant.year
    except ValueError:
        raise HTTPException(status_code=400, detail=MSG_AS_OF) from None
    if not AS_OF_MIN_YEAR <= year <= AS_OF_MAX_YEAR:
        raise HTTPException(status_code=400, detail=MSG_AS_OF)
    return canonical


async def verify_turnstile(token: str | None, ip: str, secret: str, is_production: bool,
                           required: bool = False) -> bool:
    """True when the Turnstile token is valid.

    Not configured: allowed (loudly logged in production) unless ``required``
    is set, in which case live questions fail CLOSED — the posture of the
    reference deployment once the widget exists (docs/RUNBOOK.md)."""
    if not secret:
        if required:
            logger.error("TURNSTILE_REQUIRED is set but TURNSTILE_SECRET_KEY is empty — failing closed")
            return False
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
