"""Upload workspace HTTP surface (M4, docs/v2/M4_PLAN.md 4.4, 5, 14.6, 14.7, 15).

Every route here requires uploads to be available (``serve.routes.uploads_available`` — ``UPLOADS_ENABLED`` AND the
upload service actually started, docs/v2/M4_PLAN.md 15.5); every route but creation requires ``X-Workspace-Token``
and treats a malformed id, an unknown workspace and a wrong token identically (a Neo4j parameterised lookup
naturally returns no row for any of the three, so no separate shape-validation step is needed to get the
404-for-all-three behaviour the plan asks for). Every response — success or error — carries ``Cache-Control:
no-store``. Every GET route, plus DELETE, additionally takes the free-endpoint read-rate window
(``app.state.read_rate_limiter``, docs/v2/M4_PLAN.md 15.3) BEFORE authentication: a workspace GET is a database read
like ``/api/stats`` or ``/api/evidence``, and the window must bound the Neo4j lookup itself, not just the requests
that happen to pass it — otherwise an attacker who never has a valid token (cycling random workspace ids or wrong
tokens) is never rate-limited at all (round-2 review, findings S4/R2). The window is per client address, so gating
first never lets a legitimate owner lock out a different address, and the 404 stays indistinguishable either way.

The multipart upload body is parsed by hand, directly off the ASGI byte stream, with ``python-multipart``'s
low-level :func:`create_form_parser` (never FastAPI's ``UploadFile``/``Form`` — Starlette's own multipart parser
spills a part larger than 1 MiB to a real temp file, which the plan's "bytes live only in memory, never on disk"
rule (section 5) forbids at our 15 MiB cap): the byte-size cap is enforced WHILE reading, before python-multipart
ever sees more than the cap, ``MAX_MEMORY_FILE_SIZE`` is configured above the cap so a within-cap file never touches
disk either, the body is handed to the parser OFF the event loop (``run_in_threadpool``, finding 3), and at most
:data:`MAX_MULTIPART_PARTS` fields/files are accepted (the page sends ``file`` and an optional ``document_id``/
``title`` — three, generously rounded up).

The bot-check token travels in the ``X-Turnstile-Token`` HEADER, verified BEFORE the body is read (docs/v2/M4_PLAN.md
15.2) — the literal 14.6 gate order (UPLOADS_ENABLED -> workspace token -> kill switch -> Turnstile -> per-address
window -> streamed size cap -> byte gate -> quota -> slot -> daily budget) now holds exactly, since the token no
longer has to be parsed out of the multipart body first.

M5a I4 (docs/v2/M5_DECISIONS.md 2.2): while the process drains (``serve.drain``) workspace creation and an upload are
refused with 503 ``MSG_DRAINING`` BEFORE any window is consumed, and every other route here (reads, the job stream, the
evidence drawer) is served; an upload counts on the drain from the moment it takes the one upload slot until the job
thread releases it (:class:`DrainCountedSlot`), and a drain that began after the entry check (during the Turnstile call
or the body read) refuses the upload there with the same 503, before a job exists. The kill level (``state.kill_level``)
replaces the stored-flag read: an upload is refused unless it is ``off``, and a level that cannot be read is not
``off``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading

import python_multipart.multipart as multipart
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from ..retrieval.ids import DOC_ID_RE, DOCUMENT_ID_RE
from ..uploads import jobs, new_document_id, repo
from ..uploads.gate import GateError, check_bytes, sniff
from ..uploads.versions import next_version
from . import drain, guard, routes, store
from .state.backend import KILL_OFF

logger = logging.getLogger("semigraph.serve.workspace_routes")
router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}
UPLOAD_SIZE_SLACK = 8192          # multipart framing/header overhead allowed over the byte cap before the read aborts
MAX_TITLE_CHARS = 120
MAX_MULTIPART_PARTS = 8           # finding 3: file + turnstile-adjacent fields (document_id, title) plus headroom
SSE_POLL_INTERVAL_S = 0.5         # how often the async job-stream generator re-polls the in-memory event log
SSE_POLL_TIMEOUT_S = 10           # sse_starlette's own ping cadence while nothing new has happened
TERMINAL_STATES = jobs.TERMINAL_STATES

_GATE_STATUS = {"unsupported_type": 415, "active_content": 422, "encrypted": 422, "zip_bomb": 422, "empty": 422}
# python-multipart's own parse-failure classes (finding 3): a malformed body (missing boundary, missing
# Content-Type, garbage bytes) must become a 400 with NO_STORE, never a bare 500.
_MULTIPART_PARSE_ERRORS = (multipart.MultipartParseError, multipart.FormParserError, ValueError)

MSG_BOT = "Bot check failed — reload the page and try again."
MSG_UPLOAD_RATE = "Too many requests from your address — please wait a while and try again."
MSG_READ_RATE = "Too many requests from your address — please wait a while and try again."
MSG_BUSY = "The service is busy processing another upload — try again in a moment."
MSG_TOO_MANY_WATCHERS = "Too many viewers are already watching this upload — try again in a moment."
MSG_FILE_REQUIRED = "a file is required"
MSG_TITLE_TOO_LONG = f"title is limited to {MAX_TITLE_CHARS} characters"
MSG_UNKNOWN_DOCUMENT = "unknown document_id in this workspace"
MSG_MALFORMED_UPLOAD = "the upload could not be read"
MSG_NO_CHANGES = "no change report for that version pair"
MSG_NO_EVIDENCE = "no evidence with that id"


class DrainBegan(Exception):
    """A drain began between the upload route's own draining check and its taking the upload slot."""


class DrainCountedSlot(threading.BoundedSemaphore):
    """``app.state.upload_slots``: the one-upload-at-a-time semaphore, counted on the drain. A successful ``acquire`` is
    one running upload and the matching ``release`` (the job thread's ``finally``, or the route when it gives the slot
    up) ends it, so ``DRAIN.active`` covers the upload from acceptance until its thread has finished and the lifespan
    shutdown cannot close the database under it. The count is ``try_enter``, not ``enter``: the route's draining check
    runs at entry, then comes the Turnstile call and a client-paced body read, and a drain that began meanwhile must
    refuse the upload (``DrainBegan``, answered 503), not start a job the lifespan shutdown would cut after a 202. A
    refusal gives the semaphore back through the base class, because nothing was counted for ``release`` to uncount."""

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        got = super().acquire(blocking, timeout)
        if got and not drain.DRAIN.try_enter():
            super().release()
            raise DrainBegan()
        return got

    def release(self, n: int = 1) -> None:
        super().release(n)               # an over-release raises here, before the count changes
        drain.DRAIN.leave()


class WorkspaceCreateRequest(BaseModel):
    turnstile_token: str | None = None


def _limits(s) -> dict:
    return {"max_documents": s.upload_max_documents, "max_versions": s.upload_max_versions,
           "max_pages": s.upload_max_workspace_pages, "max_bytes": s.upload_max_bytes,
           "max_pages_per_version": s.upload_max_pages, "max_tokens_per_version": s.upload_max_tokens,
           "max_workspace_tokens": s.upload_max_workspace_tokens}


def _require_uploads_enabled(request: Request) -> None:
    """Finding 29 (docs/v2/M4_PLAN.md 15.5): the ONE predicate every surface uses, so this route, ``/api/stats`` and
    ``/api/ask`` can never disagree about whether uploads actually work."""
    if not routes.uploads_available(request.app.state):
        raise HTTPException(status_code=503, detail=routes.MSG_UPLOADS_OFF, headers=NO_STORE)


def _draining_refusal() -> HTTPException:
    return HTTPException(status_code=503, detail=routes.MSG_DRAINING, headers={**NO_STORE, **routes.DRAINING_HEADERS})


def _refuse_while_draining() -> None:
    """503 while the process drains: checked before any window is consumed, so a refused request costs nothing."""
    if drain.DRAIN.draining:
        raise _draining_refusal()


def _require_read_rate(request: Request) -> None:
    """The free-endpoint read-rate window on every workspace GET, and on DELETE (docs/v2/M4_PLAN.md 15.3, finding
    1/14/22; ordering fixed for findings S4/R2 in the round-2 review): a workspace GET is a database read exactly
    like ``/api/stats`` or the public ``/api/evidence``, and had no rate limit of its own before the first fix — the
    same gap that let an unbounded number of SSE watchers pile up. Callers MUST run this BEFORE
    :func:`_authenticate` — otherwise a caller who never has a valid token (an unknown workspace id or a wrong
    token) skips the limiter entirely, since it never reaches the code after a successful auth, and still pays a
    full Neo4j lookup on every attempt."""
    st, s = request.app.state, request.app.state.settings
    if not st.read_rate_limiter.allow(guard.hash_request_ip(request, s)):
        raise HTTPException(status_code=429, detail=MSG_READ_RATE, headers=NO_STORE)


async def _authenticate(request: Request, ws: str) -> None:
    """404 (never a distinguishable status) for a malformed id, an unknown workspace or a wrong token alike."""
    st = request.app.state
    token = request.headers.get("x-workspace-token", "")
    ok = await run_in_threadpool(routes._workspace_token_ok, st.driver, ws, token)
    if not ok:
        raise HTTPException(status_code=404, detail=routes.MSG_WORKSPACE_NOT_FOUND, headers=NO_STORE)
    await run_in_threadpool(repo.touch, st.driver, ws)


async def _check_upload_turnstile(request: Request, token: str | None) -> None:
    """Stricter than ``guard.verify_turnstile``'s ``/api/ask`` posture (docs/v2/M4_PLAN.md 3.6, 14.7): unconfigured
    fails CLOSED (503) in production, and is allowed-and-logged only outside it; configured, the ordinary
    verify-or-403 path applies."""
    s = request.app.state.settings
    if not s.turnstile_secret_key:
        if s.is_production:
            raise HTTPException(status_code=503, detail=routes.MSG_UPLOADS_OFF, headers=NO_STORE)
        logger.warning("uploads: Turnstile is not configured outside production — allowing and logging")
        return
    ip = guard.client_ip(request, s.client_ip_header)
    ok = await guard.verify_turnstile(token, ip, s.turnstile_secret_key, s.is_production, required=True)
    if not ok:
        raise HTTPException(status_code=403, detail=MSG_BOT, headers=NO_STORE)


# ---------------------------------------------------------------- POST /api/workspace


@router.post("/api/workspace")
async def create_workspace(body: WorkspaceCreateRequest, request: Request):
    st, s = request.app.state, request.app.state.settings
    _require_uploads_enabled(request)
    _refuse_while_draining()
    if not st.workspace_create_limiter.allow(guard.hash_request_ip(request, s)):
        raise HTTPException(status_code=429, detail=MSG_UPLOAD_RATE, headers=NO_STORE)
    await _check_upload_turnstile(request, body.turnstile_token)
    ws, token, expires_at = await run_in_threadpool(repo.create_workspace, st.driver, s.workspace_ttl_hours)
    return JSONResponse({"workspace_id": ws, "token": token, "expires_at": expires_at, "limits": _limits(s)},
                        status_code=201, headers=NO_STORE)


# ---------------------------------------------------------------- GET/DELETE /api/workspace/{ws}


@router.get("/api/workspace/{ws}")
async def get_workspace(ws: str, request: Request):
    _require_uploads_enabled(request)
    _require_read_rate(request)
    await _authenticate(request, ws)
    data = await run_in_threadpool(repo.get_workspace, request.app.state.driver, ws)
    if data is None:
        raise HTTPException(status_code=404, detail=routes.MSG_WORKSPACE_NOT_FOUND, headers=NO_STORE)
    return JSONResponse(data, headers=NO_STORE)


@router.delete("/api/workspace/{ws}", status_code=204)
async def delete_workspace(ws: str, request: Request):
    _require_uploads_enabled(request)
    _require_read_rate(request)
    await _authenticate(request, ws)
    await run_in_threadpool(repo.delete_workspace, request.app.state.driver, ws)
    return Response(status_code=204, headers=NO_STORE)


# ---------------------------------------------------------------- POST /api/workspace/{ws}/documents


class _TooLarge(Exception):
    pass


class _TooManyParts(Exception):
    """Finding 3: more than :data:`MAX_MULTIPART_PARTS` fields/files were presented — a body just under the byte cap
    made of tens of thousands of tiny fields still runs python-multipart's Python state machine for many seconds;
    counting parts in the callbacks bounds that work regardless of how small each individual part is."""


def _parse_multipart_body(parser, body: bytes) -> None:
    """Runs OFF the event loop, in a threadpool worker (finding 3): python-multipart's pure-Python state machine
    walks every byte of ``body`` inside this call. Run on the loop thread instead, a body engineered as many tiny
    same- or unique-named fields can pin it for tens of seconds — during which NOTHING else is served: not another
    upload, not an ``/api/ask`` stream, not even ``/healthz`` (CVE-2023-30798 pattern)."""
    parser.write(body)
    parser.finalize()


async def _read_multipart(request: Request, max_bytes: int):
    """Reads the multipart body straight off the ASGI stream, in memory only, then parses the WHOLE buffered body in
    one :func:`_parse_multipart_body` threadpool hop. Raises ``_TooLarge`` the moment the streamed body exceeds
    ``max_bytes`` — before anything is parsed; raises ``_TooManyParts`` once more than :data:`MAX_MULTIPART_PARTS`
    fields/files have been seen; lets ``create_form_parser``'s own ``ValueError`` (no ``Content-Type``) and
    python-multipart's ``MultipartParseError``/``FormParserError`` (a missing boundary, a malformed body) propagate
    to the caller, which maps all of these to a 400 with ``Cache-Control: no-store`` (finding 3) — never a bare 500."""
    fields: dict[str, bytes] = {}
    files: dict[str, tuple[str, str | None, bytes]] = {}
    part_count = 0

    def _count_part() -> None:
        nonlocal part_count
        part_count += 1
        if part_count > MAX_MULTIPART_PARTS:
            raise _TooManyParts()

    def on_field(field) -> None:
        _count_part()
        fields[(field.field_name or b"").decode("utf-8", "replace")] = field.value or b""

    def on_file(file) -> None:
        _count_part()
        name = (file.field_name or b"").decode("utf-8", "replace")
        filename = (file.file_name or b"").decode("utf-8", "replace")
        file.file_object.seek(0)
        files[name] = (filename, file.content_type, file.file_object.read())

    cap = max_bytes + UPLOAD_SIZE_SLACK
    parser = multipart.create_form_parser({"Content-Type": request.headers.get("content-type", "")}, on_field,
                                          on_file, config={"MAX_MEMORY_FILE_SIZE": cap, "MAX_BODY_SIZE": cap})
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > cap:
                raise _TooLarge()
        await run_in_threadpool(_parse_multipart_body, parser, bytes(body))
    finally:
        parser.close()
    return fields, files


def _content_length_too_large(request: Request, max_bytes: int) -> bool:
    declared = request.headers.get("content-length")
    if declared is None:
        return False
    try:
        return int(declared) > max_bytes + UPLOAD_SIZE_SLACK
    except ValueError:
        return False


def _gate_error_response(e: GateError):
    return JSONResponse({"detail": e.message, "code": e.code}, status_code=_GATE_STATUS.get(e.code, 422),
                        headers=NO_STORE)


async def _resolve_document(driver, ws: str, document_id_field: bytes | None, quota: dict):
    """``(document_id, is_new_document)``, or a ``JSONResponse`` (404) when a client-supplied id is malformed or
    unknown in this workspace — a client never chooses the id of a brand-new document."""
    if document_id_field is None:
        return new_document_id(), True
    document_id = document_id_field.decode("utf-8", "replace")
    if not DOCUMENT_ID_RE.match(document_id) or document_id not in quota["versions_by_document"]:
        return JSONResponse({"detail": MSG_UNKNOWN_DOCUMENT}, status_code=404, headers=NO_STORE)
    return document_id, False


def _quota_error(quota: dict, is_new_document: bool, document_id: str, settings):
    if is_new_document and quota["documents"] >= settings.upload_max_documents:
        return JSONResponse({"detail": "this workspace already has the maximum number of documents",
                            "code": "max_documents"}, status_code=429, headers=NO_STORE)
    if not is_new_document and quota["versions_by_document"].get(document_id, 0) >= settings.upload_max_versions:
        return JSONResponse({"detail": "this document already has the maximum number of versions",
                            "code": "max_versions"}, status_code=429, headers=NO_STORE)
    return None


async def _read_and_gate_body(request: Request, ws: str, s):
    """Reads the multipart body (in memory only), validates the ``file``/``title`` fields and runs the byte-level
    gate. Returns ``(data, kind, title, fields)``, or a ``JSONResponse`` for a size/part-count/malformed-body/gate
    rejection (a missing file or an over-long title raise directly: they are plain 400s, not a ``{detail,code}``
    shape). Turnstile is verified by the caller, from the ``X-Turnstile-Token`` HEADER, before this is ever called
    (docs/v2/M4_PLAN.md 15.2) — the literal 14.6 gate order holds exactly now that the token is not itself a
    multipart field."""
    try:
        fields, files = await _read_multipart(request, s.upload_max_bytes)
    except _TooLarge:
        return JSONResponse({"detail": "the file is too large", "code": "too_large"}, status_code=413,
                            headers=NO_STORE)
    except _TooManyParts:
        return JSONResponse({"detail": MSG_MALFORMED_UPLOAD, "code": "malformed"}, status_code=400, headers=NO_STORE)
    except _MULTIPART_PARSE_ERRORS as e:
        # A missing boundary, a missing Content-Type, or outright garbage bytes: never a bare 500 (finding 3).
        logger.info("upload rejected ws_hash=%s code=malformed_multipart (%s)", jobs.ws_hash_for_log(ws),
                   type(e).__name__)
        return JSONResponse({"detail": MSG_MALFORMED_UPLOAD, "code": "malformed"}, status_code=400, headers=NO_STORE)
    if "file" not in files:
        raise HTTPException(status_code=400, detail=MSG_FILE_REQUIRED, headers=NO_STORE)
    filename, _content_type, data = files["file"]
    title = fields.get("title")
    title = title.decode("utf-8", "replace") if title is not None else None
    if title is not None and len(title) > MAX_TITLE_CHARS:
        raise HTTPException(status_code=400, detail=MSG_TITLE_TOO_LONG, headers=NO_STORE)
    try:
        kind = sniff(data[:4096], filename)
        check_bytes(data, kind)
    except GateError as e:
        logger.info("upload rejected ws_hash=%s code=%s size=%d", jobs.ws_hash_for_log(ws), e.code, len(data))
        return _gate_error_response(e)
    return data, kind, title, fields


async def _finalize_upload(request: Request, ws: str, st, s, fields: dict, data: bytes, kind: str,
                           title: str | None):
    """Resolves the target document, short-circuits an unchanged re-upload, checks quotas, then takes the upload
    slot and the daily budget (in that order) and starts the job.

    Findings 5/17/23: EVERY path from a successful ``upload_slots.acquire()`` up to a successfully STARTED job is
    wrapped in try/except, so the slot is released on any exception in between (a transient Neo4j error from
    ``reserve_daily_upload``, or ``run_upload_job`` itself raising before its thread starts) — not just the
    documented ``daily_limit`` path. Once ``run_upload_job`` returns successfully, the WORKER THREAD owns the slot's
    release (it releases exactly once, on every one of its own paths); this function must never release it again
    after that point, or a later, unrelated release would double-release a ``BoundedSemaphore`` and raise."""
    quota = await run_in_threadpool(repo.quota, st.driver, ws)
    resolved = await _resolve_document(st.driver, ws, fields.get("document_id"), quota)
    if isinstance(resolved, JSONResponse):
        return resolved
    document_id, is_new_document = resolved

    content_hash_hex = hashlib.sha256(data).hexdigest()
    latest = None
    if not is_new_document:
        latest = await run_in_threadpool(repo.latest_version, st.driver, ws, document_id)
        if latest is not None and latest["content_hash"] == content_hash_hex:
            return JSONResponse({"unchanged": True, "document_id": document_id, "version": latest["version"]},
                                status_code=200, headers=NO_STORE)

    quota_error = _quota_error(quota, is_new_document, document_id, s)
    if quota_error is not None:
        return quota_error

    try:
        taken = st.upload_slots.acquire(blocking=False)
    except DrainBegan:                  # the drain began during the body read: nothing is counted, nothing is taken
        raise _draining_refusal() from None
    if not taken:
        return JSONResponse({"detail": MSG_BUSY, "code": "busy"}, status_code=429, headers=NO_STORE)
    try:
        reserved = await run_in_threadpool(store.reserve_daily_upload, st.driver, s.max_uploads_per_day)
        if not reserved:
            st.upload_slots.release()
            return JSONResponse({"detail": "the daily upload limit has been reached", "code": "daily_limit"},
                                status_code=429, headers=NO_STORE)

        version = next_version([latest["version"]] if latest else [])
        job_id = jobs.run_upload_job(request.app, workspace_id=ws, document_id=document_id, title=title, data=data,
                                     kind=kind, content_hash_hex=content_hash_hex)
    except BaseException:
        st.upload_slots.release()
        raise
    logger.info("upload accepted ws_hash=%s document_id=%s kind=%s size=%d job_id=%s", jobs.ws_hash_for_log(ws),
               document_id, kind, len(data), job_id)
    return JSONResponse({"job_id": job_id, "document_id": document_id, "version": version}, status_code=202,
                        headers=NO_STORE)


@router.post("/api/workspace/{ws}/documents", status_code=202)
async def upload_document(ws: str, request: Request):
    """Gate order, now literal (docs/v2/M4_PLAN.md 14.6, 15.2 — the Turnstile token moved to the
    ``X-Turnstile-Token`` header, so it no longer needs the body read first; 16: every workspace route takes the
    in-memory read-rate window before its database lookup): uploads-available -> draining (503) -> read-rate window ->
    workspace token (404) -> kill level -> Turnstile -> per-address upload window -> streamed size cap (declared,
    then live) -> byte gate -> quota -> slot -> daily budget."""
    st, s = request.app.state, request.app.state.settings
    _require_uploads_enabled(request)
    _refuse_while_draining()
    _require_read_rate(request)
    await _authenticate(request, ws)
    if await routes._kill_level(st) != KILL_OFF:        # on, retrieval_only, unread or stale: no new upload job
        raise HTTPException(status_code=503, detail=routes.MSG_UPLOADS_OFF, headers=NO_STORE)
    await _check_upload_turnstile(request, request.headers.get("x-turnstile-token"))
    if not st.upload_limiter.allow(guard.hash_request_ip(request, s)):
        raise HTTPException(status_code=429, detail=MSG_UPLOAD_RATE, headers=NO_STORE)
    if _content_length_too_large(request, s.upload_max_bytes):
        return JSONResponse({"detail": "the file is too large", "code": "too_large"}, status_code=413,
                            headers=NO_STORE)

    gated = await _read_and_gate_body(request, ws, s)
    if isinstance(gated, JSONResponse):
        return gated
    data, kind, title, fields = gated
    return await _finalize_upload(request, ws, st, s, fields, data, kind, title)


# ---------------------------------------------------------------- GET /api/workspace/{ws}/jobs/{job_id} (SSE)
#
# Findings 1/14/22 (docs/v2/M4_PLAN.md 15.3, CRITICAL): the OLD sync generator (`q.get(timeout=10)` on a
# `queue.Queue` shared by every watcher) had no exit path except the ONE terminal event a `queue.Queue` delivers to
# exactly one consumer — every other watcher looped forever. Because the generator was sync, sse_starlette ran it
# through `starlette.concurrency.iterate_in_threadpool`, whose `anyio.to_thread.run_sync` is not cancellable: each
# stuck watcher permanently pinned one thread and one of anyio's 40 default threadpool tokens, and a client
# disconnect could never free it. Enough stuck watchers starve every OTHER `run_in_threadpool` call in the whole
# process (auth, /api/ask, /healthz) — a full outage from a single job's worth of extra GETs.
#
# Fixed by making the generator ASYNC (never touches the threadpool: sse_starlette iterates an async generator
# natively) over the append-only, fan-out event log in JobRegistry (every watcher reads its own cursor, so every
# watcher sees every event, including the terminal one); it re-polls with `await asyncio.sleep`, so cancellation on
# client disconnect is immediate. At most `MAX_LIVE_WATCHERS_PER_JOB` watchers may be live on one job at a time (a
# 4th gets 429); the route now also takes the read-rate window BEFORE authentication, closing the "no rate limit at
# all for a caller who never has a valid token" gap the round-2 review found in the first ordering (S4/R2) — the
# same gap the review exploited to reach dozens of watchers on one job in the first place.


def _job_sse(job: dict) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(job, default=str), event="job", sep="\n")


async def _job_event_stream(app, reg: "jobs.JobRegistry", ws: str, job_id: str):
    """Never holds a threadpool thread: polls the in-memory log with ``await asyncio.sleep`` between checks, so a
    client disconnect (which raises ``GeneratorExit`` here) is immediate, not blocked behind a synchronous call.
    Ends on the terminal event, or — once the job is no longer in the registry (finished and its grace period
    elapsed, or never on this process) — replays the persisted final state via ONE short ``run_in_threadpool`` call
    and returns."""
    cursor = 0
    try:
        while True:
            result = reg.events_from(ws, job_id, cursor)
            if result is None:
                persisted = await run_in_threadpool(repo.get_job, app.state.driver, ws, job_id)
                if persisted is not None:
                    yield _job_sse(persisted)
                return
            events, cursor = result
            for job in events:
                yield _job_sse(job)
                if job.get("state") in TERMINAL_STATES:
                    return
            await asyncio.sleep(SSE_POLL_INTERVAL_S)
    finally:
        reg.release_watch(ws, job_id)


@router.get("/api/workspace/{ws}/jobs/{job_id}")
async def workspace_job_stream(ws: str, job_id: str, request: Request):
    _require_uploads_enabled(request)
    _require_read_rate(request)
    await _authenticate(request, ws)
    app = request.app
    reg = jobs.registry(app)
    watch = reg.try_watch(ws, job_id)
    if watch is False:
        raise HTTPException(status_code=429, detail=MSG_TOO_MANY_WATCHERS, headers=NO_STORE)
    if watch is None:
        persisted = await run_in_threadpool(repo.get_job, app.state.driver, ws, job_id)
        if persisted is None:
            raise HTTPException(status_code=404, detail="job not found", headers=NO_STORE)
        return EventSourceResponse(_replay_once(persisted), sep="\n", headers=NO_STORE)
    return EventSourceResponse(_job_event_stream(app, reg, ws, job_id), ping=SSE_POLL_TIMEOUT_S, sep="\n",
                               headers=NO_STORE)


async def _replay_once(persisted: dict):
    yield _job_sse(persisted)


# ---------------------------------------------------------------- GET /api/workspace/{ws}/changes


@router.get("/api/workspace/{ws}/changes")
async def workspace_changes(ws: str, request: Request, document_id: str,
                            older: int = Query(..., alias="from"), newer: int = Query(..., alias="to")):
    _require_uploads_enabled(request)
    _require_read_rate(request)
    await _authenticate(request, ws)
    result = await run_in_threadpool(repo.get_changes, request.app.state.driver, ws, document_id, older, newer)
    if result is None:
        raise HTTPException(status_code=404, detail=MSG_NO_CHANGES, headers=NO_STORE)
    return JSONResponse(result, headers=NO_STORE)


# ---------------------------------------------------------------- GET /api/workspace/{ws}/evidence/{doc_id}


@router.get("/api/workspace/{ws}/evidence/{doc_id}")
async def workspace_evidence(ws: str, doc_id: str, request: Request):
    _require_uploads_enabled(request)
    _require_read_rate(request)
    await _authenticate(request, ws)
    if not DOC_ID_RE.match(doc_id):
        raise HTTPException(status_code=404, detail=MSG_NO_EVIDENCE, headers=NO_STORE)
    row = await run_in_threadpool(repo.evidence, request.app.state.driver, ws, doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=MSG_NO_EVIDENCE, headers=NO_STORE)
    return JSONResponse(row, headers=NO_STORE)
