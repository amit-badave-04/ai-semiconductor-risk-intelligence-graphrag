"""``GET /api/freshness`` (public, read-rate-limited) and ``POST /api/admin/freshness/check`` (X-Admin-Token);
docs/v2/M4_PLAN.md 4.1.

Reuses ``routes._check_admin`` as the task directs. That helper answers a missing or wrong ``X-Admin-Token`` with
404 (matching every other ``/api/admin/*`` route in this service), not the 401 the plan text names — a deliberate
choice for consistency with the existing admin surface over the plan's literal wording (a real behaviour change to
``_check_admin`` is out of scope: ``serve/routes.py`` is a forbidden file for this worker).
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from . import guard
from . import routes
from .monitor import MonitorBusy, status_without_a_monitor

logger = logging.getLogger("semigraph.serve.monitor_routes")
router = APIRouter()

MSG_READ_RATE = "Too many requests from your address — please slow down."
MSG_MONITOR_OFF = "The freshness monitor is not enabled."
MSG_UNCONFIGURED = "SEC_USER_AGENT is not configured — the freshness monitor is idle."

# What GET /api/freshness reports when no monitor instance exists at all: the same shape status_payload() would
# produce, so callers never special-case a missing monitor. next_check_at (and every other timing/data field) stays
# null here unconditionally — with no FreshnessMonitor thread at all there is no next attempt to report
# (docs/v2/M4_PLAN.md 15.10: next_check_at is null only when disabled/unconfigured).
_DISABLED_PAYLOAD = {"checked_at": None, "snapshot_as_of": None, "last_error_at": None, "next_check_at": None,
                     "pending_count": 0, "pending_filings": [], "federal_register": None, "unresolved": [],
                     "duration_s": None}


def _read_gate(request: Request) -> None:
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.ip_hash(guard.client_ip(request, s.client_ip_header))):
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)


_status_without_a_monitor = status_without_a_monitor   # one definition, shared with /api/stats (monitor.py)


@router.get("/api/freshness")
async def freshness(request: Request):
    _read_gate(request)
    monitor = getattr(request.app.state, "freshness_monitor", None)
    if monitor is None:
        settings = request.app.state.settings
        status, configured = _status_without_a_monitor(settings)
        return {**_DISABLED_PAYLOAD, "configured": configured, "enabled": settings.freshness_enabled,
                "status": status}
    return monitor.status_payload()


@router.post("/api/admin/freshness/check")
async def freshness_check(request: Request):
    routes._check_admin(request)
    monitor = getattr(request.app.state, "freshness_monitor", None)
    if monitor is None:
        raise HTTPException(status_code=503, detail=MSG_MONITOR_OFF)
    if not monitor.configured:
        raise HTTPException(status_code=503, detail=MSG_UNCONFIGURED)
    try:
        return await run_in_threadpool(monitor.check_now)
    except MonitorBusy:
        raise HTTPException(status_code=409, detail="a freshness check is already running") from None
