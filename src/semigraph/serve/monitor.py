"""Freshness monitor (M4, docs/v2/M4_PLAN.md 4.1): detects and SURFACES what is new at the source; it never ingests (D1).

STEP 0 STUB: the lifecycle hooks below are final (``start_if_enabled`` / ``stop`` are wired into ``serve.main.lifespan``); the
rest is implemented by Worker B against the plan's contract:

- ``check_once(driver, settings, *, fetch=None, today=None) -> dict``: EDGAR submissions per filer vs ``Filing`` nodes in the
  SERVED graph, Federal Register live count vs ``ExportControl`` nodes; the JSON shape of ``GET /api/freshness``.
- ``FreshnessMonitor(driver, settings)``: a daemon thread; first pass ``freshness_boot_delay_s`` after boot and only when the last
  check is older than ``freshness_poll_hours``; then every ``freshness_poll_hours``; one machine at a time through the
  ``SvcLease`` lease; results persisted in ``SvcFreshness``; ``stop(timeout=5)`` returns promptly; ``summary()`` is the
  ``{"status", "checked_at", "pending_count"}`` block of ``/api/stats``.
- Imports only ``semigraph.ingestion.{edgar, freshness, federal_register}`` (never pandas; tests/test_serve_monitor_isolation.py).
- A missing ``SEC_USER_AGENT`` never fails the boot: the monitor reports ``configured: false`` and idles.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("semigraph.serve.monitor")


def start_if_enabled(app) -> None:
    """Create and start the monitor when ``FRESHNESS_ENABLED``; ``app.state.freshness_monitor`` is None otherwise."""
    app.state.freshness_monitor = None
    if not app.state.settings.freshness_enabled:
        return
    raise NotImplementedError("freshness monitor: M4 Worker B (docs/v2/M4_PLAN.md 4.1)")


def stop(app) -> None:
    """Stop the monitor thread (bounded); a no-op when none runs. Called before the database driver closes."""
    monitor = getattr(app.state, "freshness_monitor", None)
    if monitor is not None:
        monitor.stop()
