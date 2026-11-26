"""The background embedding queue of an upload workspace (docs/specs/m2-search.md R16, OD11, OD12; decision 009).

After the keyword-ready write (``uploads.jobs``) a version is searchable by keyword and its chunks wait here for vectors. One
worker thread takes tasks first in, first out, at the lowered OS priority, and embeds a version's pending chunks one at a time
(priority sections first: the repo returns them in priority, then document order), writing each vector as it is made. It
reuses the embedder the process already loaded (a second 1.26 GB copy would not fit the 4 GB machine).

Every limit degrades to "keyword only" instead of failing the upload (OD12): the time limit, the workspace token budget, the
guaranteed tokens of a non-priority section, a cancel, a chunk that was superseded, an embedder error, the day's embedding job
count. The unfinished chunks are marked keyword-only (``repo.finish_embedding``) so a restart does
not requeue them. A shutdown or a restart is the one exit that leaves them pending: ``resume_pending`` requeues exactly those.

While another upload parses (``ParseGate``) the worker pauses, because the parse child may use 1 GiB and the embedding model
already holds most of the rest; paused time does not count against the time limit.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .sections import NON_PRIORITY

logger = logging.getLogger("semigraph.uploads.embed_queue")

PAUSE_POLL_S = 0.5
STOP_JOIN_TIMEOUT_S = 5
RESUME_JOB_PREFIX = "resume-"
HELD = "held"                          # an Admission reason: queued behind the running job of its workspace or client

# Why a job ended with some chunks keyword-only; carried to the client as ``stopped`` (None = every chunk got a vector).
STOP_REASONS = frozenset({"timeout", "budget", "guaranteed", "cancelled", "superseded", "embed_error", "daily_limit"})


@dataclass(frozen=True)
class EmbedTask:
    workspace_id: str
    job_id: str
    document_id: str
    version: int
    client_key: str | None = None      # a hash, never a raw address (the route computes it); None for a resumed task
    resumed: bool = False
    summary: dict = field(default_factory=dict, compare=False)   # the keyword-ready summary, repeated on the final event


@dataclass(frozen=True)
class Admission:
    queued: bool
    reason: str | None = None


class ParseGate:
    """Counts the uploads that are parsing (or comparing) right now; the embedding worker pauses while it is busy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._encode_lock = threading.Lock()
        self._count = 0

    def encoding(self):
        """Held by the embedding worker around one encode: a parse that enters waits for it (never both at once)."""
        return self._encode_lock

    @property
    def busy(self) -> bool:
        return self._count > 0

    def enter(self) -> None:
        """Marks a parse as started (the worker begins no further chunk), then waits for an encode already running."""
        with self._lock:
            self._count += 1
        with self._encode_lock:
            pass

    def leave(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)

    def __enter__(self) -> ParseGate:
        self.enter()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.leave()
        return False


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _noop(*args, **kwargs) -> None:
    return None


@dataclass
class _Run:
    """What one task's loop has reached."""

    total: int
    done: int = 0
    embedded_tokens: int = 0
    stopped: str | None = None
    stages: dict = field(default_factory=dict)


class EmbedQueue:
    """See the module docstring. ``emit(task, state, **extra)`` publishes a job event (the caller persists and fans it out);
    ``reserve_day(day) -> bool`` takes one of the day's embedding jobs; ``log_stages(ws, job_id, stages)`` and
    ``record_cpu(stages, edge)`` write the operator stage line; ``lower_priority()`` runs once on the worker thread."""

    def __init__(self, driver, embedder, settings, *, emit: Callable, reserve_day: Callable[[str], bool],
                 clock=time.monotonic, sleep=time.sleep, log_stages: Callable = _noop, record_cpu: Callable = _noop,
                 lower_priority: Callable = _noop, today: Callable[[], str] = _today) -> None:
        self._driver, self._embedder, self._settings = driver, embedder, settings
        self._emit, self._reserve_day, self._today = emit, reserve_day, today
        self._clock, self._sleep = clock, sleep
        self._log_stages, self._record_cpu, self._lower_priority = log_stages, record_cpu, lower_priority
        self.parse_gate = ParseGate()
        self.stop_requested = threading.Event()
        self._fifo: queue.Queue[EmbedTask] = queue.Queue()
        self._lock = threading.Lock()
        self._active_workspaces: Counter = Counter()
        self._active_clients: Counter = Counter()
        self._held: list[EmbedTask] = []
        self._cancelled: dict[tuple[str, str], threading.Event] = {}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ admission

    def submit(self, task: EmbedTask) -> Admission:
        """Queues ``task``, holds it while its workspace or client has a running job (the one-active-job rule; it is admitted
        and charged to the day when that job ends), or degrades it to keyword-only at once (the day's job count)."""
        with self._lock:
            if not task.resumed and self._conflicts(task):
                self._held.append(task)
                self._cancelled.setdefault((task.workspace_id, task.job_id), threading.Event())
                return Admission(True, HELD)
            self._register(task)                  # taken first, so the day's DB write below runs without the lock
        reason = None if task.resumed else self._refusal(task)
        if reason is not None:
            self._unregister(task)
            self._degrade(task, reason)
            self._admit_held_safely()
            return Admission(False, reason)
        self._fifo.put(task)
        return Admission(True)

    def _conflicts(self, task: EmbedTask) -> bool:
        return bool(self._active_workspaces[task.workspace_id] or (task.client_key and self._active_clients[task.client_key]))

    def _refusal(self, task: EmbedTask) -> str | None:
        try:
            reserved = self._reserve_day(self._today())
        except Exception:  # noqa: BLE001 - fail closed: a counter that cannot be read means keyword-only, never an unbounded day
            logger.exception("upload embed day counter failed")
            reserved = False
        return None if reserved else "daily_limit"

    def _register(self, task: EmbedTask) -> None:
        self._active_workspaces[task.workspace_id] += 1
        if task.client_key:
            self._active_clients[task.client_key] += 1
        self._cancelled.setdefault((task.workspace_id, task.job_id), threading.Event())

    def _unregister(self, task: EmbedTask) -> None:
        with self._lock:
            self._active_workspaces[task.workspace_id] -= 1
            if self._active_workspaces[task.workspace_id] <= 0:
                del self._active_workspaces[task.workspace_id]
            if task.client_key:
                self._active_clients[task.client_key] -= 1
                if self._active_clients[task.client_key] <= 0:
                    del self._active_clients[task.client_key]
            self._cancelled.pop((task.workspace_id, task.job_id), None)

    def _release(self, task: EmbedTask) -> None:
        self._unregister(task)
        self._admit_held_safely()

    def _admit_held_safely(self) -> None:
        try:
            self._admit_held()
        except Exception:  # noqa: BLE001 - nothing may kill the worker thread; a held task that failed stays keyword-only
            logger.exception("upload embed: admitting held tasks failed")

    def _admit_held(self) -> None:
        """Admits every held task whose workspace and client are now free: the day is charged only here, outside the lock."""
        while True:
            with self._lock:
                task = next((t for t in self._held if not self._conflicts(t)), None)
                if task is None:
                    return
                self._held.remove(task)
                cancelled = self._cancelled[(task.workspace_id, task.job_id)].is_set()
                if cancelled:
                    self._cancelled.pop((task.workspace_id, task.job_id), None)
                else:
                    self._register(task)
            reason = "cancelled" if cancelled else self._refusal(task)
            if reason is None:
                self._fifo.put(task)
                continue
            if not cancelled:
                self._unregister(task)
            try:
                self._degrade(task, reason)
            except Exception:  # noqa: BLE001 - one held task failing must not strand the others behind it (S-01)
                logger.exception("upload embed: admitting a held task failed; its chunks stay keyword-only")

    def _degrade(self, task: EmbedTask, reason: str) -> None:
        """A task that never ran: its chunks stay keyword-only and the job ends ready (never failed)."""
        from . import repo

        try:
            skipped = repo.finish_embedding(self._driver, task.workspace_id, task.document_id, task.version)
            self._emit(task, "ready", embedded_chunks=0, keyword_only_chunks=skipped, stopped=reason, search="keyword_only")
        except repo.WorkspaceGone:
            logger.info("upload embed: workspace gone before the job could be degraded reason=%s", reason)

    # ------------------------------------------------------------ cancel and resume

    def cancel(self, workspace_id: str, job_id: str) -> bool:
        """Asks the job to stop at its next chunk; True while the job is queued or running (idempotent), False when unknown."""
        with self._lock:
            flag = self._cancelled.get((workspace_id, job_id))
        if flag is None:
            return False
        flag.set()
        return True

    def resume_pending(self) -> int:
        """Requeues every current version that still has pending chunks (a restart). Not counted against the day's
        embedding jobs or the client rule: those were taken when the job first started."""
        from . import repo

        rows = repo.resumable_embeds(self._driver)
        for row in rows:
            job_id = row.get("job_id") or f"{RESUME_JOB_PREFIX}{row['document_id']}-{row['version']}"
            self.submit(EmbedTask(workspace_id=row["workspace_id"], job_id=job_id, document_id=row["document_id"],
                                  version=row["version"], resumed=True))
        return len(rows)

    # ------------------------------------------------------------ the worker

    def pending_count(self) -> int:
        return self._fifo.qsize()

    def run_next(self) -> bool:
        """Runs the oldest queued task on the calling thread; False when none is queued."""
        try:
            task = self._fifo.get_nowait()
        except queue.Empty:
            return False
        self._run_registered(task)
        return True

    def _run_registered(self, task: EmbedTask) -> None:
        try:
            self.run_task(task)
        except Exception:  # noqa: BLE001 - nothing may kill the worker thread
            logger.exception("upload embed task crashed")
        finally:
            self._release(task)

    def start(self, *, resume: bool = False) -> None:
        self._thread = threading.Thread(target=self._loop, args=(resume,), name="upload-embed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = STOP_JOIN_TIMEOUT_S) -> None:
        self.stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self, resume: bool) -> None:
        self._lower_priority()
        if resume:
            try:
                logger.info("upload embed: requeued %d pending version(s) after the start", self.resume_pending())
            except Exception:  # noqa: BLE001 - a failed resume leaves the chunks pending for the next start
                logger.exception("upload embed: resume failed")
        while not self.stop_requested.is_set():
            try:
                task = self._fifo.get(timeout=PAUSE_POLL_S)
            except queue.Empty:
                continue
            self._run_registered(task)

    # ------------------------------------------------------------ one task

    def run_task(self, task: EmbedTask) -> None:
        """Embeds the pending chunks of ``task`` and ends with one ``ready`` event, whatever stopped it (except a shutdown or a
        deleted workspace, which write nothing more)."""
        from . import repo

        with self._lock:
            flag = self._cancelled.setdefault((task.workspace_id, task.job_id), threading.Event())
        run = _Run(total=0)
        try:
            self._embed(task, flag, run)
        except repo.WorkspaceGone:
            logger.info("upload embed: workspace deleted mid-job; nothing more is written")
            return
        except _Shutdown:
            logger.info("upload embed: shutdown; the pending chunks resume at the next start")
            return
        except _Superseded:
            pass                      # run.stopped is already "superseded"
        except Exception as exc:  # noqa: BLE001 - an embedding fault is keyword-only, never a failed upload
            logger.warning("upload embed stopped by an error exc_type=%s", type(exc).__name__)
            run.stopped = "embed_error"
        self._finish(task, run)

    def _finish(self, task: EmbedTask, run: _Run) -> None:
        from . import repo

        self._log_stages(task.workspace_id, task.job_id, run.stages)
        try:
            skipped = repo.finish_embedding(self._driver, task.workspace_id, task.document_id, task.version)
            self._emit(task, "ready", embedded_chunks=run.done, keyword_only_chunks=skipped, stopped=run.stopped,
                       search="keyword_semantic" if run.done else "keyword_only")
        except repo.WorkspaceGone:
            logger.info("upload embed: workspace deleted before the job could end")

    def _embed(self, task: EmbedTask, cancelled: threading.Event, run: _Run) -> None:
        from . import repo

        chunks = repo.pending_chunks(self._driver, task.workspace_id, task.document_id, task.version)
        run.total = len(chunks)
        run.stages.update(fresh=len(chunks), tokens=sum(c["tokens"] or 0 for c in chunks), done=0)
        self._emit(task, "embedding", progress={"done": 0, "total": run.total, "eta_s": None})
        workspace_tokens = repo.quota(self._driver, task.workspace_id).get("embedded_tokens", 0)
        started, paused = self._clock(), 0.0
        self._record_cpu(run.stages, "start")
        try:
            for chunk in chunks:
                paused += self._pause_while_parsing()
                elapsed = self._clock() - started - paused
                run.stopped = self._stop_reason(chunk, run, cancelled, elapsed, workspace_tokens)
                if run.stopped:
                    return
                paused += self._embed_one(task, chunk, run)
                self._progress(task, run, self._clock() - started - paused)
        finally:
            run.stages["embed_s"] = self._clock() - started
            self._record_cpu(run.stages, "end")

    def _stop_reason(self, chunk: dict, run: _Run, cancelled: threading.Event, elapsed: float,
                     workspace_tokens: int) -> str | None:
        if self.stop_requested.is_set():
            raise _Shutdown
        tokens = chunk["tokens"] or 0
        if cancelled.is_set():
            return "cancelled"
        if elapsed > self._settings.upload_embed_timeout_s:
            return "timeout"
        if workspace_tokens + run.embedded_tokens + tokens > self._settings.upload_max_workspace_tokens:
            return "budget"
        if (chunk["priority"] if chunk["priority"] is not None else NON_PRIORITY) >= NON_PRIORITY and \
                run.embedded_tokens + tokens > self._settings.upload_guaranteed_embed_tokens:
            return "guaranteed"
        return None

    def _encode(self, text: str) -> tuple[list[float], float]:
        """One encode under the gate's encode lock, started only while no parse runs; returns the vector and the paused time."""
        waited = 0.0
        while True:
            with self.parse_gate.encoding():
                if not self.parse_gate.busy:
                    return [float(x) for x in self._embedder.encode_passages([text])[0]], waited
            waited += self._pause_while_parsing()
            if self.stop_requested.is_set():
                raise _Shutdown

    def _embed_one(self, task: EmbedTask, chunk: dict, run: _Run) -> float:
        """Embeds and stores one chunk; returns the time spent paused for a parse (not counted against the time limit)."""
        from . import repo

        vector, waited = self._encode(chunk["text"])
        if not repo.set_chunk_embedding(self._driver, task.workspace_id, chunk["chunk_id"], vector, chunk["tokens"] or 0):
            run.stopped = "superseded"
            raise _Superseded
        run.done += 1
        run.embedded_tokens += chunk["tokens"] or 0
        run.stages["done"] = run.done
        return waited

    def _progress(self, task: EmbedTask, run: _Run, active_s: float) -> None:
        remaining = run.total - run.done
        eta = round(active_s / run.done * remaining, 1) if run.done else None
        self._emit(task, "embedding", progress={"done": run.done, "total": run.total, "eta_s": eta})

    def _pause_while_parsing(self) -> float:
        waited = 0.0
        while self.parse_gate.busy and not self.stop_requested.is_set():
            before = self._clock()
            self._sleep(PAUSE_POLL_S)
            waited += self._clock() - before
        return waited


class _Shutdown(Exception):
    """The process is stopping: the pending chunks stay pending."""


class _Superseded(Exception):
    """A chunk was no longer pending when its vector arrived (replaced, cancelled elsewhere or deleted)."""
