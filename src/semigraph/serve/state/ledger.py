"""Every Cypher statement of the state package (M5a I4a; docs/v2/M5A_BUILD_PLAN.md section 3).

Each function takes the state driver first, runs ONE managed transaction (``execute_write`` / ``execute_read``) whose
function carries a server-side transaction timeout (``unit_of_work(timeout=...)``), and is a plain synchronous call: the
async caller chooses the thread hop. A timeout of 0 would mean NO timeout to the server, so a non-positive one is
refused.

The two rules every statement here follows, because the properties the backends promise hold only if they do:

1. **Lock, then decide.** A statement that reads a value and then changes what depends on it first takes the write
   lock of the node that serialises the decision (``SET x._lock = true``): the day counter for a reserve, and the row
   itself before its ``status`` is checked for a settle, a renew or an expiry. A ``MATCH ... {status: 'reserved'}``
   alone is not enough. Two transactions can both match the row while it is still ``reserved``; the second then blocks
   on its ``SET`` and goes on with its stale match (the lost update ``store.reserve_daily_upload`` documents). The lock
   on a row is taken and removed again in the same statement (``SET q._lock = true REMOVE q._lock``): the lock lasts
   until the transaction ends, and no ``_lock`` property is left on a ledger row. The counters keep theirs, as
   ``SvcUploadDay`` does.
2. **One lock order**, so two writers cannot deadlock: the day counter, then the per-IP counter or the ledger row.

Columns. A ledger row is a ``SvcQuery`` (the label ``store.log_query`` writes), so every existing reader keeps working
(``store.ledger_summary``, ``paid_queries_today``): ``id, day, cached, strategy, workspace, ip_hash, ip_hash_v,
prompt_tokens, completion_tokens, cost_usd, created_at`` keep their names. New columns: ``status`` (``reserved`` |
``settled``; absent on a row written before I4, which counts as settled), ``outcome``, ``estimate_micro``,
``cost_micro`` (integer micro-dollars: sums over them are exact), ``lease_until`` and ``ts`` / ``settled_at`` (epoch
seconds, wall clock), ``machine_id``. ``SvcDayCounter {day, paid, spend_micro}`` and ``SvcIpDay {day, ip_hash, paid}``
exist for the neo4j backend only.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from neo4j import unit_of_work

from .backend import MICRO

T = TypeVar("T")

RESERVED, SETTLED = "reserved", "settled"
DENIED_DAILY_COUNT, DENIED_DAILY_SPEND = "daily_count", "daily_spend"
DENIED_IP_DAILY, DENIED_INFLIGHT = "ip_daily", "inflight"
LEASE_LIST_LIMIT = 100
SWEEP_BATCH = 100

STATE_SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT svc_query_id_unique IF NOT EXISTS FOR (q:SvcQuery) REQUIRE q.id IS UNIQUE",
    "CREATE CONSTRAINT svc_day_counter_unique IF NOT EXISTS FOR (c:SvcDayCounter) REQUIRE c.day IS UNIQUE",
    # a composite UNIQUE constraint (Community has it), not NODE KEY (Enterprise only)
    "CREATE CONSTRAINT svc_ip_day_unique IF NOT EXISTS FOR (i:SvcIpDay) REQUIRE (i.day, i.ip_hash) IS UNIQUE",
    "CREATE INDEX svc_query_status IF NOT EXISTS FOR (q:SvcQuery) ON (q.status)",
    "CREATE INDEX svc_query_day_status IF NOT EXISTS FOR (q:SvcQuery) ON (q.day, q.status)",
)

# ---- reserve (neo4j backend: the counters and the row in ONE transaction) --------------------------------------------

# The two MERGE ... SET _lock statements come first: they serialise concurrent reserves (one day counter per day), and
# nothing is read before both locks are held. In-flight is every unexpired reserved row of any day and any machine.
RESERVE_COUNTED = """\
MERGE (c:SvcDayCounter {day: $day})
  ON CREATE SET c.paid = 0, c.spend_micro = 0
SET c._lock = true
MERGE (i:SvcIpDay {day: $day, ip_hash: $ip_hash})
  ON CREATE SET i.paid = 0
SET i._lock = true
WITH c, i
OPTIONAL MATCH (r:SvcQuery {status: 'reserved'}) WHERE r.lease_until > $now
WITH c, i, count(r) AS inflight
WHERE ($max_count = 0 OR c.paid < $max_count)
  AND ($max_spend = 0 OR c.spend_micro + $estimate <= $max_spend)
  AND ($max_ip = 0 OR i.paid < $max_ip)
  AND inflight < $max_inflight
SET c.paid = c.paid + 1, c.spend_micro = c.spend_micro + $estimate, c.updated_at = $created_at,
    i.paid = i.paid + 1
CREATE (q:SvcQuery {id: $id, day: $day, ts: $now, created_at: $created_at, status: 'reserved', cached: false,
                    strategy: $strategy, workspace: $workspace, ip_hash: $ip_hash, ip_hash_v: $ip_hash_v,
                    estimate_micro: $estimate, lease_until: $lease_until, machine_id: $machine_id})
RETURN q.id AS id
"""

# Read in the same transaction, after a denial, while the locks are still held: the counters it reads are the ones the
# decision was made on.
DENIAL_READ = """\
MATCH (c:SvcDayCounter {day: $day})
OPTIONAL MATCH (i:SvcIpDay {day: $day, ip_hash: $ip_hash})
OPTIONAL MATCH (r:SvcQuery {status: 'reserved'}) WHERE r.lease_until > $now
RETURN c.paid AS paid, c.spend_micro AS spend_micro, i.paid AS ip_paid, count(r) AS inflight
"""

# ---- the durable row alone (in-process backend) ----------------------------------------------------------------------

RESERVE_ROW = """\
MERGE (q:SvcQuery {id: $id})
  ON CREATE SET q.day = $day, q.ts = $now, q.created_at = $created_at, q.status = 'reserved', q.cached = false,
                q.strategy = $strategy, q.workspace = $workspace, q.ip_hash = $ip_hash, q.ip_hash_v = $ip_hash_v,
                q.estimate_micro = $estimate, q.lease_until = $lease_until, q.machine_id = $machine_id
RETURN q.id AS id
"""

SETTLE_ROW = """\
MATCH (q:SvcQuery {id: $id})
SET q._lock = true
REMOVE q._lock
WITH q
WHERE q.status = 'reserved'
SET q.status = 'settled', q.outcome = $outcome, q.settled_at = $now, q.cost_micro = $actual,
    q.cost_usd = $cost_usd, q.prompt_tokens = $pt, q.completion_tokens = $ct
RETURN q.id AS id
"""

RENEW_ROW = """\
MATCH (q:SvcQuery {id: $id})
SET q._lock = true
REMOVE q._lock
WITH q
WHERE q.status = 'reserved'
SET q.lease_until = $lease_until
RETURN q.id AS id
"""

# ---- settle with the counters (neo4j backend) ------------------------------------------------------------------------

# The day counter is locked first (the order every writer uses), then the row, THEN the status is checked: whoever gets
# the locks second sees ``settled`` and matches nothing. The counter is adjusted by actual - estimate in integers and
# never goes below zero. An unknown cost ($cost_micro null) keeps the estimate.
SETTLE_COUNTED = """\
MATCH (q:SvcQuery {id: $id})
MERGE (c:SvcDayCounter {day: q.day})
  ON CREATE SET c.paid = 0, c.spend_micro = 0
SET c._lock = true
WITH q, c
SET q._lock = true
REMOVE q._lock
WITH q, c
WHERE q.status = 'reserved'
WITH q, c, coalesce($cost_micro, q.estimate_micro) AS actual
SET q.status = 'settled', q.outcome = $outcome, q.settled_at = $now, q.cost_micro = actual,
    q.cost_usd = toFloat(actual) / 1000000.0, q.prompt_tokens = $pt, q.completion_tokens = $ct,
    c.spend_micro = CASE WHEN c.spend_micro + actual - q.estimate_micro < 0 THEN 0
                         ELSE c.spend_micro + actual - q.estimate_micro END,
    c.updated_at = $created_at
RETURN q.id AS id
"""

EXPIRED_LEASES = """\
MATCH (q:SvcQuery {status: 'reserved'})
WHERE q.machine_id = $me AND q.lease_until < $now AND NOT q.id IN $skip
RETURN q.id AS id
ORDER BY q.lease_until
LIMIT $limit
"""

# ---- boot rebuild -----------------------------------------------------------------------------------------------

# Step 1. Rows of this machine (the process that wrote them is gone) and rows whose lease expired more than a grace ago
# (a dead machine's) are closed and charged their estimate. The row is locked and the conditions are evaluated again
# AFTER the lock: a lease another machine renewed between the match and the lock is left alone.
EXPIRE_RESERVED = """\
MATCH (q:SvcQuery {status: 'reserved'})
WHERE q.lease_until IS NULL OR q.lease_until < $now - $grace OR q.machine_id = $me
SET q._lock = true
REMOVE q._lock
WITH q
WHERE q.status = 'reserved'
  AND (q.lease_until IS NULL OR q.lease_until < $now - $grace OR q.machine_id = $me)
SET q.status = 'settled', q.outcome = 'abandoned_restart', q.settled_at = $now,
    q.cost_micro = coalesce(q.estimate_micro, 0), q.cost_usd = toFloat(coalesce(q.estimate_micro, 0)) / 1000000.0
RETURN count(q) AS n
"""

# Step 2. Today's paid asks: settled rows, rows written before I4 (no ``status``: they ARE settled, at their dollar
# cost, read here as whole micro-dollars), and the unexpired reserved ones at their estimate.
DAY_SUMS = """\
MATCH (q:SvcQuery {day: $day, cached: false})
WHERE q.status IS NULL OR q.status = 'settled' OR (q.status = 'reserved' AND q.lease_until >= $now)
WITH q, CASE WHEN q.status = 'reserved' THEN coalesce(q.estimate_micro, 0)
             ELSE coalesce(q.cost_micro, toInteger(round(coalesce(q.cost_usd, 0.0) * 1000000))) END AS micro
RETURN count(q) AS paid, coalesce(sum(micro), 0) AS spend_micro,
       sum(CASE WHEN q.status = 'reserved' AND q.machine_id <> $me THEN 1 ELSE 0 END) AS foreign
"""

# Step 3. The per-IP counts and the timestamps that reseed the paid rate-limiter windows. Only rows made under the
# CURRENT pepper: a hash made under another pepper (or nulled) can never equal what the routes compute now. A row
# written by ``store.log_query`` before I4 has no ``ts``; its ISO ``created_at`` stands in.
IP_ROWS = """\
MATCH (q:SvcQuery {day: $day, cached: false})
WHERE q.ip_hash IS NOT NULL AND q.ip_hash_v = $ip_hash_v
  AND (q.status IS NULL OR q.status = 'settled' OR (q.status = 'reserved' AND q.lease_until >= $now))
RETURN q.ip_hash AS ip, coalesce(q.ts, toFloat(datetime(q.created_at).epochSeconds)) AS ts
"""

# neo4j backend: set the counters to the sums, in the transaction that holds the day counter's lock.
LOCK_DAY = """\
MERGE (c:SvcDayCounter {day: $day})
  ON CREATE SET c.paid = 0, c.spend_micro = 0
SET c._lock = true
RETURN c.paid AS paid, c.spend_micro AS spend_micro
"""

WRITE_DAY = """\
MATCH (c:SvcDayCounter {day: $day})
SET c.paid = $paid, c.spend_micro = $spend_micro, c.updated_at = $created_at
"""

WRITE_IP_DAYS = """\
UNWIND $rows AS row
MERGE (i:SvcIpDay {day: $day, ip_hash: row.ip})
SET i._lock = true, i.paid = row.n
"""

ZERO_OTHER_IP_DAYS = """\
MATCH (i:SvcIpDay {day: $day})
WHERE NOT i.ip_hash IN $ips
SET i._lock = true, i.paid = 0
"""

# ---- snapshot ---------------------------------------------------------------------------------------------------

SNAPSHOT_COUNTERS = """\
OPTIONAL MATCH (c:SvcDayCounter {day: $day})
WITH c
OPTIONAL MATCH (i:SvcIpDay {day: $day})
RETURN c.paid AS paid, c.spend_micro AS spend_micro, max(i.paid) AS per_ip_max
"""

SNAPSHOT_INFLIGHT = """\
MATCH (r:SvcQuery {status: 'reserved'}) WHERE r.lease_until > $now
RETURN count(r) AS inflight
"""

SNAPSHOT_LEASES = """\
MATCH (r:SvcQuery {status: 'reserved'}) WHERE r.lease_until > $now
RETURN r.id AS id, r.day AS day, r.strategy AS strategy, r.workspace AS workspace,
       r.estimate_micro AS estimate_micro, r.machine_id AS machine_id, r.lease_until AS lease_until
ORDER BY r.lease_until
LIMIT $limit
"""


@dataclass(frozen=True)
class Caps:
    """The four admission limits of a reserve. 0 means off, EXCEPT ``max_inflight`` where 0 means nothing is allowed."""

    max_count: int
    max_spend_micro: int
    max_per_ip: int
    max_inflight: int


@dataclass(frozen=True)
class ReserveOutcome:
    granted: bool
    reason: str | None          # one of the ``DENIED_*`` names when not granted


@dataclass(frozen=True)
class DaySums:
    paid: int
    spend_micro: int
    foreign_leases: int                         # unexpired reserved rows of OTHER machines
    per_ip: Mapping[str, int]
    ip_events: tuple[tuple[str, float], ...]    # (ip_hash, wall-clock time)


@dataclass(frozen=True)
class SyncResult:
    sums: DaySums
    synced: bool                                # the counters were set to the sums
    delta: Mapping[str, int] | None             # not synced: sums minus the stored counters


def iso(epoch_s: float) -> str:
    """ISO 8601 text of an epoch second, as ``store.log_query`` writes ``created_at``."""
    return datetime.fromtimestamp(epoch_s, UTC).isoformat(timespec="seconds")


def _bound(timeout_s: float) -> float:
    if not timeout_s > 0:
        raise ValueError(f"timeout_s must be positive (the server reads 0 as NO timeout), got {timeout_s!r}")
    return float(timeout_s)


def _rows(tx: Any, cypher: str, params: Mapping[str, Any]) -> list[dict]:
    return [dict(record) for record in tx.run(cypher, dict(params))]


def write_tx(driver: Any, timeout_s: float, work: Callable[[Any], T]) -> T:
    """``work(tx)`` as a managed write transaction with a server-side timeout. ``work`` may run again after a transient
    failure, so it must not keep state between attempts."""
    bounded = unit_of_work(timeout=_bound(timeout_s))(work)
    with driver.session() as session:
        return session.execute_write(bounded)


def read_tx(driver: Any, timeout_s: float, work: Callable[[Any], T]) -> T:
    bounded = unit_of_work(timeout=_bound(timeout_s))(work)
    with driver.session() as session:
        return session.execute_read(bounded)


def run_write(driver: Any, cypher: str, *, timeout_s: float, **params: Any) -> list[dict]:
    return write_tx(driver, timeout_s, lambda tx: _rows(tx, cypher, params))


def run_read(driver: Any, cypher: str, *, timeout_s: float, **params: Any) -> list[dict]:
    return read_tx(driver, timeout_s, lambda tx: _rows(tx, cypher, params))


def ensure_state_schema(driver: Any) -> None:
    """Idempotent: the constraints and indexes the state package needs (``IF NOT EXISTS``)."""
    with driver.session() as session:
        for statement in STATE_SCHEMA_STATEMENTS:
            list(session.run(statement))


# ---- reserve ---------------------------------------------------------------------------------------------------------

def _denial_reason(read: Mapping[str, Any], caps: Caps, estimate_micro: int) -> str | None:
    """The first cap, in the order the in-process backend checks them, that the counters in ``read`` exceed."""
    paid, spend = read.get("paid") or 0, read.get("spend_micro") or 0
    if caps.max_count and paid >= caps.max_count:
        return DENIED_DAILY_COUNT
    if caps.max_spend_micro and spend + estimate_micro > caps.max_spend_micro:
        return DENIED_DAILY_SPEND
    if caps.max_per_ip and (read.get("ip_paid") or 0) >= caps.max_per_ip:
        return DENIED_IP_DAILY
    if (read.get("inflight") or 0) >= caps.max_inflight:
        return DENIED_INFLIGHT
    return None


def reserve_counted(driver: Any, *, lease_id: str, day: str, ip_hash: str, ip_hash_v: int | None, strategy: str,
                    workspace: bool, estimate_micro: int, now_wall: float, lease_until: float, machine_id: str,
                    caps: Caps, timeout_s: float) -> ReserveOutcome:
    """Check all four caps against the counters and, if they all pass, increment them and create the ``reserved`` row,
    all in ONE transaction. Zero rows back means a cap said no; the cap is then named by a read in the same
    transaction, while the day counter is still locked. Returns ``ReserveOutcome(False, None)`` only if no cap
    explains the denial (it cannot happen under the lock; the caller treats it as unavailable)."""
    params = {"id": lease_id, "day": day, "ip_hash": ip_hash, "ip_hash_v": ip_hash_v, "strategy": strategy,
              "workspace": workspace, "estimate": int(estimate_micro), "now": now_wall, "lease_until": lease_until,
              "machine_id": machine_id, "created_at": iso(now_wall), "max_count": caps.max_count,
              "max_spend": caps.max_spend_micro, "max_ip": caps.max_per_ip, "max_inflight": caps.max_inflight}

    def work(tx: Any) -> ReserveOutcome:
        if _rows(tx, RESERVE_COUNTED, params):
            return ReserveOutcome(True, None)
        read = _rows(tx, DENIAL_READ, params)
        return ReserveOutcome(False, _denial_reason(read[0] if read else {}, caps, int(estimate_micro)))

    return write_tx(driver, timeout_s, work)


def reserve_row(driver: Any, *, lease_id: str, day: str, ip_hash: str, ip_hash_v: int | None, strategy: str,
                workspace: bool, estimate_micro: int, now_wall: float, lease_until: float, machine_id: str,
                timeout_s: float) -> None:
    """The durable ``reserved`` row of the in-process backend (whose counters live in memory). Idempotent on the id: a
    transaction the driver runs again after a transient failure cannot create two rows."""
    run_write(driver, RESERVE_ROW, timeout_s=timeout_s, id=lease_id, day=day, ip_hash=ip_hash, ip_hash_v=ip_hash_v,
              strategy=strategy, workspace=workspace, estimate=int(estimate_micro), now=now_wall,
              created_at=iso(now_wall), lease_until=lease_until, machine_id=machine_id)


# ---- settle, renew, sweep --------------------------------------------------------------------------------------------

def _tokens(usage: Mapping[str, Any] | None) -> tuple[Any, Any]:
    usage = usage or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def settle_counted(driver: Any, lease_id: str, *, outcome: str, usage: Mapping[str, Any] | None,
                   cost_micro: int | None, now_wall: float, timeout_s: float) -> bool:
    """Settle a reserved row and adjust its day counter by ``cost - estimate`` in one transaction. True iff this call
    settled it; a second call (or one that lost a race to a sweep) matches nothing and returns False."""
    pt, ct = _tokens(usage)
    rows = run_write(driver, SETTLE_COUNTED, timeout_s=timeout_s, id=lease_id, outcome=outcome,
                     cost_micro=None if cost_micro is None else int(cost_micro), pt=pt, ct=ct, now=now_wall,
                     created_at=iso(now_wall))
    return bool(rows)


def settle_row(driver: Any, lease_id: str, *, outcome: str, usage: Mapping[str, Any] | None, actual_micro: int,
               now_wall: float, timeout_s: float) -> bool:
    """Settle a reserved row without touching any counter (the in-process backend's). Same one-winner rule."""
    pt, ct = _tokens(usage)
    rows = run_write(driver, SETTLE_ROW, timeout_s=timeout_s, id=lease_id, outcome=outcome, actual=int(actual_micro),
                     cost_usd=round(actual_micro / MICRO, 6), pt=pt, ct=ct, now=now_wall)
    return bool(rows)


def renew_row(driver: Any, lease_id: str, *, lease_until: float, timeout_s: float) -> bool:
    """Write ``lease_until`` on a row that is still reserved; False when it is not (settled, swept, unknown)."""
    return bool(run_write(driver, RENEW_ROW, timeout_s=timeout_s, id=lease_id, lease_until=lease_until))


def expired_lease_ids(driver: Any, *, machine_id: str, now_wall: float, skip: Iterable[str], limit: int,
                      timeout_s: float) -> list[str]:
    """Ids of this machine's reserved rows whose lease ran out, other than ``skip`` (the registered, renewed ones)."""
    rows = run_read(driver, EXPIRED_LEASES, timeout_s=timeout_s, me=machine_id, now=now_wall, skip=list(skip),
                    limit=limit)
    return [row["id"] for row in rows]


# ---- boot rebuild -----------------------------------------------------------------------------------------------

def expire_reserved(driver: Any, *, now_wall: float, grace_s: float, machine_id: str, timeout_s: float) -> int:
    """Boot step 1: close the reserved rows of this machine and the long-expired ones as ``abandoned_restart``, each
    charged its estimate. Returns how many were closed. No day filter: a row of an earlier day is closed too."""
    rows = run_write(driver, EXPIRE_RESERVED, timeout_s=timeout_s, now=now_wall, grace=grace_s, me=machine_id)
    return int(rows[0]["n"]) if rows else 0


def _day_sums_in(tx: Any, *, day: str, now_wall: float, machine_id: str, ip_hash_v: int | None) -> DaySums:
    base = {"day": day, "now": now_wall, "me": machine_id}
    head = (_rows(tx, DAY_SUMS, base) or [{}])[0]
    per_ip: dict[str, int] = {}
    events: list[tuple[str, float]] = []
    if ip_hash_v is not None:
        for row in _rows(tx, IP_ROWS, {**base, "ip_hash_v": ip_hash_v}):
            per_ip[row["ip"]] = per_ip.get(row["ip"], 0) + 1
            if row.get("ts") is not None:
                events.append((row["ip"], float(row["ts"])))
    return DaySums(paid=int(head.get("paid") or 0), spend_micro=int(head.get("spend_micro") or 0),
                   foreign_leases=int(head.get("foreign") or 0), per_ip=per_ip, ip_events=tuple(sorted(events)))


def day_sums(driver: Any, *, day: str, now_wall: float, machine_id: str, ip_hash_v: int | None,
             timeout_s: float) -> DaySums:
    """Boot steps 2 and 3, read only (the in-process backend): today's paid count and spend, the live leases of other
    machines, and the per-IP counts and window timestamps of the rows made under the current pepper."""
    return read_tx(driver, timeout_s, lambda tx: _day_sums_in(tx, day=day, now_wall=now_wall, machine_id=machine_id,
                                                              ip_hash_v=ip_hash_v))


def sync_day_counters(driver: Any, *, day: str, now_wall: float, machine_id: str, ip_hash_v: int | None,
                      timeout_s: float) -> SyncResult:
    """Boot steps 2-4 for the neo4j backend, in ONE transaction under the day counter's lock: compute the sums and, only
    when no unexpired lease of another machine exists, set the counters (the day's and every per-IP one) to them.
    Otherwise the counters are left alone (the other machine is live and has charged its own leases) and the difference
    comes back as ``delta`` for the log."""
    def work(tx: Any) -> SyncResult:
        stored = (_rows(tx, LOCK_DAY, {"day": day}) or [{}])[0]
        sums = _day_sums_in(tx, day=day, now_wall=now_wall, machine_id=machine_id, ip_hash_v=ip_hash_v)
        if sums.foreign_leases:
            delta = {"paid": sums.paid - int(stored.get("paid") or 0),
                     "spend_micro": sums.spend_micro - int(stored.get("spend_micro") or 0)}
            return SyncResult(sums, False, delta)
        _rows(tx, WRITE_DAY, {"day": day, "paid": sums.paid, "spend_micro": sums.spend_micro,
                              "created_at": iso(now_wall)})
        if ip_hash_v is not None:
            rows = [{"ip": ip, "n": n} for ip, n in sorted(sums.per_ip.items())]
            _rows(tx, WRITE_IP_DAYS, {"day": day, "rows": rows})
            _rows(tx, ZERO_OTHER_IP_DAYS, {"day": day, "ips": sorted(sums.per_ip)})
        return SyncResult(sums, True, None)

    return write_tx(driver, timeout_s, work)


def snapshot_counted(driver: Any, *, day: str, now_wall: float, timeout_s: float) -> dict:
    """The neo4j backend's view for ``snapshot()``: today's counters, the unexpired in-flight count and up to
    ``LEASE_LIST_LIMIT`` of the live leases (no IP hash is returned)."""
    def work(tx: Any) -> dict:
        head = (_rows(tx, SNAPSHOT_COUNTERS, {"day": day}) or [{}])[0]
        inflight = (_rows(tx, SNAPSHOT_INFLIGHT, {"now": now_wall}) or [{}])[0].get("inflight") or 0
        leases = _rows(tx, SNAPSHOT_LEASES, {"now": now_wall, "limit": LEASE_LIST_LIMIT})
        return {"paid": int(head.get("paid") or 0), "spend_micro": int(head.get("spend_micro") or 0),
                "per_ip_max": int(head.get("per_ip_max") or 0), "inflight": int(inflight), "leases": leases}

    return read_tx(driver, timeout_s, work)
