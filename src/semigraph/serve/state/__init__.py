"""Paid-ask admission state behind one swappable interface (M5a I4a; docs/v2/M5A_BUILD_PLAN.md sections 2 and 3,
docs/v2/M5_DECISIONS.md 2.2).

``make_backend(settings, drivers)`` returns a :class:`StateBackend`: ``inprocess`` (the live default: the counters are
memory under one lock, the durable ledger rows are written to Neo4j) or ``neo4j`` (the counters live in Neo4j too, in
the same transaction as the row: the rollback, and the only choice with more than one machine on one database).

Modules: ``backend`` (the contract, the shared core, ``make_backend``), ``inprocess``, ``neo4j``, ``ledger`` (all the
Cypher), ``settle_queue`` (the retry queue of settles that failed), ``maintenance`` (the thread that refreshes the kill
level, renews leases, sweeps and retries the queued settles).

Settings read from the plain object handed to ``make_backend`` / ``MaintenanceThread`` (no ``config.py`` import here,
so the package works with a ``SimpleNamespace`` in tests):

- ``state_backend``: ``"inprocess"`` (default) or ``"neo4j"``.
- ``max_queries_per_day``: paid asks per UTC day; 0 = off.
- ``max_spend_usd_per_day``: estimate-based daily spend cap in dollars (whole micro-dollars inside); 0 = off.
- ``paid_per_ip_per_day``: paid asks per address hash per UTC day; 0 = off.
- ``max_concurrent_answers``: leases in flight; 0 = nothing is allowed (the one cap where 0 is not "off").
- ``kill_switch``: env override: when true the kill level is ``on`` whatever the database says.
- ``kill_switch_refresh_s``: how often the maintenance thread re-reads the kill level (10).
- ``kill_switch_stale_s``: a cached kill level older than this reads as ``on`` (30).
- ``state_op_timeout_s``: the budget of every state operation (1.0): the server-side transaction timeout of each
  statement, and what the state driver's per-attempt timeouts are derived from. See ``backend`` for what is measured.
- ``state_connection_acquisition_s``: a CEILING on one connection attempt of the state driver (0.5). With the 1 s
  budget an attempt gets 0.47 s, because a managed transaction makes two. Read by
  ``graph.client.make_state_driver``, not by the backends.
- ``lease_ttl_s``: how long a lease lives without a renewal (60); it only has to cover a stream that never started.
- ``lease_renew_s``: renewal and sweep period (15); a boot also expires leases older than twice this.
- ``machine_id``: this process's identity on every row it writes (``FLY_MACHINE_ID``, else the hostname).
- ``ip_hash_version``: optional; the pepper id written on each row as ``ip_hash_v`` and used to pick the rows that
  reseed the per-IP counters and windows at boot.

Money is an integer number of micro-dollars everywhere (:func:`usd_to_micro`). Every backend method is synchronous: the
async caller runs it as ``await anyio.to_thread.run_sync(fn, limiter=limiters.state)`` with no timeout of its own, and
follows it with ``anyio.lowlevel.checkpoint()``. The bound lives in the work itself (see ``backend`` and ``ledger``).

A failed settle never makes the caller wait: ``reconcile`` returns at once and the row write is retried by the
maintenance thread (``drain_settles`` on every tick, ``flush_settles`` at shutdown; see ``settle_queue``).
"""

from .backend import (
    Denied,
    Lease,
    RebuildReport,
    StateBackend,
    StateConfig,
    StateDrivers,
    StateUnavailable,
    make_backend,
    micro_to_usd,
    usd_to_micro,
)

__all__ = [
    "Denied",
    "Lease",
    "RebuildReport",
    "StateBackend",
    "StateConfig",
    "StateDrivers",
    "StateUnavailable",
    "make_backend",
    "micro_to_usd",
    "usd_to_micro",
]
