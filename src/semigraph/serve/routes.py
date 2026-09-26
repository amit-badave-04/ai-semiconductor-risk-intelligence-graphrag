"""HTTP surface of the service. Paid work (``POST /api/ask``) passes every
gate in order — free-tier window, cache, kill switch, daily ceiling, Turnstile,
paid per-address window, concurrency slot — before a single LLM token is bought.
Nothing is written to the database before the first (free-tier) gate."""

import json
import logging
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from ..artifacts import load_examples
from ..graph.client import run_cypher
from ..retrieval.answerer import answer_stream
from ..retrieval.ids import CHUNK_ID_RE
from . import guard, store

logger = logging.getLogger("semigraph.serve")
router = APIRouter()

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
       "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://challenges.cloudflare.com; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Strict-Transport-Security": "max-age=31536000; includeSubDomains"}
MSG_READ_RATE = "Too many requests from your address — please slow down."
MSG_PAUSED = "Live questions are paused right now — the example questions still work."
MSG_BUDGET = "The daily budget of live questions is used up — try an example, or come back tomorrow."
MSG_BOT = "Bot check failed — reload the page and try again."
MSG_RATE = "Too many questions from your address — please wait a few minutes."
MSG_BUSY = "The service is busy answering other questions — try again in a moment."


# The citation drawer: the excerpt, where it comes from, and its FRESHNESS — a paragraph a later
# amendment restated is ``corrected`` and names the amending filing (AMD's 10-K/A, Item 7).
EVIDENCE_QUERY = """MATCH (e:EvidenceSpan {chunk_id: $id})
OPTIONAL MATCH (e)-[:FROM_SECTION]->(s:FilingSection)<-[:HAS_SECTION]-(f:Filing)<-[:FILED]-(c:Company)
OPTIONAL MATCH (e)-[:MENTIONS]->(m:Company)
OPTIONAL MATCH (amender:Filing)-[:AMENDS]->(f)
WITH e, s, f, c, collect(DISTINCT m.name) AS mentions, collect(DISTINCT amender.accession_no) AS amenders
RETURN e.chunk_id AS chunk_id, e.text AS text, e.source_url AS source_url,
       s.section_key AS section_key, s.title AS section_title, f.accession_no AS accession_no,
       f.form AS form, toString(f.filing_date) AS filing_date, c.name AS filer,
       e.status AS status, e.is_current AS is_current, e.retrievable AS retrievable,
       toString(e.valid_to) AS valid_to, f.superseded_by AS superseded_by,
       CASE WHEN e.status = 'corrected' THEN head(amenders) END AS corrected_by, mentions"""


class AskRequest(BaseModel):
    question: str = Field(..., max_length=4000)
    strategy: str = "hybrid"
    turnstile_token: str | None = None


class PolicyRequest(BaseModel):
    kill_switch: bool


def _read_gate(request: Request) -> None:
    """Per-address window for the free read endpoints (they hit the database)."""
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.ip_hash(guard.client_ip(request, s.client_ip_header))):
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)


def _sse(event: dict) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(event, default=str), event=event["event"], sep="\n")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    page = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TURNSTILE_SITE_KEY__", request.app.state.settings.turnstile_site_key)
    return HTMLResponse(page, headers=SECURITY_HEADERS)


@router.get("/healthz")
async def healthz(request: Request):
    st = request.app.state
    try:
        await run_in_threadpool(run_cypher, st.driver, "RETURN 1 AS ok")
        return {"status": "ok", "db": True, "embedder": st.embedder.name}
    except Exception as e:  # noqa: BLE001 — any failure means not ready
        logger.warning("healthz: neo4j unreachable: %s", e)
        return JSONResponse(status_code=503, content={"status": "degraded", "db": False})


@router.get("/api/examples")
async def examples(request: Request):
    _read_gate(request)
    ex = load_examples()
    return {"source": ex["source"],
            "examples": [{"id": e["id"], "type": e["type"], "question": e["question"]}
                         for e in ex["examples"]]}


async def _ledger_cached(st) -> dict:
    """Ledger aggregation cached for a few seconds: the page loads it on every
    visit and the all-time part is a label scan."""
    now = time.monotonic()
    cached = getattr(st, "stats_cache", None)
    if cached and now - cached[0] < st.settings.stats_cache_seconds:
        return cached[1]
    ledger = await run_in_threadpool(store.ledger_summary, st.driver)
    st.stats_cache = (now, ledger)
    return ledger


@router.get("/api/stats")
async def stats(request: Request):
    _read_gate(request)
    st, s = request.app.state, request.app.state.settings
    ledger = await _ledger_cached(st)
    paused = await run_in_threadpool(store.kill_switch_on, st.driver, s.kill_switch)
    return {"graph": st.graph_stats, "snapshot": getattr(st, "snapshot", None), "ledger": ledger, "paused": paused,
            "limits": {"max_queries_per_day": s.max_queries_per_day,
                       "per_ip": f"{s.rate_limit_questions} per {s.rate_limit_window_seconds // 60} min",
                       "max_question_chars": s.max_question_chars},
            "models": {"llm": s.answer_model, "escalation": s.escalation_model or None, "embedder": st.embedder.name}}


@router.get("/api/evidence/{chunk_id}")
async def evidence(chunk_id: str, request: Request):
    _read_gate(request)
    if not CHUNK_ID_RE.match(chunk_id):
        raise HTTPException(status_code=400, detail="malformed chunk id")
    rows = await run_in_threadpool(run_cypher, request.app.state.driver, EVIDENCE_QUERY, id=chunk_id)
    if not rows:
        raise HTTPException(status_code=404, detail="no evidence span with that id")
    return rows[0]


@router.post("/api/ask")
async def ask(body: AskRequest, request: Request):
    st, s = request.app.state, request.app.state.settings
    question = guard.validate_question(body.question, s.max_question_chars)
    strategy = guard.validate_strategy(body.strategy)
    ip = guard.client_ip(request, s.client_ip_header)
    iph = guard.ip_hash(ip)
    snapshot_id = getattr(st, "snapshot_id", "")

    # Free tier (cache hits) has its own, wider window — and it is the first gate,
    # so an unauthenticated client cannot write a ledger row without passing it.
    if not st.free_rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)
    cached = await run_in_threadpool(store.get_answer, st.driver, store.cache_key(question, strategy, snapshot_id),
                                     s.answer_cache_ttl_hours)
    if cached:
        await run_in_threadpool(store.log_query, st.driver, ip_hash=iph, strategy=strategy, cached=True)
        event = {"event": "done", "cached": True, **cached}
        return EventSourceResponse(iter([_sse(event)]), sep="\n")

    if await run_in_threadpool(store.kill_switch_on, st.driver, s.kill_switch):
        raise HTTPException(status_code=503, detail=MSG_PAUSED)
    if s.max_queries_per_day and await run_in_threadpool(store.paid_queries_today, st.driver) >= s.max_queries_per_day:
        raise HTTPException(status_code=429, detail=MSG_BUDGET)
    if not await guard.verify_turnstile(body.turnstile_token, ip, s.turnstile_secret_key,
                                        s.is_production, required=s.turnstile_required):
        raise HTTPException(status_code=403, detail=MSG_BOT)
    if not st.rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)
    return EventSourceResponse(_paid_stream(st, question, strategy, iph, snapshot_id), ping=15, sep="\n")


def _paid_stream(st, question: str, strategy: str, iph: str, snapshot_id: str = ""):
    """Sync generator (runs in the threadpool): slot -> retrieval -> LLM deltas -> done.

    The concurrency slot is taken INSIDE the generator so it is released by the
    same ``finally`` on every path (a slot taken in the handler would leak if the
    client vanished before the stream started). Spend is written to the ledger
    on ``done`` AND on ``error`` — a mid-stream failure still cost tokens.
    """
    s = st.settings
    if not st.answer_slots.acquire(blocking=False):
        yield _sse({"event": "error", "detail": MSG_BUSY})
        return
    logged = False
    try:
        for ev in answer_stream(question, st.driver, st.embedder, strategy=strategy,
                                timeout=s.llm_request_timeout_s, max_tokens=s.llm_answer_max_tokens,
                                escalation_model=s.escalation_model or None):
            if ev["event"] in ("done", "error"):
                store.log_query(st.driver, ip_hash=iph, strategy=strategy, cached=False,
                                usage=ev.get("usage"), cost_usd=ev.get("cost_usd"))
                logged = True
            if ev["event"] == "done":
                if ev["answer"].strip() and ev["finish_reason"] != "length":
                    store.put_answer(st.driver, question=question, strategy=strategy, answer=ev["answer"],
                                     citations=ev["citations"], hallucinated=ev["hallucinated"],
                                     usage=ev["usage"], cost_usd=ev["cost_usd"], snapshot_id=snapshot_id)
                logger.info("answered strategy=%s citations=%d hallucinated=%d cost=%s routed=%s escalated=%s by=%s",
                            strategy, len(ev["citations"]), len(ev["hallucinated"]), ev["cost_usd"],
                            ev.get("routed"), ev.get("escalated"), ev.get("answered_by"))
            elif ev["event"] == "error":
                logger.warning("answer failed mid-stream: %s (cost=%s)", ev["detail"], ev.get("cost_usd"))
                ev = {"event": "error", "detail": "The answer could not be completed — please try again."}
            yield _sse(ev)
    except Exception as e:  # noqa: BLE001 — report, never hang the stream
        logger.exception("answer failed")
        try:
            store.log_query(st.driver, ip_hash=iph, strategy=strategy, cached=False)
            logged = True
        except Exception:  # noqa: BLE001
            logger.exception("ledger write failed after an answer failure")
        yield _sse({"event": "error", "detail": f"The answer could not be completed ({type(e).__name__})."})
    finally:
        if not logged:
            # The client went away before the terminal event (a buffered draft widens that window to the whole
            # generation). The query still counts against the daily ceiling; its cost is unknown here.
            try:
                store.log_query(st.driver, ip_hash=iph, strategy=strategy, cached=False)
            except Exception:  # noqa: BLE001
                logger.exception("ledger write failed for an abandoned answer")
        st.answer_slots.release()


def _check_admin(request: Request) -> None:
    token = request.app.state.settings.admin_token
    given = request.headers.get("x-admin-token", "")
    if not token or not secrets.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8")):
        raise HTTPException(status_code=404)


@router.get("/api/admin/policy")
async def admin_policy(request: Request):
    _check_admin(request)
    st = request.app.state
    return {"kill_switch": await run_in_threadpool(store.get_policy, st.driver, "kill_switch") or "off",
            "ledger": await run_in_threadpool(store.ledger_summary, st.driver)}


@router.post("/api/admin/policy")
async def admin_set_policy(body: PolicyRequest, request: Request):
    _check_admin(request)
    value = "on" if body.kill_switch else "off"
    await run_in_threadpool(store.set_policy, request.app.state.driver, "kill_switch", value)
    logger.warning("kill switch set to %s by admin", value)
    return {"kill_switch": value}
