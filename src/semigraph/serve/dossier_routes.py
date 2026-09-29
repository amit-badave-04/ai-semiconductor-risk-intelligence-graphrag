"""``GET /api/company/{ticker}/dossier`` and ``GET /api/company/{ticker}/risk-changes`` (read-only, no LLM, no
embedder); docs/v2/M4_PLAN.md 4.5. Read-rate limited, 60 s in-process cache per (ticker, endpoint, limit) kept on
``app.state`` (never a module global — a module global would leak between test fixtures and between requests to
different apps in the same process)."""

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..retrieval import dossier
from ..universe import FILERS
from . import guard

logger = logging.getLogger("semigraph.serve.dossier_routes")
router = APIRouter()

CACHE_SECONDS = 60
RISK_CHANGES_LIMIT_CAP = dossier.RISK_CHANGES_LIMIT_CAP
CACHE_CONTROL = {"Cache-Control": f"public, max-age={CACHE_SECONDS}"}
MSG_READ_RATE = "Too many requests from your address — please slow down."
MSG_UNKNOWN_TICKER = "unknown ticker"


def _read_gate(request: Request) -> None:
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.ip_hash(guard.client_ip(request, s.client_ip_header))):
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)


def _cached(request: Request, key: tuple, compute) -> dict | None:
    st = request.app.state
    cache = getattr(st, "dossier_cache", None)
    if cache is None:
        cache = st.dossier_cache = {}
    now = time.monotonic()
    hit = cache.get(key)
    if hit is not None and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = compute()
    cache[key] = (now, value)
    return value


@router.get("/api/company/{ticker}/dossier")
async def company_dossier(ticker: str, request: Request):
    _read_gate(request)
    ticker = ticker.upper()
    if ticker not in FILERS:
        raise HTTPException(status_code=404, detail=MSG_UNKNOWN_TICKER)
    st = request.app.state
    data_as_of = (getattr(st, "snapshot", None) or {}).get("as_of")

    def compute():
        return dossier.get_dossier(st.driver, ticker, data_as_of=data_as_of)

    result = await run_in_threadpool(_cached, request, ("dossier", ticker), compute)
    if result is None:
        raise HTTPException(status_code=404, detail=MSG_UNKNOWN_TICKER)
    return JSONResponse(result, headers=CACHE_CONTROL)


@router.get("/api/company/{ticker}/risk-changes")
async def company_risk_changes(ticker: str, request: Request, limit: int = 20):
    _read_gate(request)
    ticker = ticker.upper()
    if ticker not in FILERS:
        raise HTTPException(status_code=404, detail=MSG_UNKNOWN_TICKER)
    capped = min(max(1, limit), RISK_CHANGES_LIMIT_CAP)
    st = request.app.state

    def compute():
        return dossier.get_risk_changes(st.driver, ticker, limit=capped)

    result = await run_in_threadpool(_cached, request, ("risk-changes", ticker, capped), compute)
    if result is None:
        raise HTTPException(status_code=404, detail=MSG_UNKNOWN_TICKER)
    return JSONResponse(result, headers=CACHE_CONTROL)
