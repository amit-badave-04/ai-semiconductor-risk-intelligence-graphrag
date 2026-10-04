"""HTTP surface of the service. Paid work (``POST /api/ask``) passes every
gate in order — free-tier window, cache, kill switch, daily ceiling, Turnstile,
paid per-address window, concurrency slot — before a single LLM token is bought.
Nothing is written to the database before the first (free-tier) gate.

An ask over an upload workspace (M4, docs/v2/M4_PLAN.md 4.4) is ``hybrid`` only and never touches the answer cache; its token
is checked right after the free-tier window (404 for a bad id or token alike), and nothing is written before that check."""

import logging
import re
import secrets
import time
from functools import partial
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from ..artifacts import load_examples
from ..graph.client import run_cypher
from ..retrieval.answerer_async import aanswer_stream
from ..retrieval.ids import CHUNK_ID_RE, classify_id, metric_id_of, rule_id_of  # noqa: F401 - CHUNK_ID_RE: re-exported (tests/test_ids.py)
from ..retrieval.workspace_async import astream_workspace_answer
from ..uploads import WORKSPACE_TOKEN_MAX_CHARS
from . import guard, store
from .stream_runtime import (  # noqa: F401 - MSG_BUSY: re-exported (the message the busy event carries)
    MSG_BUSY, PaidResponse, PaidStream, select_twin, sse_event)

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
MSG_UPLOADS_OFF = "Uploaded documents are not available right now."
MSG_WORKSPACE_NOT_FOUND = "workspace not found"    # an unknown workspace and a wrong token must look the same
MSG_NOT_PUBLIC_EVIDENCE = "not a public evidence id"


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
    kill_switch: bool


def _read_gate(request: Request) -> None:
    """Per-address window for the free read endpoints (they hit the database)."""
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.ip_hash(guard.client_ip(request, s.client_ip_header))):
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
    iph = guard.ip_hash(ip)
    snapshot_id = getattr(st, "snapshot_id", "")

    # Free tier (cache hits) has its own, wider window — and it is the first gate,
    # so an unauthenticated client cannot write a ledger row without passing it.
    if not st.free_rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)
    if workspace is not None:
        # No cache on either side: an answer drawn from a private document must never be replayed to anyone else.
        token = request.headers.get("x-workspace-token", "")
        if not await run_in_threadpool(_workspace_token_ok, st.driver, workspace["workspace_id"], token):
            raise HTTPException(status_code=404, detail=MSG_WORKSPACE_NOT_FOUND)
    else:
        cached = await run_in_threadpool(store.get_answer, st.driver, store.cache_key(question, strategy, snapshot_id),
                                         s.answer_cache_ttl_hours)
        if cached:
            await run_in_threadpool(store.log_query, st.driver, ip_hash=iph, strategy=strategy, cached=True)
            event = {"event": "done", "cached": True, **cached}
            return EventSourceResponse(iter([sse_event(event)]), sep="\n")

    if await run_in_threadpool(store.kill_switch_on, st.driver, s.kill_switch):
        raise HTTPException(status_code=503, detail=MSG_PAUSED)
    if s.max_queries_per_day and await run_in_threadpool(store.paid_queries_today, st.driver) >= s.max_queries_per_day:
        raise HTTPException(status_code=429, detail=MSG_BUDGET)
    if not await guard.verify_turnstile(body.turnstile_token, ip, s.turnstile_secret_key,
                                        s.is_production, required=s.turnstile_required):
        raise HTTPException(status_code=403, detail=MSG_BOT)
    if not st.rate_limiter.allow(iph):
        raise HTTPException(status_code=429, detail=MSG_RATE)
    stream = PaidStream(st, question, strategy, iph, snapshot_id, workspace,
                        twin=_stream_fn(strategy, workspace is not None))
    return PaidResponse(stream, send_timeout=s.send_timeout_s)


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
