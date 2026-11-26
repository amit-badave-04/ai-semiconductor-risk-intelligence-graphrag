"""Red-team PoC M2RT-01: a held (never-charged) embedding task is embedded for free after a restart.

``EmbedQueue.submit`` holds a task whose workspace or client already has a running job and does NOT charge the day for it
(``_refusal`` runs only on admission). Its chunks are already stored ``embed_state='pending'``. If the process restarts before
the held task is admitted, ``resume_pending`` lists that version and submits it with ``resumed=True``, which skips
``_refusal``: the version is embedded although the day's embedding-job cap (OD11, ``upload_embed_jobs_per_day``) was never
charged for it, even when that cap is already full.

Doubles only: a fake repo and a fake day counter that outlive the "restart" (as Neo4j does), a fake embedder. No network, no DB.
Expected today: FAILS (two jobs embed while the day counter, limit 1, was charged once).
"""

from __future__ import annotations

from types import SimpleNamespace

from semigraph.uploads import embed_queue as eq
from semigraph.uploads import repo

WS = "w" * 32
DAY = "2026-11-26"


class DayCounter:
    """Stands in for ``store.reserve_daily_upload`` on the persistent ``SvcUploadDay`` node: survives the restart."""

    def __init__(self, limit: int) -> None:
        self.limit, self.n = limit, 0

    def reserve(self, day: str) -> bool:
        if self.n >= self.limit:
            return False
        self.n += 1
        return True


class FakeRepo:
    """Pending chunks per (document, version); survives the restart like the database."""

    def __init__(self, docs: list[str]) -> None:
        self.pending = {(d, 1): [{"chunk_id": f"{d}-c{i}", "seq": i, "text": f"t{i}", "tokens": 10, "priority": 0}
                                 for i in range(3)] for d in docs}
        self.embedded_docs: set[str] = set()

    def pending_chunks(self, driver, ws, doc, version):
        return list(self.pending.get((doc, version), []))

    def set_chunk_embedding(self, driver, ws, chunk_id, vec, tokens):
        doc = chunk_id.split("-c")[0]
        self.pending[(doc, 1)] = [c for c in self.pending[(doc, 1)] if c["chunk_id"] != chunk_id]
        self.embedded_docs.add(doc)
        return True

    def finish_embedding(self, driver, ws, doc, version):
        skipped = len(self.pending.get((doc, version), []))
        self.pending[(doc, version)] = []
        return skipped

    def resumable_embeds(self, driver):
        return [{"workspace_id": WS, "document_id": d, "version": v, "job_id": f"job-{d}"}
                for (d, v), chunks in self.pending.items() if chunks]


class Embedder:
    def encode_passages(self, texts):
        return [[0.1, 0.2] for _ in texts]


def _queue(counter: DayCounter, events: list) -> eq.EmbedQueue:
    settings = SimpleNamespace(upload_embed_timeout_s=3600, upload_guaranteed_embed_tokens=19_900,
                               upload_max_workspace_tokens=48_000, upload_embed_jobs_per_day=counter.limit)
    return eq.EmbedQueue(object(), Embedder(), settings, emit=lambda task, state, **kw: events.append((task.document_id, state, kw)),
                         reserve_day=counter.reserve, today=lambda: DAY)


def test_m2rt01_held_task_resumed_after_restart_is_not_embedded_past_the_day_cap(monkeypatch):
    fake = FakeRepo(["docA", "docB"])
    for name in ("pending_chunks", "set_chunk_embedding", "finish_embedding", "resumable_embeds"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    monkeypatch.setattr(repo, "quota", lambda driver, ws: {"embedded_tokens": 0})
    counter = DayCounter(limit=1)                      # the day's embedding-job cap: one job, already about to be taken

    # ---- before the restart: A is admitted (takes the day's only job), B (same workspace) is held and never charged
    events: list = []
    first = _queue(counter, events)
    a = first.submit(eq.EmbedTask(workspace_id=WS, job_id="job-docA", document_id="docA", version=1, client_key="ip"))
    b = first.submit(eq.EmbedTask(workspace_id=WS, job_id="job-docB", document_id="docB", version=1, client_key="ip"))
    assert a == eq.Admission(True) and b == eq.Admission(True, eq.HELD)
    assert counter.n == 1, "precondition: only A was charged; B is held and uncharged"
    # The process dies here (deploy / OOM / crash) before A ends and B is admitted; both versions keep pending chunks.

    # ---- the restart: a new queue, the same database and the same day counter (already full)
    second = _queue(counter, events)
    assert second.resume_pending() == 2
    while second.run_next():
        pass

    # Invariant (OD11 / embed_queue docstring "those were taken when the job first started"): every version that gets
    # vectors on this day was charged one of the day's embedding jobs. B never was, and the cap was already full.
    assert len(fake.embedded_docs) <= counter.n, (
        f"{len(fake.embedded_docs)} versions embedded ({sorted(fake.embedded_docs)}) but the day was charged only "
        f"{counter.n} time(s) with a cap of {counter.limit}: the held, never-charged task bypassed the daily embed-job cap"
    )
