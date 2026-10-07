"""HTTP surface of the service. Paid work (``POST /api/ask``) passes every gate in order before a single LLM token is
bought (M5a I4, docs/v2/M5_DECISIONS.md 2.2, docs/v2/M5A_BUILD_PLAN.md step 0-I4). Every call to the state backend that
touches the store is a worker-thread hop under ``limiters.state`` (``stream_runtime.state_call``) that waits at most
``state_op_timeout_s`` for a free slot; the kill level is a memory read and is taken directly. A state failure (or no
free slot) refuses, it never lets an ask through. The order, and what each gate answers:

1. validate the question, strategy, ``as_of`` and workspace id (400) and hash the address (``guard.hash_request_ip``);
2. DRAINING: a workspace ask is always paid, so while the process drains (``serve.drain``) it is refused here, before
   any window, with 503 ``MSG_DRAINING``. A public ask is refused after the cache step below, if it misses (and, while
   draining, the cache is read BEFORE the free window, so a refused miss consumes no window either);
3. the free window (429 ``MSG_RATE``): the first gate that counts, so an unauthenticated client cannot write a ledger
   row without passing it;
4. a workspace ask: its token (404 for a bad id or token alike) is read through the state driver under the cache read
   budget; a public ask: a token of the cache read budget (429 ``MSG_READ_RATE`` when the process-wide bucket is empty),
   then ``state.cache_get``. A hit is logged as a cached row (under the same state bound) and replayed. The database
   being unreachable or slow is 503 ``MSG_STATE_UNAVAILABLE`` in both (``MSG_PAUSED`` is the kill switch's alone): a
   cached answer is never served on a guess, and a failed read never falls through to a paid call. A workspace ask
   never touches the answer cache;
5. the kill level (``state.kill_level``): ``on`` (also unread, stale or the env override) 503 ``MSG_PAUSED``,
   ``retrieval_only`` 503 ``MSG_RETRIEVAL_ONLY``;
6. Turnstile (403 ``MSG_BOT``), then the paid per-address window (429 ``MSG_RATE``);
7. the ask is counted on the drain (``DRAIN.try_enter``: 503 ``MSG_DRAINING``) and ``state.reserve`` takes a lease with
   the ask type's estimate: both daily caps (429 ``MSG_BUDGET``), the per-address daily cap (429 ``MSG_IP_BUDGET``), the
   in-flight cap (429 ``MSG_BUSY``, a pre-stream refusal with no event stream), the kill level again (503
   ``MSG_PAUSED``) and an unreachable state (503 ``MSG_STATE_UNAVAILABLE``). The lease, and the drain count, belong to
   the :class:`PaidStream` from then on;
8. the event stream.

A lease granted and then lost before the stream could take it (an exception, a cancelled request) is settled here as
``abandoned``. Nothing is written to the database before the first (free-tier) gate.

An ask over an upload workspace (M4, docs/v2/M4_PLAN.md 4.4) is ``hybrid`` only and never touches the answer cache; its token
is checked right after the free-tier window (404 for a bad id or token alike), and nothing is written before that check."""

import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from ..artifacts import load_examples
from ..graph.client import run_cypher
from ..retrieval.answerer_async import aanswer_stream
from ..retrieval.ids import CHUNK_ID_RE, classify_id, metric_id_of, rule_id_of  # noqa: F401 - CHUNK_ID_RE: re-exported (tests/test_ids.py)
from ..retrieval.workspace_async import astream_workspace_answer
from ..uploads import WORKSPACE_TOKEN_MAX_CHARS
from . import drain, guard, store
from .state import Denied, Lease, StateUnavailable, micro_to_usd
from .state.backend import KILL_LEVELS, KILL_OFF, KILL_ON, KILL_RETRIEVAL_ONLY
from .stream_runtime import (  # noqa: F401 - MSG_BUSY: re-exported (the message of the in-flight refusal)
    MSG_BUSY, NoStateSlot, PaidResponse, PaidStream, admin_call, select_twin, settle_call, slot_call, sse_event,
    state_call)

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
# The state store cannot be reached or is too slow: the example questions are served from the same store, so they are
# not promised here (MSG_PAUSED is the kill switch's message and keeps that promise).
MSG_STATE_UNAVAILABLE = "Live questions are temporarily unavailable — please try again in a few minutes."
MSG_BUDGET = "The daily budget of live questions is used up — try an example, or come back tomorrow."
MSG_IP_BUDGET = ("You have used today's live questions for your address — the example questions still work, "
                 "or come back tomorrow.")
MSG_RETRIEVAL_ONLY = "Live questions are limited to cached answers right now — the example questions still work."
MSG_DRAINING = "The service is restarting — please try again in a minute."
MSG_BOT = "Bot check failed — reload the page and try again."
MSG_RATE = "Too many questions from your address — please wait a few minutes."
MSG_UPLOADS_OFF = "Uploaded documents are not available right now."
MSG_WORKSPACE_NOT_FOUND = "workspace not found"    # an unknown workspace and a wrong token must look the same
MSG_NOT_PUBLIC_EVIDENCE = "not a public evidence id"
DRAINING_HEADERS = {"Retry-After": "30"}
# How long ``/api/stats`` waits for a state slot for the spend figure. The figure is decoration: a busy pool shows None
# (as an unreachable store does) instead of holding the page's poll for the whole ``state_op_timeout_s``.
STATS_SLOT_WAIT_S = 0.25


# The citation drawer: the excerpt, where it comes from, and its FRESHNESS — a paragraph a later
# amendment restated is ``corrected`` and names the amending filing (AMD's 10-K/A, Item 7). When the chunk lies inside a
# risk item (M1b) the item's headline comes along (empty until the loader has produced RiskItem nodes).
EVIDENCE_QUERY = """MATCH (e:EvidenceSpan {chunk_id: $id})
OPTIONAL MATCH (e)-[:FROM_SECTION]->(s:FilingSection)<-[:HAS_SECTION]-(f:Filing)<-[:FILED]-(c:Company)
OPTIONAL MATCH (e)-[:MENTIONS]->(m:Company)
OPTIONAL MATCH (amender:Filing)-[:AMENDS]->(f)
WITH e, s, f, c, collect(DISTINCT m.name) AS mentions, collect(DISTINCT amender.accession_no) AS amenders
OPTIONAL MATCH (ri:RiskItem {filer_cik: c.cik, accession_no: f.accession_no}) WHERE $id IN ri.chunk_ids
WITH e, s, f, c, mentions, amenders, ri ORDER BY ri.seq
WITH e, s, f, c, mentions, amenders, collect(ri.headline) AS item_headlines
RETURN e.chunk_id AS chunk_id, e.text AS text, e.source_url AS source_url,
       s.section_key AS section_key, s.title AS section_title, f.accession_no AS accession_no,
       f.form AS form, toString(f.filing_date) AS filing_date, c.name AS filer,
       e.status AS status, e.is_current AS is_current, e.retrievable AS retrievable,
       toString(e.valid_to) AS valid_to, f.superseded_by AS superseded_by,
       CASE WHEN e.status = 'corrected' THEN head(amenders) END AS corrected_by, mentions, item_headlines"""

# A reported financial fact (``xbrl:<cik>:<metric>:<period_end>``): the Metric node, and the filing that first disclosed
# it (the accession rides on the REPORTS_METRIC edge; an older filing may not be in the graph, so the join is optional).
XBRL_EVIDENCE_QUERY = """MATCH (m:Metric {metric_id: $id})
OPTIONAL MATCH (c:Company)-[rel:REPORTS_METRIC]->(m)
OPTIONAL MATCH (f:Filing {accession_no: rel.accession_no})
RETURN m.metric_id AS metric_id, m.metric AS metric, m.concept AS concept, m.value AS value, m.unit AS unit,
       toString(m.period_start) AS period_start, toString(m.period_end) AS period_end,
       c.name AS company, c.cik AS cik, rel.accession_no AS accession_no, f.form AS form,
       toString(f.filing_date) AS filing_date, f.url AS source_url
LIMIT 1"""

# A Federal Register rule (``fr:<document_number>``): an EXTERNAL event that a keyword heuristic links to companies.
FR_EVIDENCE_QUERY = """MATCH (x:ExportControl {rule_id: $id})
RETURN x.rule_id AS document_number, x.title AS title, toString(x.date) AS publication_date, x.url AS url,
       x.kind AS kind, x.topics AS topics, x.relevant AS relevant, x.abstract AS abstract"""

MAX_EVIDENCE_ID_CHARS = 120
# id form -> (query, the query parameter derived from the id, static fields added to every answer of that form)
_EVIDENCE = {
    "chunk": (EVIDENCE_QUERY, lambda evidence_id: evidence_id, {}),
    "xbrl": (XBRL_EVIDENCE_QUERY, metric_id_of, {}),
    "fr": (FR_EVIDENCE_QUERY, rule_id_of,
           {"source": "federal_register", "external": True,
            "note": "A Federal Register rule linked to companies by keyword match; not a company disclosure."}),
}


class AskRequest(BaseModel):
    question: str = Field(..., max_length=4000)
    strategy: str = "hybrid"
    turnstile_token: str | None = None
    workspace_id: str | None = Field(None, max_length=64)   # M4: ask over an upload workspace (X-Workspace-Token header)
    as_of: str | None = Field(None, max_length=32)          # M4: YYYY-MM-DD, workspace asks only (the guard says why not)


class PolicyRequest(BaseModel):
    """The kill level: a level name, or a bool as before (``true`` is ``on``, ``false`` is ``off``)."""

    kill_switch: Literal["on", "retrieval_only", "off"] | bool

    @property
    def level(self) -> str:
        if isinstance(self.kill_switch, bool):
            return KILL_ON if self.kill_switch else KILL_OFF
        return self.kill_switch


def _read_gate(request: Request) -> None:
    """Per-address window for the free read endpoints (they hit the database)."""
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.hash_request_ip(request, s)):
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    page = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TURNSTILE_SITE_KEY__", request.app.state.settings.turnstile_site_key)
    return HTMLResponse(page, headers=SECURITY_HEADERS)


@router.get("/healthz")
async def healthz(request: Request):
    st = request.app.state
    try:
        # The probe's own one-thread pool: a database that hangs cannot take the threads the answer streams wait for.
        await anyio.to_thread.run_sync(partial(run_cypher, st.driver, "RETURN 1 AS ok"), limiter=st.limiters.health)
        return {"status": "ok", "db": True, "embedder": st.embedder.name}
    except Exception as e:  # noqa: BLE001 — any failure means not ready
        logger.warning("healthz: neo4j unreachable: %s", e)
        return JSONResponse(status_code=503, content={"status": "degraded", "db": False})


@router.get("/api/examples")
async def examples(request: Request):
    """The saved example questions that are actually cached: the service lists only the ones ``bootstrap`` seeded (an
    example refused for missing or failed checks, another snapshot or another prompt template is not listed: a click on it
    would be a paid live call under a label that says "instant, cached"). Without bootstrap state every packaged example is
    listed."""
    _read_gate(request)
    ex = load_examples()
    accepted = getattr(request.app.state, "example_ids", None)
    return {"source": ex["source"],
            "examples": [{"id": e["id"], "type": e["type"], "question": e["question"]}
                         for e in ex["examples"] if accepted is None or e["id"] in accepted]}


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


async def _kill_level(st) -> str:
    """The kill level (``off``, ``retrieval_only`` or ``on``) from the backend's cache, which the maintenance thread
    refreshes. Anything that goes wrong reads ``on``: a gate that cannot tell is closed. It is a memory read (the
    StateBackend protocol requires it), so it is taken here, on the loop: no thread hop, no state slot to wait for, and
    nothing the slots held by a stuck database can delay. (``async`` for the callers that await it.)"""
    try:
        level = st.state.kill_level()
    except Exception as e:  # noqa: BLE001
        logger.warning("kill level unavailable (%s): treating it as on", type(e).__name__)
        return KILL_ON
    return level if level in KILL_LEVELS else KILL_ON


async def _spend_today_usd(st) -> float | None:
    """Today's spend as the daily cap counts it (settled asks at their cost, running ones at their estimate), from the
    backend's snapshot (cached like the ledger: the neo4j backend reads its counters from the database); None when the
    backend cannot say right now."""
    now = time.monotonic()
    cached = getattr(st, "spend_cache", None)
    if cached and now - cached[0] < st.settings.stats_cache_seconds:
        return cached[1]
    wait_s = min(STATS_SLOT_WAIT_S, getattr(st.settings, "state_op_timeout_s", STATS_SLOT_WAIT_S))
    try:
        spend = micro_to_usd((await slot_call(st.limiters.state, wait_s, st.state.snapshot))["spend_micro"])
    except Exception as e:  # noqa: BLE001
        logger.warning("spend snapshot unavailable (%s)", type(e).__name__)
        return None
    st.spend_cache = (now, spend)
    return spend


@router.get("/api/stats")
async def stats(request: Request):
    _read_gate(request)
    st, s = request.app.state, request.app.state.settings
    ledger = await _ledger_cached(st)
    paused = await _kill_level(st) != KILL_OFF
    return {"graph": st.graph_stats, "snapshot": getattr(st, "snapshot", None), "ledger": ledger, "paused": paused,
            "spend_today_usd": await _spend_today_usd(st),
            "limits": {"max_queries_per_day": s.max_queries_per_day,
                       "max_spend_usd_per_day": s.max_spend_usd_per_day,
                       "per_ip_per_day": s.paid_per_ip_per_day,
                       "per_ip": f"{s.rate_limit_questions} per {s.rate_limit_window_seconds // 60} min",
                       "max_question_chars": s.max_question_chars},
            "models": {"llm": s.answer_model, "escalation": s.escalation_model or None, "embedder": st.embedder.name},
            "agent_enabled": s.agent_enabled,
            # True only while a sample of agent questions is really traced: the page shows its privacy line on this flag.
            "tracing": bool(s.agent_enabled and getattr(getattr(st, "tracer", None), "enabled", False)),
            "uploads_enabled": uploads_available(st),
            "freshness": _freshness_summary(st)}


def uploads_available(st) -> bool:
    """Uploads answer only when the deployment enables them AND the upload service started (``uploads.jobs`` sets
    ``uploads_ready``; False when the embedder cannot count tokens) AND, in production, the Turnstile secret exists (the
    upload routes fail closed without it). The page, ``/api/stats`` and every workspace route use this one predicate, so
    they can never disagree."""
    s = st.settings
    bot_gate_ok = bool(getattr(s, "turnstile_secret_key", "")) or not getattr(s, "is_production", False)
    return bool(s.uploads_enabled and getattr(st, "uploads_ready", False) and bot_gate_ok)


def _freshness_summary(st) -> dict:
    """The freshness monitor's last result for the page header, or the ``disabled``/``unconfigured``/``never`` shape when
    no monitor runs (M4, docs/v2/M4_PLAN.md 4.1 and 15.10): always the same three keys, never None."""
    from .monitor import summary_without_a_monitor

    monitor = getattr(st, "freshness_monitor", None)
    return monitor.summary() if monitor is not None else summary_without_a_monitor(st.settings)


@router.get("/api/evidence/{evidence_id}")
async def evidence(evidence_id: str, request: Request):
    """Resolve one citation id: a filing chunk, a reported XBRL fact (``xbrl:...``) or a Federal Register rule
    (``fr:...``). The answer's ``type`` says which; a malformed id is 400, an unknown one 404.

    An uploaded-document id (``doc:...``) is 404 here without a query: the workspace is not part of the citation, so this
    public route cannot resolve one without revealing that it exists. The workspace's own, token-gated route resolves it."""
    _read_gate(request)
    kind = classify_id(evidence_id) if len(evidence_id) <= MAX_EVIDENCE_ID_CHARS else None
    if kind is None:
        raise HTTPException(status_code=400, detail="malformed evidence id")
    if kind not in _EVIDENCE:
        raise HTTPException(status_code=404, detail=MSG_NOT_PUBLIC_EVIDENCE)
    query, to_param, static = _EVIDENCE[kind]
    rows = await run_in_threadpool(run_cypher, request.app.state.driver, query, id=to_param(evidence_id))
    if not rows:
        raise HTTPException(status_code=404, detail="no evidence with that id")
    return {"type": kind, **rows[0], **static}


def authenticate_workspace(driver, workspace_id: str, token: str) -> bool:
    """True when ``token`` opens ``workspace_id`` (constant-time; an unknown workspace is False, like a wrong token). The
    upload package is imported only when a workspace is actually used."""
    from ..uploads.repo import authenticate
    return authenticate(driver, workspace_id, token)


def _workspace_token_ok(driver, workspace_id: str, token: str) -> bool:
    if not token or len(token) > WORKSPACE_TOKEN_MAX_CHARS:
        return False
    return authenticate_workspace(driver, workspace_id, token)


@router.post("/api/ask")
async def ask(body: AskRequest, request: Request):
    st, s = request.app.state, request.app.state.settings
    in_workspace = body.workspace_id is not None
    question = guard.validate_question(body.question, s.max_question_chars)
    strategy = guard.validate_strategy(body.strategy, agent_enabled=s.agent_enabled, workspace=in_workspace)
    as_of = guard.validate_as_of(body.as_of)
    workspace = None
    if in_workspace:
        workspace = {"workspace_id": guard.validate_workspace_id(body.workspace_id), "as_of": as_of}
        if not uploads_available(st):
            raise HTTPException(status_code=503, detail=MSG_UPLOADS_OFF)
    elif as_of is not None:
        raise HTTPException(status_code=400, detail="as_of is available only with a workspace")
    ip = guard.client_ip(request, s.client_ip_header)
    iph = guard.hash_request_ip(request, s)
    snapshot_id = getattr(st, "snapshot_id", "")

    if workspace is not None:
        # Always paid, never cached: refused before it takes a window while draining.
        _refuse_while_draining()
        _take_free_window(st, iph)
        await _check_workspace_access(st, request, workspace["workspace_id"])
    else:
        cached = await _answer_from_cache(st, s, iph, strategy, store.cache_key(question, strategy, snapshot_id))
        if cached is not None:
            return cached

    level = await _kill_level(st)
    if level == KILL_RETRIEVAL_ONLY:
        raise HTTPException(status_code=503, detail=MSG_RETRIEVAL_ONLY)
    if level != KILL_OFF:
        raise HTTPException(status_code=503, detail=MSG_PAUSED)
    if not await guard.verify_turnstile(body.turnstile_token, ip, s.turnstile_secret_key,
                                        s.is_production, required=s.turnstile_required):
        raise HTTPException(status_code=403, detail=MSG_BOT)
    if not st.rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)
    # resolved before the reserve: an import error costs nothing
    twin = _stream_fn(strategy, workspace is not None)
    lease = await _admit(st, iph, strategy, workspace is not None, st.estimates["workspace" if workspace else strategy])
    stream = PaidStream(st, question, strategy, iph, snapshot_id, workspace, twin=twin, lease=lease)
    try:
        return PaidResponse(stream, send_timeout=s.send_timeout_s)
    except BaseException:
        await stream.finalize()
        raise


# ---- the gates of an ask


def _refuse_while_draining() -> None:
    if drain.DRAIN.draining:
        raise HTTPException(status_code=503, detail=MSG_DRAINING, headers=DRAINING_HEADERS)


def _take_free_window(st, iph: str) -> None:
    if not st.free_rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)


async def _check_workspace_access(st, request: Request, workspace_id: str) -> None:
    """The workspace token (404 for a bad id and a bad token alike) through the bounded state driver, under the cache
    read budget: it is a graph read before the bot check. An empty budget is 429, an unreachable or slow database
    503."""
    token = request.headers.get("x-workspace-token", "")
    if not st.cache_budget.take():
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)
    try:
        ok = await state_call(st, _workspace_token_ok, st.state_store_driver, workspace_id, token)
    except Exception as e:  # noqa: BLE001
        logger.warning("workspace token check unavailable (%s): refusing", type(e).__name__)
        raise HTTPException(status_code=503, detail=MSG_STATE_UNAVAILABLE) from None
    if not ok:
        raise HTTPException(status_code=404, detail=MSG_WORKSPACE_NOT_FOUND)


async def _answer_from_cache(st, s, iph: str, strategy: str, key: str) -> EventSourceResponse | None:
    """Free window, then the answer cache, for a public ask: the cached answer's response, or None on a miss (the ask
    goes on as a paid one). While draining the order flips, so a miss that is refused takes no window: the cache is read
    first, a hit then takes the free window, a miss is 503 ``MSG_DRAINING``."""
    draining = drain.DRAIN.draining
    if not draining:
        _take_free_window(st, iph)
    cached = await _read_cache(st, s, key)
    if cached is None:
        if draining:
            raise HTTPException(status_code=503, detail=MSG_DRAINING, headers=DRAINING_HEADERS)
        return None
    if draining:
        _take_free_window(st, iph)
    await _log_cached_hit(st, s, iph, strategy)
    return EventSourceResponse(_one_event({"event": "done", "cached": True, **cached}), sep="\n")


async def _read_cache(st, s, key: str) -> dict | None:
    """One token of the process-wide cache read budget, then ``state.cache_get``. Any failure is 503: a cached answer is
    never served on a guess and a read that failed never falls through to a paid call."""
    if not st.cache_budget.take():
        raise HTTPException(status_code=429, detail=MSG_READ_RATE)
    try:
        return await state_call(st, st.state.cache_get, key, s.answer_cache_ttl_hours)
    except Exception as e:  # noqa: BLE001
        logger.warning("answer cache unavailable (%s): refusing", type(e).__name__)
        raise HTTPException(status_code=503, detail=MSG_STATE_UNAVAILABLE) from None


async def _log_cached_hit(st, s, iph: str, strategy: str) -> None:
    """The cached answer's ledger row, through the bounded state driver. If it cannot be written the answer is not
    served: the free tier's accounting is the one thing a cache hit must not skip."""
    try:
        await state_call(st, store.log_query, st.state_store_driver, ip_hash=iph, strategy=strategy, cached=True,
                         **guard.ip_hash_version_fields(s))
    except Exception as e:  # noqa: BLE001
        logger.warning("cached-answer row not written (%s): refusing", type(e).__name__)
        raise HTTPException(status_code=503, detail=MSG_STATE_UNAVAILABLE) from None


_DENIALS = {Denied.DAILY_COUNT: (429, MSG_BUDGET), Denied.DAILY_SPEND: (429, MSG_BUDGET),
            Denied.IP_DAILY: (429, MSG_IP_BUDGET), Denied.INFLIGHT: (429, MSG_BUSY),
            Denied.KILL: (503, MSG_PAUSED), Denied.UNAVAILABLE: (503, MSG_STATE_UNAVAILABLE)}


async def _admit(st, iph: str, strategy: str, workspace: bool, estimate_micro: int) -> Lease:
    """Count the ask on the drain, then take its lease (``state.reserve``). Returns the lease, and with it the drain
    count: both belong to the :class:`PaidStream` from then on. A refusal raises its HTTP error and gives the count
    back; so does an exception or a cancellation, which also settles a lease that was granted but never reached the
    caller (the reserve runs on a thread that a cancellation does not interrupt: the lease is recorded there, before the
    checkpoint that delivers the cancellation)."""
    if not drain.DRAIN.try_enter():
        raise HTTPException(status_code=503, detail=MSG_DRAINING, headers=DRAINING_HEADERS)
    granted: list[Lease] = []
    try:
        outcome = await state_call(st, _reserve_noting, st.state, granted, ip_hash=iph, strategy=strategy,
                                   workspace=workspace, estimate_micro=estimate_micro, now_wall=time.time(),
                                   now_mono=time.monotonic())
    except StateUnavailable as e:
        drain.DRAIN.leave()
        logger.warning("reserve unavailable (%s): refusing", type(e).__name__)
        raise HTTPException(status_code=503, detail=MSG_STATE_UNAVAILABLE) from None
    except BaseException:
        await _abandon(st, granted)
        drain.DRAIN.leave()
        raise
    if isinstance(outcome, Denied):
        drain.DRAIN.leave()
        status, detail = _DENIALS[outcome]
        raise HTTPException(status_code=status, detail=detail)
    return outcome


def _reserve_noting(backend, granted: list[Lease], **kwargs) -> Lease | Denied:
    """``backend.reserve`` on the worker thread, remembering a granted lease where the caller can still find it when the
    result is lost to a cancellation."""
    outcome = backend.reserve(**kwargs)
    if isinstance(outcome, Lease):
        granted.append(outcome)
    return outcome


async def _abandon(st, granted: list[Lease]) -> None:
    """Settle a lease that no stream took (shielded; the failure is logged: the sweep charges the estimate then). It
    waits for a state slot as long as it takes: a settle that gave up would leave the lease to the sweep, a minute
    later."""
    for lease in granted:
        with anyio.CancelScope(shield=True):
            try:
                await settle_call(st, st.state.reconcile, lease.lease_id, outcome="abandoned", usage=None,
                                  cost_micro=None)
            except Exception as e:  # noqa: BLE001
                logger.error("settling an unused lease failed (%s): its estimate stays charged", type(e).__name__)


async def _one_event(event: dict) -> AsyncIterator[ServerSentEvent]:
    """A cached answer's single event as an async stream. A sync iterator would be wrapped by sse-starlette in
    ``iterate_in_threadpool``: every cached answer would wait for one of the 40 default worker threads, the pool that is
    full whenever Neo4j is slow."""
    yield sse_event(event)


def _stream_fn(strategy: str, workspace: bool = False):
    """The async event-stream function for this ask, looked up here at request time so a test can replace
    ``aanswer_stream`` / ``astream_workspace_answer`` on this module. The selection (and the lazy import of the agent
    package) is ``stream_runtime.select_twin``."""
    return select_twin(strategy, workspace, sec=aanswer_stream, workspace_twin=astream_workspace_answer)


def _check_admin(request: Request) -> None:
    token = request.app.state.settings.admin_token
    given = request.headers.get("x-admin-token", "")
    if not token or not secrets.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8")):
        raise HTTPException(status_code=404)


@router.get("/api/admin/policy")
async def admin_policy(request: Request):
    """``kill_switch`` is the stored level (``on``, ``retrieval_only`` or ``off``); ``effective`` is what the gates
    apply right now (the cached level: ``on`` while it is unread or stale, or the ``KILL_SWITCH`` env override is
    set)."""
    _check_admin(request)
    st = request.app.state
    return {"kill_switch": await run_in_threadpool(store.get_policy, st.driver, "kill_switch") or KILL_OFF,
            "effective": await _kill_level(st),
            "ledger": await run_in_threadpool(store.ledger_summary, st.driver)}


@router.post("/api/admin/policy")
async def admin_set_policy(body: PolicyRequest, request: Request):
    """Set the kill level through the backend, so the flip is immediate on this machine.

    1. A level that can be HELD (``state.hold_kill_level``: not ``off``, and at least as tight as the level the gates
       apply now; a cache that is stale or was never read applies ``on``) is applied in memory at once, here on the loop,
       BEFORE the flip waits for the admin limiter, and its database write is queued for the maintenance thread. An
       emergency kill is therefore never delayed by an earlier admin call stuck on a silent connection (up to ~120 s).
    2. The flip then runs on the admin limiter (one token, apart from the public state pool) and writes the level. If the
       token does not free within ``state_op_timeout_s``, or the write fails, a level that was held answers 200 with
       ``stored`` false (it is in force and will be stored); a level that was not held (a relaxation, or ``off``) answers
       503 and nothing changed, nothing is queued: it is applied only once stored. A held level is never reported as
       not applied because the KILL_SWITCH env override makes the gates read ``on``: the answer comes from the backend,
       not from comparing the effective level with the request.

    ``stored`` false means: in force on this machine now, not yet in the database. Confirm with ``GET /api/admin/policy``
    (``kill_switch`` is the stored level) once the database is back. A backend that has no ``hold_kill_level`` (a test
    double) skips step 1 and is judged by the effective level, as before.

    A pre-M5 image reads only ``on`` as stopped, so it treats ``retrieval_only`` as ``off``: before rolling back to one,
    set ``on`` or ``off`` (docs/v2/M5A_BUILD_PLAN.md section 1, I4)."""
    _check_admin(request)
    st, level = request.app.state, body.level
    hold = getattr(st.state, "hold_kill_level", None)
    held = None if hold is None else bool(hold(level))        # None: the backend cannot say before the write
    stored = True
    try:
        await admin_call(st, st.state.set_kill_level, level)
    except StateUnavailable as e:
        stored = False
        logger.warning("kill level %s was not stored (%s, held=%s)", level, type(e).__name__, held)
        if not await _flip_holds(st, level, held, e):
            if isinstance(e, NoStateSlot):
                # the flip never ran: nothing was applied and nothing is queued for a retry
                raise HTTPException(status_code=503, detail="another admin call is still running: the kill level was "
                                                            "not changed, try again") from None
            raise HTTPException(status_code=503,
                                detail="the kill level could not be stored and was not applied") from None
    logger.warning("kill switch set to %s by admin (stored=%s)", level, stored)
    return {"kill_switch": level, "stored": stored}


async def _flip_holds(st, level: str, held_before: bool | None, error: StateUnavailable) -> bool:
    """Whether a flip that could not be stored nonetheless holds on this machine with its write queued. The backend
    says so: ``hold_kill_level`` before the write, or ``KillNotStored.held`` after a failed one. Only a backend that says
    neither (a test double) is judged by the effective level, where a held tightening reads as itself."""
    if held_before:
        return True
    reported = getattr(error, "held", None)
    if reported is not None:
        return bool(reported)
    if held_before is None and not isinstance(error, NoStateSlot):
        return await _kill_level(st) == level
    return False


@router.get("/api/admin/state")
async def admin_state(request: Request):
    """The backend's snapshot (counters, leases, the kill level and its age) with what is running around it: the named
    thread limiters, the drain and the maintenance thread. Admin only, like the policy: a lease id and the per-address
    maximum are not for the public ``/api/stats``. The snapshot is taken on the admin limiter, so the report (the
    limiter counts above all) is there when the public state pool is full, which is when it is needed."""
    _check_admin(request)
    st = request.app.state
    try:
        snapshot = await admin_call(st, st.state.snapshot)
    except StateUnavailable:
        raise HTTPException(status_code=503, detail="the state store is not reachable") from None
    maintenance = getattr(st, "maintenance", None)
    return {"state": snapshot, "limiters": _limiter_counts(st.limiters),
            "drain": {"draining": drain.DRAIN.draining, "active": drain.DRAIN.active},
            "maintenance": {"alive": bool(maintenance is not None and maintenance.is_alive())},
            "pending_settles": getattr(st.state, "pending_settles", lambda: None)()}


def _limiter_counts(limiters) -> dict:
    return {name: {"borrowed": limiter.borrowed_tokens, "total": limiter.total_tokens}
            for name, limiter in limiters._asdict().items() if limiter is not None}
