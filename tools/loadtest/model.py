"""The pre-registered traffic model, as code (docs/v2/M5_PLAN.md section 6 = PLAN.md:33). Pure stdlib; no locust.

Everything the report and the generator must agree on lives here, so a number cannot drift between the two:

* **Population.** 1,000 virtual users; per iteration the shell, ``/api/stats``, ``/api/examples``, ``/api/freshness``, then
  exactly one ask (45 % cached example, 40 % live from the 300-question pool, 10 % live unique suffix, 5 % live agent),
  plus an evidence lookup 20 % of the time and a dossier or risk-changes read 5 % of the time. A separate 5-VU upload
  population runs one upload each per 10 minutes and watches the job to ``ready``.
* **Pacing (the one place a generator can quietly miss the model).** "Think time U(120, 300) s, 4.76 iterations/s" holds only
  when 120-300 s is the whole CYCLE, start to start. Locust's ``between(120, 300)`` waits after the iteration has finished, so
  a 15 s live stream stretches the cycle to ~220 s and the offered live rate to ~2.50/s, a hair above the VOID floor of
  0.95 x 2.6 = 2.47/s. That would turn a server slowdown into a VOID instead of a FAIL, the split council 5 wants kept apart.
  So a VU draws its cycle length when the iteration STARTS and sleeps only the remainder (:func:`wait_after`), and its first
  delay is drawn from the renewal process's residual life (:func:`first_delay`) so the offered rate is stationary from the
  moment a VU is spawned, not only after a first full cycle.
* **Phases.** ramp 10 min, steady 20 min, spike (+500 VUs) 3 min, soak 60 min, fault 10 min (103 minutes).
* **Per-VU client address.** ``X-Test-Client-IP`` is a unique IPv4 per VU (:func:`vu_ip`): worker digit, a nibble of the
  run id and the VU's sequence number, so two VUs never share a per-address window and two runs rarely share one.
* **The target must be a staging host.** :func:`check_target_host` refuses the live app.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import random
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

# ---- the pre-registered numbers (PLAN.md:33; M5_PLAN.md section 6), except MOCK_CPU_VOID_PCT (a harness rule) ---------

VUS = 1000
SPIKE_EXTRA_VUS = 500
UPLOAD_VUS = 5
UPLOAD_PERIOD_S = 600.0
THINK_MIN_S = 120.0
THINK_MAX_S = 300.0

# (phase, seconds). The spike adds SPIKE_EXTRA_VUS on top of VUS for its three minutes.
PHASES: tuple[tuple[str, float], ...] = (
    ("ramp", 600.0), ("steady", 1200.0), ("spike", 180.0), ("soak", 3600.0), ("fault", 600.0))
MEASURED_PHASES = ("steady", "soak")          # the pass criteria are judged on steady + soak (M5_PLAN section 6)

ASK_MIX: tuple[tuple[str, float], ...] = (
    ("cached", 0.45), ("live_pool", 0.40), ("live_unique", 0.10), ("live_agent", 0.05))
LIVE_CLASSES = frozenset({"live_pool", "live_unique", "live_agent"})
P_EVIDENCE = 0.20
P_DOSSIER_OR_CHANGES = 0.05

TARGET_LIVE_RATE = 2.6                         # live asks/s at 1,000 VUs (derived below; the plan's rounded figure)
VOID_RATE_FRACTION = 0.95                      # the offered live rate must reach this share of TARGET_LIVE_RATE
GENERATOR_CPU_VOID_PCT = 70.0
HARNESS_RULE_LABEL = "harness rule (not pre-registered)"   # how a VOID clause that is not in the plan or the councils is named
MOCK_CPU_VOID_PCT = 50.0                       # harness rule (not pre-registered): from the harness plan, in none of M5_PLAN / M5_DECISIONS / council 5
CPU_PER_ASK_VOID_FRACTION = 0.8                # process CPU-s per live ask vs the Fly per-embed cost (council 5)
TTFE_P95_LIMIT_S = 1.5
ERROR_RATE_LIMIT = 0.005
SERVER_CPU_LIMIT_PCT = 70.0
MIN_CONCURRENT_LIVE = 40

STRATEGY_FOR = {"cached": "hybrid", "live_pool": "hybrid", "live_unique": "hybrid", "live_agent": "agent"}


def mean_think_s(lo: float = THINK_MIN_S, hi: float = THINK_MAX_S) -> float:
    return (lo + hi) / 2.0


def live_share() -> float:
    """The share of asks that are live (paid): 0.55."""
    return sum(p for name, p in ASK_MIX if name in LIVE_CLASSES)


def derived_rates(vus: int = VUS, lo: float = THINK_MIN_S, hi: float = THINK_MAX_S) -> dict[str, float]:
    """The load the model offers at ``vus`` users, assuming every cycle is exactly one iteration (start to start)."""
    iterations = vus / mean_think_s(lo, hi)
    return {"iterations_per_s": iterations, "live_per_s": iterations * live_share(),
            "cached_per_s": iterations * (1.0 - live_share())}


def void_floor_rate(vus: int = VUS, *, scale_to_vus: bool = False) -> float:
    """The offered live rate under which a run is VOID: 95 % of the plan's 2.6/s. ``scale_to_vus`` (the local smoke only)
    scales the figure by ``vus / VUS``."""
    base = TARGET_LIVE_RATE * (vus / VUS if scale_to_vus else 1.0)
    return VOID_RATE_FRACTION * base


# ---- phases and the VU target ---------------------------------------------------------------------------------------

def phase_schedule() -> list[tuple[str, float, float]]:
    """``[(name, t0, t1)]`` in seconds from the start of the run."""
    out, t = [], 0.0
    for name, length in PHASES:
        out.append((name, t, t + length))
        t += length
    return out


def run_length_s() -> float:
    return sum(length for _, length in PHASES)


def phase_at(t: float) -> str | None:
    """The phase at ``t`` seconds into the run, or None once the run is over."""
    for name, t0, t1 in phase_schedule():
        if t0 <= t < t1:
            return name
    return None


def target_users(t: float, vus: int = VUS) -> tuple[int, float] | None:
    """``(main_users, spawn_rate_per_s)`` at ``t`` seconds into the run, or None when the run is over (the 5 upload VUs are
    added by the caller). The ramp is linear to ``vus``; the spike adds :data:`SPIKE_EXTRA_VUS` at once."""
    name = phase_at(t)
    if name is None:
        return None
    if name == "ramp":
        users = max(1, round(vus * t / PHASES[0][1]))
        return min(users, vus), max(vus / PHASES[0][1], 1.0)
    if name == "spike":
        return vus + SPIKE_EXTRA_VUS, 50.0
    return vus, 50.0


def expected_paid_asks(vus: int = VUS) -> float:
    """Live (paid) asks one whole run offers: VU-seconds under the schedule / mean cycle x the live share."""
    vu_seconds = 0.0
    for name, length in PHASES:
        if name == "ramp":
            vu_seconds += length * vus / 2.0
        elif name == "spike":
            vu_seconds += length * (vus + SPIKE_EXTRA_VUS)
        else:
            vu_seconds += length * vus
    return vu_seconds / mean_think_s() * live_share()


# ---- pacing ---------------------------------------------------------------------------------------------------------

def draw_cycle(rng: random.Random, lo: float = THINK_MIN_S, hi: float = THINK_MAX_S) -> float:
    """One cycle length, U(lo, hi) seconds, START to START."""
    return rng.uniform(lo, hi)


def wait_after(elapsed: float, target: float) -> float:
    """The sleep that completes a cycle of ``target`` seconds after an iteration that took ``elapsed``; never negative. An
    iteration that overran its cycle starts the next one at once (the overrun is logged by the caller)."""
    return max(0.0, target - elapsed)


def first_delay(rng: random.Random, lo: float = THINK_MIN_S, hi: float = THINK_MAX_S) -> float:
    """The delay before a new VU's first iteration so that its arrivals are STATIONARY from the moment it is spawned: the
    residual life of a renewal process with U(lo, hi) cycles, i.e. a cycle picked with probability proportional to its length
    (rejection sampling), then a uniform point inside it. Mean (var + mean^2) / (2 mean) = 111 s for 120-300 s."""
    while True:
        cycle = rng.uniform(lo, hi)
        if rng.random() * hi <= cycle:
            return rng.uniform(0.0, cycle)


def choose_ask_class(rng: random.Random, mix: tuple[tuple[str, float], ...] = ASK_MIX) -> str:
    """One ask class by the pre-registered shares (45 / 40 / 10 / 5)."""
    x, acc = rng.random(), 0.0
    for name, share in mix:
        acc += share
        if x < acc:
            return name
    return mix[-1][0]


# ---- the client address -----------------------------------------------------------------------------------------------

UPLOAD_SEQ_BASE = 60000          # upload VUs use sequence numbers from here, main VUs below it (16-bit space)


def _run_nibble(run_id: str) -> int:
    return hashlib.sha256(run_id.encode("utf-8")).digest()[0] & 0x0F


def vu_ip(run_id: str, worker: int, seq: int, *, upload: bool = False) -> str:
    """A unique 10.x.y.z address for one VU: 4 bits of worker digit, 4 bits of the run id's hash, 16 bits of VU sequence. Two
    VUs of one run never share an address (distinct worker or sequence); a main VU and an upload VU never do either."""
    if not 0 <= worker <= 9:
        raise ValueError(f"worker must be a digit 0-9, got {worker!r}")
    if upload:
        seq += UPLOAD_SEQ_BASE
    if not 0 <= seq < 65536 or (not upload and seq >= UPLOAD_SEQ_BASE):
        raise ValueError(f"VU sequence {seq!r} is out of range")
    value = (worker << 20) | (_run_nibble(run_id) << 16) | seq
    return str(ipaddress.IPv4Address((10 << 24) | value))


# ---- the target host --------------------------------------------------------------------------------------------------

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_FLY_SUFFIXES = (".fly.dev", ".internal")
LIVE_APP_NAMES = frozenset({"semigraph", "semigraph-neo4j"})


def check_target_host(host: str | None) -> str:
    """The hostname if ``host`` (a URL or bare host) is a place a load test may point at: loopback, or a Fly name
    (``<app>.fly.dev``, ``<app>.internal``, ``<region>.<app>.internal``) whose app name carries the token ``stg``
    (``semigraph-stg``). Anything else, the live app first of all, is a ValueError: the generator refuses to start."""
    if not host or not str(host).strip():
        raise ValueError("no target host: set --host to the staging API")
    text = str(host).strip()
    name = (urlsplit(text if "://" in text else f"//{text}").hostname or "").lower().rstrip(".")
    if name in _LOOPBACK:
        return name
    for suffix in _FLY_SUFFIXES:
        if name.endswith(suffix):
            app = name[: -len(suffix)].split(".")[-1]
            if app in LIVE_APP_NAMES:
                raise ValueError(f"{name!r} is the LIVE app: the load generator refuses to target it")
            if "stg" in app.split("-"):
                return name
            break
    raise ValueError(f"{name!r} is not a staging host (need loopback or an app whose name has the token 'stg')")


# ---- the staging server's own limits, which can make a correct 429 look like a failure ---------------------------------

_PER_IP_RE = re.compile(r"^\s*(\d+)\s+per\s+(\d+)\s*(?:min|m)\b", re.I)


def limit_warnings(stats: Mapping, *, vus: int = VUS) -> list[str]:
    """Settings that would make the server's CORRECT refusals count as errors in this run, read from ``/api/stats``. Advisory:
    the caller logs them (a staging configuration is for the main session to change, never for the generator)."""
    warnings: list[str] = []
    limits = stats.get("limits") or {}
    total = expected_paid_asks(vus)
    per_vu = run_length_s() / mean_think_s() * live_share()      # a VU present for the whole run
    cap = limits.get("max_queries_per_day")
    if isinstance(cap, int) and 0 < cap < total * 1.1:
        warnings.append(f"max_queries_per_day={cap} is below the ~{math.ceil(total * 1.1)} paid asks one run offers (0 = off)")
    spend = limits.get("max_spend_usd_per_day")
    if isinstance(spend, (int, float)) and spend > 0:
        warnings.append(f"max_spend_usd_per_day={spend} is on: the estimate-based daily cap (0 = off) can refuse asks")
    per_ip_day = limits.get("per_ip_per_day")
    need = math.ceil(per_vu + 4 * math.sqrt(per_vu))
    if isinstance(per_ip_day, int) and 0 < per_ip_day < need:
        warnings.append(f"per_ip_per_day={per_ip_day}: a VU makes ~{per_vu:.0f} paid asks per run (binomial), a cap below "
                        f"~{need} refuses some VUs (0 = off)")
    match = _PER_IP_RE.match(str(limits.get("per_ip") or ""))
    if match:
        allowed, minutes = int(match.group(1)), int(match.group(2))
        worst = math.ceil(minutes * 60 / THINK_MIN_S)
        if allowed < worst:
            warnings.append(f"per_ip window {allowed} per {minutes} min: back-to-back minimum-length cycles make up to "
                            f"{worst} paid asks in that window")
    if stats.get("agent_enabled") is False:
        warnings.append("agent_enabled=false: the 5 % live-agent asks will all be refused with 400")
    if stats.get("uploads_enabled") is False:
        warnings.append("uploads_enabled=false: the upload population cannot create a workspace")
    if stats.get("paused") is True:
        warnings.append("the kill switch is not off: every paid ask will be refused with 503")
    return warnings
