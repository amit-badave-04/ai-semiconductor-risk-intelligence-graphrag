"""Admission control for paid answers: rate limit, question validation,
client-IP handling, Cloudflare Turnstile. Persisted policy (kill switch,
daily ceiling) lives in :mod:`semigraph.serve.store`.

The deployment is deliberately ONE machine (fly.toml), so a process-local
sliding window is the correct scope for per-IP limits; anything that must
survive a restart (daily ceiling, kill switch) is in Neo4j instead.
"""

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import UTC, date, datetime

from fastapi import HTTPException, Request

from ..uploads import WORKSPACE_ID_RE

logger = logging.getLogger("semigraph.serve.guard")

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_BUCKETS = 20_000
IP_HASH_HEX_CHARS = 16          # of the HMAC: 64 bits, the width every ledger row already has
PROCESS_PEPPER_BYTES = 32
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

    def seed(self, key: str, timestamps: Sequence[float]) -> None:
        """Boot only, before serving: put the ``time.monotonic()`` readings of earlier events into ``key``'s window, so
        a restart does not hand a client a fresh window. Readings older than the window or in the future are dropped,
        only the newest ``max_events`` are kept, and a key that already has events is left alone (nothing here replaces
        what a live request recorded). Like ``allow`` it is not thread-safe: call it before the first request."""
        if self.max_events <= 0 or self._buckets.get(key):
            return
        now = time.monotonic()
        recent = sorted(t for t in timestamps if 0 <= now - t <= self.window)
        if recent:
            self._buckets[key] = deque(recent[-self.max_events:])


class TokenBucket:
    """A refilling budget of ``rate`` operations per second (burst: one second's worth), for a gate that protects a
    resource shared by every client, such as the answer-cache read before the bot check. ``take`` is not thread-safe;
    the routes call it from the event loop only. ``rate <= 0`` disables it."""

    def __init__(self, rate: float, clock=time.monotonic):
        self.rate, self._clock = float(rate), clock
        self._tokens, self._at = self.rate, clock()

    def take(self) -> bool:
        if self.rate <= 0:
            return True
        now = self._clock()
        self._tokens = min(self.rate, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


def client_ip(request: Request, trusted_header: str = "") -> str:
    """The client address. A forwarding header is honoured ONLY when the
    deployment names it (``CLIENT_IP_HEADER=fly-client-ip`` on Fly, whose edge
    proxy sets it from the real connection); any other header is attacker
    controlled and would turn the per-address limiter into a no-op.

    ``Settings`` stores the name stripped and lower-cased (``CLIENT_IP_HEADER``); the name is stripped here too, so a
    stray space from any other caller can never make the lookup miss and key every visitor by the proxy's address."""
    name = trusted_header.strip()
    if name:
        value = request.headers.get(name)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def canonical_ip(ip: str) -> str:
    """The text that is hashed for a client address. IPv4 as it is. IPv6 as its /64 network address: one subscriber is
    handed a whole /64, so a window keyed by the full address would cost an abuser nothing to dodge. An IPv4-mapped IPv6
    address (``::ffff:a.b.c.d``) is that IPv4. A zone id (``%eth0``) belongs to the host part and is dropped with it.
    Text that is not an address comes back unchanged: hostile input is hashed like any other string, never raises."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Address(int(address) >> 64 << 64))
    return str(address)


def ip_hash(ip: str, pepper: str | bytes, version: int | None = None) -> str:
    """The key of the per-address windows and of a ledger row: 16 hex characters of HMAC-SHA-256 over
    :func:`canonical_ip`, keyed by the secret ``pepper`` (``IP_HASH_PEPPER``). Only a holder of the pepper can recompute
    it, so a row no longer names its client; the unsalted hash this replaces could be brute-forced over the whole IPv4
    space (docs/v2/M5_DECISIONS.md 1.4 item 4). An empty pepper is a ``ValueError``: there is no safe hash without one.

    ``version`` is the id of the pepper (``IP_HASH_VERSION``, stored on the row as ``ip_hash_v``). It is checked here
    and NOT mixed into the hash: the secret already carries the entropy, and a mixed-in id would let a version bump
    alone silently re-key every address (resetting every window) without a secret having changed. Rotate the pepper
    only on exposure; that resets the windows and breaks correlation with older rows, and the version tells those
    rows apart. Version 0 is never valid: it marks a row whose legacy hash was nulled."""
    if version is not None and (type(version) is not int or version < 1):
        raise ValueError("the IP hash version must be a positive integer (0 marks a nulled legacy row)")
    key = pepper.encode("utf-8") if isinstance(pepper, str) else bytes(pepper)
    if not key:
        raise ValueError("IP_HASH_PEPPER is empty: an address cannot be hashed without it")
    # surrogatepass: a lone surrogate in hostile text must hash like any other text, not raise
    message = canonical_ip(ip).encode("utf-8", "surrogatepass")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:IP_HASH_HEX_CHARS]


_process_pepper: bytes | None = None
_process_pepper_lock = threading.Lock()


def _random_process_pepper() -> bytes:
    """The pepper of a settings object that has none, outside production: random, created on first use, kept for the
    life of the process. Development and tests work without a secret; the price (windows and hashes start over at every
    restart) is logged once."""
    global _process_pepper
    with _process_pepper_lock:
        if _process_pepper is None:
            _process_pepper = secrets.token_bytes(PROCESS_PEPPER_BYTES)
            logger.info("IP_HASH_PEPPER is not set: using a random pepper for this process, so per-address windows and "
                        "address hashes start over at every restart")
        return _process_pepper


def _pepper_of(settings) -> str | bytes:
    pepper = getattr(settings, "ip_hash_pepper", "")
    if pepper or getattr(settings, "is_production", False):
        # in production an empty one is never replaced: ``ip_hash`` refuses it (the settings validator comes first)
        return pepper
    return _random_process_pepper()


def hash_request_ip(request: Request, settings) -> str:
    """The ONE way a route keys a per-address window or a ledger row: :func:`ip_hash` of the client address
    (:func:`client_ip`, which trusts a forwarding header only when ``CLIENT_IP_HEADER`` names it) under the configured
    pepper. A settings object without the pepper attributes (a test double) gets the process-random pepper."""
    return ip_hash(client_ip(request, settings.client_ip_header), _pepper_of(settings),
                   getattr(settings, "ip_hash_version", None))


def ip_hash_version_fields(settings) -> dict[str, int]:
    """``{"ip_hash_v": n}``, the keyword a ledger write hands ``store.log_query`` so the row names the pepper that made
    its ``ip_hash``. Empty for a settings object without a version (a test double): the row is written as before."""
    version = getattr(settings, "ip_hash_version", None)
    return {} if version is None else {"ip_hash_v": version}


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
