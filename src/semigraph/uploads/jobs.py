"""Upload jobs and the workspace TTL sweeper (M4, docs/v2/M4_PLAN.md 4.2 and 5).

``UploadJob`` runs the state machine ``received -> validating -> parsing -> chunking -> embedding -> comparing ->
indexing -> ready | failed`` on a worker thread. The ROUTE (``serve/workspace_routes.py``) does the byte-level gate,
the identical-content short-circuit and acquires ``app.state.upload_slots`` (one upload at a time on the machine)
and reserves the day's upload budget BEFORE calling :func:`run_upload_job`; the job releases that slot on every path
(success, a known failure code, or an unexpected exception) — never the caller.

This module stays import-light at MODULE level (``tests/test_serve_monitor_isolation.py`` / ``test_static_ui.py``
pin that the API process never imports a document parser eagerly): every parser-adjacent import
(``uploads.parse``, ``uploads.units``, ``uploads.changes``, ``uploads.repo``) happens lazily, inside the functions
that actually run a job.

Progress is published two ways: an in-memory, append-only, per-(workspace, job) event log for every live SSE
watcher on THIS process (:class:`JobRegistry` — fan-out: every watcher sees every event, not just the next one), and
a full snapshot persisted via ``uploads.repo.put_job`` after every transition, so a client that (re)connects after
the log is gone (the job finished and its grace period elapsed, or this process restarted) can replay the job's
last known event from Neo4j.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sys
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger("semigraph.uploads.jobs")

SWEEP_INTERVAL_S = 15 * 60
SWEEP_STOP_TIMEOUT_S = 5

# M4 review (docs/v2/M4_PLAN.md 15.3): at most this many LIVE SSE watchers per (workspace, job) — a 4th gets 429.
MAX_LIVE_WATCHERS_PER_JOB = 3
# A finished job's event log is kept this long after its terminal event, so a watcher that reconnects just after
# the job ended still gets a live replay instead of falling back to the (coarser) persisted snapshot.
REGISTRY_GRACE_PERIOD_S = 60

# A progress write (``put_job``) is retried this many times only when the event is TERMINAL (ready/failed): a
# non-terminal write is best-effort (logged, never retried) so one flaky write can never kill a job (finding 27).
PUT_JOB_RETRY_ATTEMPTS = 3
PUT_JOB_RETRY_BACKOFF_S = 0.2

TERMINAL_STATES = ("ready", "failed")

# Fixed, non-upload-controlled client messages per failure code (never the raw exception text — docs/v2/M4_PLAN.md 5).
JOB_ERROR_MESSAGES = {
    "unsupported_type": "unsupported file type",
    "too_large": "the file is too large",
    "active_content": "the file contains active content and was rejected",
    "encrypted": "the file is encrypted and was rejected",
    "zip_bomb": "the file failed a compression safety check",
    "empty": "the file is empty",
    "parse_failed": "the document could not be parsed",
    "timeout": "processing took too long and was stopped",
    "scanned": "the document appears to be scanned; no extractable text was found",
    "too_many_pages": "the document has more pages than this workspace allows",
    "too_many_tokens": "the document is larger than this workspace allows",
    "too_many_chunks": "the document has more sections than this workspace allows",
    "workspace_quota": "this workspace has reached its embedded-content limit",
    "workspace_deleted": "this workspace no longer exists",
    "interrupted": "processing was interrupted and could not finish",
    "internal_error": "the upload could not be processed",
}


class _NeverRaised(Exception):
    """Never raised by anything — a placeholder ``except`` target for a cross-worker exception (``repo.WorkspaceGone``)
    that may not exist yet on ``uploads.repo`` (docs/v2/M4_PLAN.md 15.4): resolving it once with
    ``getattr(repo, "WorkspaceGone", _NeverRaised)`` means a missing attribute never turns an unrelated exception
    into an ``AttributeError`` inside an ``except`` clause."""


def ws_hash_for_log(workspace_id: str) -> str:
    """Never log a raw workspace id (docs/v2/M4_PLAN.md 5) — only its sha256 prefix. Public: the route layer logs
    the same hash for an upload's accept/reject lines, so a workspace's log lines correlate without ever naming it."""
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


_ws_hash = ws_hash_for_log   # short internal alias used throughout this module


class _JobLog:
    """One job's append-only event log plus its live-SSE-watcher count. ``finished_at`` (a monotonic timestamp) is
    set the moment a terminal event is appended, and drives the registry's grace-period pruning."""

    __slots__ = ("events", "watchers", "finished_at")

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.watchers = 0
        self.finished_at: float | None = None


class JobRegistry:
    """In-memory, per-MACHINE: an append-only event log per ``(workspace_id, job_id)``, so one workspace's token
    can never observe another workspace's job. FAN-OUT (docs/v2/M4_PLAN.md 15.3): every watcher reads the same log
    from its own cursor, so every watcher sees every event — unlike a ``queue.Queue``, where each item goes to
    exactly one consumer. A finished job's log is kept for :data:`REGISTRY_GRACE_PERIOD_S` after its terminal event
    (pruned lazily, on the next access) so a watcher that reconnects just after the job ended still gets a live
    replay; :func:`try_watch` caps live watchers at :data:`MAX_LIVE_WATCHERS_PER_JOB` per job. Durable state (for
    replay once a log is gone) lives in Neo4j via ``uploads.repo.get_job``."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._logs: dict[tuple[str, str], _JobLog] = {}
        self._clock = clock

    def create(self, workspace_id: str, job_id: str) -> None:
        with self._lock:
            self._logs[(workspace_id, job_id)] = _JobLog()

    def append(self, workspace_id: str, job_id: str, event: dict) -> None:
        """No-op when the job is unknown here (e.g. a test that never called :meth:`create`, or the log was
        already pruned) — the caller's own persistence to Neo4j is what a late reader ultimately falls back on."""
        with self._lock:
            log = self._logs.get((workspace_id, job_id))
            if log is None:
                return
            log.events.append(event)
            if event.get("state") in TERMINAL_STATES:
                log.finished_at = self._clock()

    def events_from(self, workspace_id: str, job_id: str, start: int) -> tuple[list[dict], int] | None:
        """``(new_events, new_cursor)`` for a watcher whose cursor is ``start``, or ``None`` when the job is
        unknown here (never existed on this process, or its grace period has elapsed) — the caller should then
        fall back to the persisted snapshot."""
        with self._lock:
            self._prune_locked()
            log = self._logs.get((workspace_id, job_id))
            if log is None:
                return None
            return list(log.events[start:]), len(log.events)

    def try_watch(self, workspace_id: str, job_id: str) -> bool | None:
        """``None`` when the job is unknown here (fall back to the persisted replay); ``False`` when it exists but
        already has :data:`MAX_LIVE_WATCHERS_PER_JOB` live watchers (the caller answers 429); ``True`` once this
        call has reserved a watcher slot — the caller MUST eventually call :meth:`release_watch` exactly once."""
        with self._lock:
            self._prune_locked()
            log = self._logs.get((workspace_id, job_id))
            if log is None:
                return None
            if log.watchers >= MAX_LIVE_WATCHERS_PER_JOB:
                return False
            log.watchers += 1
            return True

    def release_watch(self, workspace_id: str, job_id: str) -> None:
        """Idempotent: releasing a job whose log has already been pruned, or over-releasing, is a safe no-op."""
        with self._lock:
            log = self._logs.get((workspace_id, job_id))
            if log is not None and log.watchers > 0:
                log.watchers -= 1

    def is_terminal(self, workspace_id: str, job_id: str) -> bool:
        with self._lock:
            log = self._logs.get((workspace_id, job_id))
            return bool(log is not None and log.finished_at is not None)

    def discard(self, workspace_id: str, job_id: str) -> None:
        """Explicit, immediate removal — used by tests; production code lets grace-period pruning handle it."""
        with self._lock:
            self._logs.pop((workspace_id, job_id), None)

    def keys(self) -> frozenset[tuple[str, str]]:
        """A snapshot of every ``(workspace_id, job_id)`` THIS process currently tracks — including a job whose
        terminal event just landed and is only counting down its grace period, which is harmless to include here
        too (its persisted state is already terminal, so the interrupted-job sweep's own state filter would skip
        it anyway). Used by the sweeper (docs/v2/M4_PLAN.md 15.4, finding 27) so a job genuinely running (or just
        finished) on THIS machine is never marked ``interrupted`` by the age-based Neo4j sweep, even when its
        non-terminal progress writes have gone quiet for a while (best-effort by design — see :func:`_persist_job`)."""
        with self._lock:
            return frozenset(self._logs.keys())

    def _prune_locked(self) -> None:
        now = self._clock()
        stale = [key for key, log in self._logs.items()
                if log.finished_at is not None and now - log.finished_at > REGISTRY_GRACE_PERIOD_S]
        for key in stale:
            del self._logs[key]


def registry(app) -> JobRegistry:
    """``app.state.upload_jobs``, created lazily (a test that builds ``app.state`` by hand need not call
    :func:`start_if_enabled` first)."""
    reg = getattr(app.state, "upload_jobs", None)
    if reg is None:
        reg = app.state.upload_jobs = JobRegistry()
    return reg


def count_tokens_available(embedder) -> bool:
    """True when ``embedder`` can count tokens (the ONNX and local backends can; the remote backend cannot —
    docs/v2/M4_PLAN.md 4.2: uploads stay unavailable rather than silently skip the token caps)."""
    try:
        embedder.count_tokens("probe")
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001 — an embedder error here must not crash the boot probe
        logger.exception("probing the embedder's token counter failed")
        return False
    return True


def _persist_job(driver, workspace_id: str, job: dict) -> None:
    """Persists ``job`` via ``repo.put_job`` (finding 27): a TERMINAL event (ready/failed) is retried up to
    :data:`PUT_JOB_RETRY_ATTEMPTS` times — a client that only ever sees the persisted replay must not be stuck on a
    non-terminal state forever because one write blipped — while a non-terminal progress write is best-effort (one
    attempt, logged at WARNING on failure, never retried and never fatal to the job: a client still watching the
    live, in-memory log already has the event regardless of whether Neo4j accepted it).

    ``repo.WorkspaceGone`` (raised once another worker's ``put_job``/``put_version`` locking lands — see
    ``jobs._process``) is never retried and always re-raised: it means the workspace is gone, not that the write
    was flaky."""
    from . import repo

    workspace_gone = getattr(repo, "WorkspaceGone", _NeverRaised)
    is_terminal = job.get("state") in TERMINAL_STATES
    attempts = PUT_JOB_RETRY_ATTEMPTS if is_terminal else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            repo.put_job(driver, workspace_id, job)
            return
        except workspace_gone:
            raise
        except Exception as exc:  # noqa: BLE001 — a progress write must never crash the job (finding 27)
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(PUT_JOB_RETRY_BACKOFF_S)
    level = logger.error if is_terminal else logger.warning
    level("upload job progress write failed ws_hash=%s job_id=%s state=%s terminal=%s attempts=%d: %s",
         _ws_hash(workspace_id), job.get("job_id"), job.get("state"), is_terminal, attempts,
         type(last_exc).__name__)


def _emit(driver, workspace_id: str, reg: JobRegistry, job: dict) -> None:
    """Persists ``job`` FIRST, then publishes it to the live, in-memory log (every watcher sees it, fan-out —
    docs/v2/M4_PLAN.md 15.3). A non-terminal or terminal write that fails for an ORDINARY reason is still
    best-effort/retried-then-logged inside :func:`_persist_job` and never raises, so a live watcher still sees
    progress on a flaky-but-recoverable Neo4j write. Only ``repo.WorkspaceGone`` propagates out of
    :func:`_persist_job` — and this order means THAT event is never appended here at all: the caller (``_worker``)
    is left to emit the accurate ``workspace_deleted`` terminal event instead, so a live watcher's log can never
    show a stale/misleading event (e.g. ``too_many_pages``) as its last word when the true final state is that the
    workspace no longer exists (docs/v2/M4_PLAN.md 15.4, C3 item 1)."""
    _persist_job(driver, workspace_id, job)
    reg.append(workspace_id, job["job_id"], job)


def _emit_local_only(reg: JobRegistry, workspace_id: str, job: dict) -> None:
    """Publish ``job`` to the live log WITHOUT persisting it: used exactly once, for the terminal
    ``workspace_deleted`` failure (docs/v2/M4_PLAN.md 15.4) — the workspace is already gone, so nothing further may
    be written for it, not even the failure event itself."""
    reg.append(workspace_id, job["job_id"], job)


def _fail(driver, workspace_id: str, reg: JobRegistry, job_id: str, document_id: str, version: int | None,
          code: str, *, detail: str = "", persist: bool = True) -> None:
    logger.info("upload job failed ws_hash=%s job_id=%s code=%s%s", _ws_hash(workspace_id), job_id, code,
               f" ({detail})" if detail else "")
    job = {"job_id": job_id, "state": "failed", "document_id": document_id, "version": version,
          "error": {"code": code, "message": JOB_ERROR_MESSAGES.get(code, JOB_ERROR_MESSAGES["internal_error"])}}
    if persist:
        _emit(driver, workspace_id, reg, job)
    else:
        _emit_local_only(reg, workspace_id, job)


def _lower_priority() -> None:
    """Linux only, best-effort: the embedding loop runs at a lower OS priority so a concurrent live ask wins CPU
    contention on the shared machine (docs/v2/M4_PLAN.md 4.2, risk 13)."""
    import os

    if sys.platform != "linux" or not hasattr(os, "setpriority"):
        return
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except OSError:
        logger.debug("setpriority(nice 10) failed; continuing at the default priority")


def _version_view(row: dict):
    from .changes import VersionView
    from .units import Unit

    units = tuple(Unit(unit_id=u["unit_id"], kind=u["kind"], headline=u["headline"] or "",
                       char_start=u["char_start"], char_end=u["char_end"]) for u in row["units"])
    spans = tuple((cid, start, end) for cid, start, end in row["chunk_spans"])
    return VersionView(text=row["text"], units=units, chunk_spans=spans, method=row["method"],
                      chars_per_page=row["chars_per_page"] or 0.0)


def _parse_or_fail(driver, ws, reg, job_id, document_id, version, data, kind, settings):
    from .parse import ParseError, parse_document

    try:
        return parse_document(data, kind, timeout_s=settings.upload_parse_timeout_s,
                              max_pages=settings.upload_max_pages)
    except ParseError as e:
        # Logs e.code and e.exc_type ONLY — never e.message (docs/v2/M4_PLAN.md 15.7): exc_type is the sandboxed
        # child's own exception CLASS NAME (e.g. "PdfReadError"), useful for triage; type(e).__name__ would always
        # be the constant string "ParseError" and tell an operator nothing.
        _fail(driver, ws, reg, job_id, document_id, version, e.code, detail=e.exc_type or "")
        return None


def _build_units_and_chunks(text: str, blocks, kind: str, embedder, settings):
    from ..hashing import content_hash
    from .units import chunk_units, detect_units

    units = detect_units(blocks, kind)
    chunks = chunk_units(text, units, count_tokens=embedder.count_tokens, max_tokens=settings.upload_max_chunk_tokens)
    unit_dicts = [{"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline,
                  "text_hash": content_hash(text[u.char_start:u.char_end]),
                  "char_start": u.char_start, "char_end": u.char_end} for u in units]
    return units, chunks, unit_dicts


def _embed_chunks(driver, ws, document_id, chunk_texts, chunk_hashes, chunks, embedder, settings, emit):
    """Embeds only the chunks whose text_hash is not already embedded for this document. Returns
    ``{"vectors": {index: [float, ...]}, "already": {text_hash: embedding}}``; ``None`` if the workspace's
    embedded-token budget would be exceeded; the string ``"timeout"`` if the wall budget ran out first."""
    from . import repo

    already = repo.embedded_chunks(driver, ws, document_id)
    to_embed = [i for i, h in enumerate(chunk_hashes) if h not in already]
    new_tokens = sum(chunks[i].tokens for i in to_embed)
    current_tokens = repo.quota(driver, ws).get("embedded_tokens", 0)
    if to_embed and current_tokens + new_tokens > settings.upload_max_workspace_tokens:
        return None
    _lower_priority()
    vectors: dict[int, list[float]] = {}
    started = time.monotonic()
    for done, i in enumerate(to_embed, start=1):
        vectors[i] = [float(x) for x in embedder.encode_passages([chunk_texts[i]])[0]]
        elapsed = time.monotonic() - started
        remaining = len(to_embed) - done
        eta = round(elapsed / done * remaining, 1) if done else 0.0
        emit("embedding", progress={"done": done, "total": len(to_embed), "eta_s": eta})
        if elapsed > settings.upload_embed_timeout_s and remaining:
            return "timeout"
    wall_s = round(time.monotonic() - started, 3)
    logger.info("upload embed ws_hash=%s document_id=%s tokens=%d wall_s=%.3f tokens_per_s=%.1f",
               _ws_hash(ws), document_id, new_tokens, wall_s, (new_tokens / wall_s) if wall_s > 0 else 0.0)
    return {"vectors": vectors, "already": already}


def _compare_with_previous(driver, ws, document_id, latest, text, units, chunk_rows, parsed):
    from .changes import compare_versions

    if latest is None:
        # Same shape as changes.compare_versions' own "not compared" result (docs/v2/M4_PLAN.md 15.6/15): every key
        # compare_versions ever returns must be present here too, including minor_rewordings — a consumer (the
        # page's changesHtml) must never need a special case for "first version" versus "not compared".
        return {"items_compared": False, "not_compared_reason": "first_version",
               "added": [], "removed": [], "changed": [], "minor_rewordings": [], "unchanged_count": 0}
    from . import repo

    older_row = repo.version_view(driver, ws, document_id, latest["version"])
    older_view = _version_view(older_row)
    newer_view = _version_view({"text": text, "units": [
        {"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline, "char_start": u.char_start,
        "char_end": u.char_end} for u in units],
        "chunk_spans": [(r["chunk_id"], r["char_start"], r["char_end"]) for r in chunk_rows],
        "method": parsed.method, "chars_per_page": parsed.chars_per_page})
    return compare_versions(older_view, newer_view)


def _workspace_page_quota_exceeded(driver, ws: str, document_id: str, new_pages: int, settings) -> bool:
    """The 120-page WORKSPACE cap (docs/v2/M4_PLAN.md 15.8, ``upload_max_workspace_pages``), distinct from the
    per-version page cap above. Precise for a re-upload of an EXISTING document (``repo.quota``'s
    ``pages_by_document``, C3 item 5): the target document's OWN current pages are subtracted from the workspace
    total before the new version's pages are added back in, so a same-size (or smaller) new version of a document
    already sitting at the cap is accepted, while a brand-new document — or a bigger version — that would push the
    total over the cap is still refused."""
    from . import repo

    quota = repo.quota(driver, ws)
    current_total = quota.get("pages", 0)
    document_current_pages = quota.get("pages_by_document", {}).get(document_id, 0)
    return current_total - document_current_pages + new_pages > settings.upload_max_workspace_pages


def _parse_stage(driver, ws, reg, job_id, document_id, version, data, kind, settings, embedder):
    """Parses, then enforces the per-version page cap, the workspace-wide page cap and the whole-document token
    cap. Returns ``(parsed, text)``, or ``None`` once a failure event has already been emitted."""
    from .units import canonical_text

    parsed = _parse_or_fail(driver, ws, reg, job_id, document_id, version, data, kind, settings)
    if parsed is None:
        return None
    if parsed.pages > settings.upload_max_pages:
        _fail(driver, ws, reg, job_id, document_id, version, "too_many_pages")
        return None
    if _workspace_page_quota_exceeded(driver, ws, document_id, parsed.pages, settings):
        _fail(driver, ws, reg, job_id, document_id, version, "workspace_quota")
        return None
    text = canonical_text(parsed.blocks)
    if embedder.count_tokens(text) > settings.upload_max_tokens:
        _fail(driver, ws, reg, job_id, document_id, version, "too_many_tokens")
        return None
    return parsed, text


def _chunk_and_embed_stage(driver, ws, reg, job_id, document_id, version, text, parsed, kind, embedder, settings,
                           emit):
    """Chunks, then embeds the new chunks. Returns ``(units, unit_dicts, chunk_rows)``, or ``None`` once a failure
    event has already been emitted (``too_many_chunks``, ``workspace_quota`` or ``timeout``)."""
    from ..hashing import content_hash
    from ..retrieval.ids import doc_id as make_doc_id

    units, chunks, unit_dicts = _build_units_and_chunks(text, parsed.blocks, kind, embedder, settings)
    if len(chunks) > settings.upload_max_chunks:
        # Enforced right after chunking, BEFORE any embedding (docs/v2/M4_PLAN.md 15.8 / finding 8): a
        # whitespace-heavy or short-paragraph-heavy document can produce hundreds of chunks well under the
        # whole-document token cap, and each chunk is embedded and written with a 1024-float vector.
        _fail(driver, ws, reg, job_id, document_id, version, "too_many_chunks")
        return None
    chunk_texts = [text[c.char_start:c.char_end] for c in chunks]
    chunk_hashes = [content_hash(t) for t in chunk_texts]
    emit("embedding", progress={"done": 0, "total": len(chunks), "eta_s": None})
    embedded = _embed_chunks(driver, ws, document_id, chunk_texts, chunk_hashes, chunks, embedder, settings, emit)
    if embedded is None or embedded == "timeout":
        _fail(driver, ws, reg, job_id, document_id, version, "workspace_quota" if embedded is None else "timeout")
        return None
    chunk_rows = [{"chunk_id": make_doc_id(document_id, version, i), "seq": i, "text": chunk_texts[i],
                  "text_hash": chunk_hashes[i], "char_start": c.char_start, "char_end": c.char_end,
                  "tokens": c.tokens, "embedded": i in embedded["vectors"],
                  "embedding": embedded["vectors"].get(i) or embedded["already"].get(chunk_hashes[i])}
                 for i, c in enumerate(chunks)]
    return units, unit_dicts, chunk_rows


def _process(driver, embedder, settings, ws, job_id, document_id, title, data, kind, content_hash_hex, reg) -> None:
    from ..retrieval.workspace import looks_suspicious
    from . import repo
    from .versions import next_version

    def emit(state: str, version: int | None = None, **extra) -> None:
        _emit(driver, ws, reg, {"job_id": job_id, "state": state, "document_id": document_id, "version": version,
                                **extra})

    emit("received")
    latest = repo.latest_version(driver, ws, document_id)
    version = next_version([latest["version"]] if latest else [])
    emit("validating", version=version)

    emit("parsing", version=version)
    parsed_result = _parse_stage(driver, ws, reg, job_id, document_id, version, data, kind, settings, embedder)
    if parsed_result is None:
        return
    parsed, text = parsed_result

    emit("chunking", version=version)
    chunked = _chunk_and_embed_stage(driver, ws, reg, job_id, document_id, version, text, parsed, kind, embedder,
                                     settings, lambda state, **kw: emit(state, version=version, **kw))
    if chunked is None:
        return
    units, unit_dicts, chunk_rows = chunked

    emit("comparing", version=version)
    change_report = _compare_with_previous(driver, ws, document_id, latest, text, units, chunk_rows, parsed)
    # Defence in depth on top of the linear-time regex fix (docs/v2/M4_PLAN.md 15.12): collapsing whitespace runs
    # first means even a pathological pattern this heuristic does not yet anticipate stays bounded by the
    # (already-capped) token count rather than the raw character count of an upload.
    suspicious = looks_suspicious(" ".join(text.split()))

    emit("indexing", version=version)
    workspace_gone = getattr(repo, "WorkspaceGone", _NeverRaised)
    try:
        repo.put_version(driver, ws, document_id=document_id, title=title, version=version,
                         content_hash=content_hash_hex, method=parsed.method, pages=parsed.pages, chars=len(text),
                         chars_per_page=parsed.chars_per_page, text=text, units=unit_dicts, chunks=chunk_rows,
                         change_report=change_report, suspicious=suspicious, now=datetime.now(UTC))
    except workspace_gone:
        # The workspace was deleted (or TTL-swept) while this job ran (docs/v2/M4_PLAN.md 15.4): put_version wrote
        # NOTHING (it locks the UserWorkspace node first and refuses otherwise), so nothing here may write further
        # either — not even this failure event, which goes to the live log only, never to Neo4j.
        _fail(driver, ws, reg, job_id, document_id, version, "workspace_deleted", persist=False)
        return
    emit("ready", version=version, chunks=len(chunk_rows), units=len(unit_dicts),
        items_compared=change_report["items_compared"], not_compared_reason=change_report["not_compared_reason"],
        suspicious=suspicious)


def _worker(app, ws: str, job_id: str, document_id: str, title: str | None, data: bytes, kind: str,
           content_hash_hex: str) -> None:
    """Ends in ready/failed on every path (never a silently dead thread) and always releases the upload slot.
    The job's registry entry is NOT discarded here: :class:`JobRegistry` keeps a finished job's log for
    :data:`REGISTRY_GRACE_PERIOD_S` (pruned lazily on the next access) so a watcher that reconnects moments after
    the job ends still gets a live replay instead of falling back to the persisted snapshot.

    ``repo.WorkspaceGone`` is caught here TOO, not only around the ``put_version`` call inside ``_process``: since
    ``put_job`` (every progress write) now ALSO locks the workspace and raises it, a delete/sweep that lands during
    an earlier stage (parsing, chunking, comparing) surfaces the same way as one that lands right at indexing — a
    local-only ``workspace_deleted`` event, never a second, doomed write attempt against a workspace that is
    already gone.

    A SECOND, subtler window (C3 item 1, docs/v2/M4_PLAN.md 15.4): a workspace deletion can first surface as some
    OTHER exception (e.g. ``repo.version_view`` starts returning ``None`` for a deleted document mid-``comparing``,
    which ``_version_view`` then fails to unpack) — caught below by ``except Exception``, which tries to report
    ``internal_error``. Reporting THAT failure is itself a ``put_job`` write, so it can ALSO raise
    ``WorkspaceGone`` (nothing was persisted); without the nested ``try`` below, that second exception would
    escape this function entirely — an unhandled exception silently killing the thread, exactly the bug this item
    fixes. Either window ends the same way: a local-only ``workspace_deleted`` event, never a crash."""
    from . import repo

    st = app.state
    reg = registry(app)
    workspace_gone = getattr(repo, "WorkspaceGone", _NeverRaised)
    try:
        _process(st.driver, st.embedder, st.settings, ws, job_id, document_id, title, data, kind,
                 content_hash_hex, reg)
    except workspace_gone:
        _fail(st.driver, ws, reg, job_id, document_id, None, "workspace_deleted", persist=False)
    except Exception:  # noqa: BLE001 — a job must always end in ready/failed, never a silently dead thread
        logger.exception("upload job crashed ws_hash=%s job_id=%s", _ws_hash(ws), job_id)
        try:
            _fail(st.driver, ws, reg, job_id, document_id, None, "internal_error")
        except workspace_gone:
            _fail(st.driver, ws, reg, job_id, document_id, None, "workspace_deleted", persist=False)
    finally:
        st.upload_slots.release()


def run_upload_job(app, *, workspace_id: str, document_id: str, title: str | None, data: bytes, kind: str,
                   content_hash_hex: str) -> str:
    """Starts the worker thread and returns its job id immediately. The caller must already hold
    ``app.state.upload_slots`` (acquired non-blocking) and have reserved today's upload budget.

    Once the thread has actually started, IT owns the upload slot's release (every path through :func:`_worker`
    releases it exactly once) — a ``BoundedSemaphore`` raises on a double release, so this function must never
    release it too. The only failure window is here, before ``thread.start()`` returns (for example the OS refusing
    a new thread): on that path the registry entry this call created is discarded (nothing will ever run to emit
    into it) and the exception propagates to the caller, which is what still owns the slot at that point and must
    release it itself (docs/v2/M4_PLAN.md 15.3/15.4, findings 5/17/23)."""
    job_id = secrets.token_hex(8)
    reg = registry(app)
    reg.create(workspace_id, job_id)
    thread = threading.Thread(target=_worker, name=f"upload-{job_id}", daemon=True,
                              args=(app, workspace_id, job_id, document_id, title, data, kind, content_hash_hex))
    try:
        thread.start()
    except BaseException:
        reg.discard(workspace_id, job_id)
        raise
    return job_id


# Extra silent time (beyond the parse + embed wall budgets) a genuinely LIVE job's put_job writes may go quiet
# for, on top of settings.upload_parse_timeout_s + settings.upload_embed_timeout_s, before the interrupted-job
# sweep's age threshold (docs/v2/M4_PLAN.md 15.4, finding 27, C3 item 2). Arithmetic: `_embed_chunks` only checks
# its wall budget BETWEEN chunks, so one slow chunk can carry the embed stage past upload_embed_timeout_s before
# that check fires; then `comparing` (a pure-Python alignment) and `put_version`'s own transaction still have to
# run before the NEXT progress write lands. None of those has its own settings-backed timeout today, so this
# margin is a fixed, generous allowance for all three combined, not a per-stage figure.
FAIL_INTERRUPTED_STAGE_MARGIN_S = 10 * 60


def _fail_interrupted_older_than_s(settings) -> int:
    """The age threshold below which a non-terminal job is presumed still running rather than abandoned by a dead
    process — derived from the SAME settings the job stages themselves use, so it can never silently fall out of
    sync with a future change to those budgets. Passed to ``repo.fail_interrupted_jobs`` together with the jobs this
    process still has open (``exclude``), which are never failed whatever their stored age."""
    return settings.upload_parse_timeout_s + settings.upload_embed_timeout_s + FAIL_INTERRUPTED_STAGE_MARGIN_S


class _Sweeper:
    """Deletes expired workspaces and orphaned ``User*`` nodes every :data:`SWEEP_INTERVAL_S`
    (``uploads.repo.sweep_expired`` / ``uploads.repo.sweep_orphans``, both idempotent), and marks any job left
    non-terminal by a dead process ``failed`` — at start AND on every cycle (docs/v2/M4_PLAN.md 15.4, finding 27, C3
    item 2: a process that died mid-embed must not wait a full :data:`SWEEP_INTERVAL_S` past this sweeper's own
    restart before its orphaned job is marked interrupted). Runs whenever a Neo4j driver exists, independent of
    ``UPLOADS_ENABLED`` (docs/v2/M4_PLAN.md 15.4, finding 26): the 24 h retention promise covers workspaces created
    before an operator's soft rollback (``UPLOADS_ENABLED=false``) too, and sweeping an empty label is a cheap
    indexed query. Same daemon-thread-plus-``Event`` shape as :class:`semigraph.serve.monitor.FreshnessMonitor`.

    ``registry`` and ``settings`` (both optional; production always passes both, via :func:`start_if_enabled`) feed
    ``repo.fail_interrupted_jobs``: a job still open in THIS process's :class:`JobRegistry` is excluded (never marked
    interrupted by age alone) and the age threshold derives from the job budgets; without them repo's defaults apply."""

    def __init__(self, driver, registry: JobRegistry | None = None, settings=None) -> None:
        self.driver = driver
        self.registry = registry
        self.settings = settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="upload-sweeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = SWEEP_STOP_TIMEOUT_S) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        self._safe_fail_interrupted()
        while not self._stop_event.wait(SWEEP_INTERVAL_S):
            self._safe_fail_interrupted()
            self._safe_sweep()
            self._safe_sweep_orphans()

    def _safe_fail_interrupted(self) -> None:
        from . import repo

        try:      # everything inside: nothing here may ever kill the sweeper thread
            kwargs = {}
            if self.settings is not None:
                kwargs["older_than_s"] = _fail_interrupted_older_than_s(self.settings)
            if self.registry is not None:
                kwargs["exclude"] = self.registry.keys()
            n = repo.fail_interrupted_jobs(self.driver, **kwargs)
            if n:
                logger.info("upload sweeper: marked %d interrupted job(s) failed", n)
        except Exception:  # noqa: BLE001 - boot must never crash or block on this
            logger.exception("upload sweeper: fail_interrupted_jobs failed")

    def _safe_sweep(self) -> None:
        from . import repo

        try:
            n = repo.sweep_expired(self.driver, datetime.now(UTC))
            if n:
                logger.info("upload sweeper: removed %d expired workspace(s)", n)
        except Exception:  # noqa: BLE001 - a failed sweep must not kill the sweeper thread
            logger.exception("upload sweeper: a sweep failed")

    def _safe_sweep_orphans(self) -> None:
        """A SEPARATE try/except from :meth:`_safe_sweep`: a missing or broken ``sweep_orphans`` must never stop
        the (already shipped, load-bearing) expired-workspace sweep from running every cycle."""
        from . import repo

        try:
            n = repo.sweep_orphans(self.driver, datetime.now(UTC))
            if n:
                logger.info("upload sweeper: removed %d orphaned node(s)", n)
        except Exception:  # noqa: BLE001 - a failed sweep must not kill the sweeper thread
            logger.exception("upload sweeper: sweep_orphans failed")


def start_if_enabled(app) -> None:
    """Starts the TTL sweeper unconditionally (finding 26: independent of ``UPLOADS_ENABLED``, see :class:`_Sweeper`).
    Also probes whether the configured embedder can count tokens and publishes the single availability flag
    ``app.state.uploads_ready`` that ``serve.routes.uploads_available`` reads (docs/v2/M4_PLAN.md 15.5, finding 29):
    uploads are available only when BOTH ``UPLOADS_ENABLED`` and this flag are true, so the page, ``/api/stats`` and
    every workspace route agree on whether uploads actually work.

    DEVIATION (reported, not silently done): this makes ``app.state.upload_sweeper`` non-None even when
    ``UPLOADS_ENABLED`` is false, which conflicts with the pre-review assertion in the forbidden, main-session-owned
    ``tests/test_serve_lifespan.py::test_with_both_m4_flags_off_nothing_background_starts_and_the_upload_gates_exist``
    (``st.upload_sweeper is None``). That test needs updating to assert the sweeper always starts; this worker does
    not edit it (out of file ownership) — reported as a seam."""
    registry(app)
    s = app.state.settings
    if s.uploads_enabled:
        app.state.uploads_ready = count_tokens_available(app.state.embedder)
        if not app.state.uploads_ready:
            logger.error("the configured embedding backend cannot count tokens — uploads will answer 503")
    else:
        app.state.uploads_ready = False
    app.state.upload_sweeper = _Sweeper(app.state.driver, registry(app), s)
    app.state.upload_sweeper.start()


def stop(app) -> None:
    """Stop the sweeper (bounded); a no-op when none runs. Called before the database driver closes."""
    sweeper = getattr(app.state, "upload_sweeper", None)
    if sweeper is not None:
        sweeper.stop()
