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

Progress is published two ways: an in-memory ``queue.Queue`` per (workspace, job) for a live SSE consumer on THIS
process (:class:`JobRegistry`), and a full snapshot persisted via ``uploads.repo.put_job`` after every transition,
so a client that (re)connects after the queue is gone (the job finished, or this process restarted) can replay the
job's last known event from Neo4j.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import secrets
import sys
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger("semigraph.uploads.jobs")

SWEEP_INTERVAL_S = 15 * 60
SWEEP_STOP_TIMEOUT_S = 5

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
    "workspace_quota": "this workspace has reached its embedded-content limit",
    "internal_error": "the upload could not be processed",
}


def ws_hash_for_log(workspace_id: str) -> str:
    """Never log a raw workspace id (docs/v2/M4_PLAN.md 5) — only its sha256 prefix. Public: the route layer logs
    the same hash for an upload's accept/reject lines, so a workspace's log lines correlate without ever naming it."""
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


_ws_hash = ws_hash_for_log   # short internal alias used throughout this module


class JobRegistry:
    """In-memory, per-MACHINE: the live progress queue of a job still running on this process, keyed by
    ``(workspace_id, job_id)`` so one workspace's token can never observe another workspace's job. Durable state
    (for replay once this queue is gone) lives in Neo4j via ``uploads.repo.get_job``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[tuple[str, str], queue.Queue] = {}

    def create(self, workspace_id: str, job_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[(workspace_id, job_id)] = q
        return q

    def get(self, workspace_id: str, job_id: str) -> queue.Queue | None:
        with self._lock:
            return self._queues.get((workspace_id, job_id))

    def discard(self, workspace_id: str, job_id: str) -> None:
        with self._lock:
            self._queues.pop((workspace_id, job_id), None)


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


def _emit(driver, workspace_id: str, reg: JobRegistry, job: dict) -> None:
    """Publish ``job`` to the live queue (if a consumer is attached) and persist it (for replay)."""
    from . import repo

    q = reg.get(workspace_id, job["job_id"])
    if q is not None:
        q.put(job)
    repo.put_job(driver, workspace_id, job)


def _fail(driver, workspace_id: str, reg: JobRegistry, job_id: str, document_id: str, version: int | None,
          code: str, *, detail: str = "") -> None:
    logger.info("upload job failed ws_hash=%s job_id=%s code=%s%s", _ws_hash(workspace_id), job_id, code,
               f" ({detail})" if detail else "")
    job = {"job_id": job_id, "state": "failed", "document_id": document_id, "version": version,
          "error": {"code": code, "message": JOB_ERROR_MESSAGES.get(code, JOB_ERROR_MESSAGES["internal_error"])}}
    _emit(driver, workspace_id, reg, job)


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
        _fail(driver, ws, reg, job_id, document_id, version, e.code, detail=type(e).__name__)
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
        return {"items_compared": False, "not_compared_reason": "first_version",
               "added": [], "removed": [], "changed": [], "unchanged_count": 0}
    from . import repo

    older_row = repo.version_view(driver, ws, document_id, latest["version"])
    older_view = _version_view(older_row)
    newer_view = _version_view({"text": text, "units": [
        {"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline, "char_start": u.char_start,
        "char_end": u.char_end} for u in units],
        "chunk_spans": [(r["chunk_id"], r["char_start"], r["char_end"]) for r in chunk_rows],
        "method": parsed.method, "chars_per_page": parsed.chars_per_page})
    return compare_versions(older_view, newer_view)


def _parse_stage(driver, ws, reg, job_id, document_id, version, data, kind, settings, embedder):
    """Parses, then enforces the page and whole-document token caps. Returns ``(parsed, text)``, or ``None`` once a
    failure event has already been emitted."""
    from .units import canonical_text

    parsed = _parse_or_fail(driver, ws, reg, job_id, document_id, version, data, kind, settings)
    if parsed is None:
        return None
    if parsed.pages > settings.upload_max_pages:
        _fail(driver, ws, reg, job_id, document_id, version, "too_many_pages")
        return None
    text = canonical_text(parsed.blocks)
    if embedder.count_tokens(text) > settings.upload_max_tokens:
        _fail(driver, ws, reg, job_id, document_id, version, "too_many_tokens")
        return None
    return parsed, text


def _chunk_and_embed_stage(driver, ws, reg, job_id, document_id, version, text, parsed, kind, embedder, settings,
                           emit):
    """Chunks, then embeds the new chunks. Returns ``(units, unit_dicts, chunk_rows)``, or ``None`` once a failure
    event has already been emitted (``workspace_quota`` or ``timeout``)."""
    from ..hashing import content_hash
    from ..retrieval.ids import doc_id as make_doc_id

    units, chunks, unit_dicts = _build_units_and_chunks(text, parsed.blocks, kind, embedder, settings)
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
    suspicious = looks_suspicious(text)

    emit("indexing", version=version)
    repo.put_version(driver, ws, document_id=document_id, title=title, version=version, content_hash=content_hash_hex,
                     method=parsed.method, pages=parsed.pages, chars=len(text), chars_per_page=parsed.chars_per_page,
                     text=text, units=unit_dicts, chunks=chunk_rows, change_report=change_report,
                     suspicious=suspicious, now=datetime.now(UTC))
    emit("ready", version=version, chunks=len(chunk_rows), units=len(unit_dicts),
        items_compared=change_report["items_compared"], not_compared_reason=change_report["not_compared_reason"],
        suspicious=suspicious)


def _worker(app, ws: str, job_id: str, document_id: str, title: str | None, data: bytes, kind: str,
           content_hash_hex: str) -> None:
    st = app.state
    reg = registry(app)
    try:
        _process(st.driver, st.embedder, st.settings, ws, job_id, document_id, title, data, kind,
                 content_hash_hex, reg)
    except Exception:  # noqa: BLE001 — a job must always end in ready/failed, never a silently dead thread
        logger.exception("upload job crashed ws_hash=%s job_id=%s", _ws_hash(ws), job_id)
        _fail(st.driver, ws, reg, job_id, document_id, None, "internal_error")
    finally:
        st.upload_slots.release()
        reg.discard(ws, job_id)


def run_upload_job(app, *, workspace_id: str, document_id: str, title: str | None, data: bytes, kind: str,
                   content_hash_hex: str) -> str:
    """Starts the worker thread and returns its job id immediately. The caller must already hold
    ``app.state.upload_slots`` (acquired non-blocking) and have reserved today's upload budget; the thread releases
    the slot on every path."""
    job_id = secrets.token_hex(8)
    registry(app).create(workspace_id, job_id)
    thread = threading.Thread(target=_worker, name=f"upload-{job_id}", daemon=True,
                              args=(app, workspace_id, job_id, document_id, title, data, kind, content_hash_hex))
    thread.start()
    return job_id


class _Sweeper:
    """Deletes expired workspaces every :data:`SWEEP_INTERVAL_S` (``uploads.repo.sweep_expired``, idempotent). Same
    daemon-thread-plus-``Event`` shape as :class:`semigraph.serve.monitor.FreshnessMonitor`."""

    def __init__(self, driver) -> None:
        self.driver = driver
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
        while not self._stop_event.wait(SWEEP_INTERVAL_S):
            self._safe_sweep()

    def _safe_sweep(self) -> None:
        from . import repo

        try:
            n = repo.sweep_expired(self.driver, datetime.now(UTC))
            if n:
                logger.info("upload sweeper: removed %d expired workspace(s)", n)
        except Exception:  # noqa: BLE001 - a failed sweep must not kill the sweeper thread
            logger.exception("upload sweeper: a sweep failed")


def start_if_enabled(app) -> None:
    """Start the TTL sweeper when ``UPLOADS_ENABLED``; ``app.state.upload_sweeper`` is None otherwise. Also probes
    whether the configured embedder can count tokens (``app.state.uploads_token_counter_ok``): the routes answer 503
    for every workspace route when it cannot (docs/v2/M4_PLAN.md 4.2)."""
    app.state.upload_sweeper = None
    registry(app)
    if not app.state.settings.uploads_enabled:
        app.state.uploads_token_counter_ok = False
        return
    app.state.uploads_token_counter_ok = count_tokens_available(app.state.embedder)
    if not app.state.uploads_token_counter_ok:
        logger.error("the configured embedding backend cannot count tokens — uploads will answer 503")
    app.state.upload_sweeper = _Sweeper(app.state.driver)
    app.state.upload_sweeper.start()


def stop(app) -> None:
    """Stop the sweeper (bounded); a no-op when none runs. Called before the database driver closes."""
    sweeper = getattr(app.state, "upload_sweeper", None)
    if sweeper is not None:
        sweeper.stop()
