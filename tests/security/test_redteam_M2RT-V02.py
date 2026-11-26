"""Red-team PoC M2RT-V02 (variant of M2RT-01): a refused embedding task whose degrade write fails is embedded uncharged later.

``EmbedQueue.submit`` refuses a task when the day's embedding-job cap is full (``_refusal`` -> "daily_limit") and calls
``_degrade``, which marks the version's chunks 'skipped' via ``repo.finish_embedding``. ``_degrade`` only catches
``repo.WorkspaceGone``: a Neo4j fault there (``run_cypher`` is an auto-commit ``session.run``, no managed retry) escapes
``submit`` and is swallowed by ``jobs._hand_off_to_embedding``, which emits ``ready / keyword_only / embed_error`` but never
retries the 'skipped' write. The chunks written by ``put_version`` stay 'pending', so after the next restart ``resume_pending``
submits the version with ``resumed=True`` (no ``_refusal``) and it is embedded although the day refused it.

The trigger is a database fault at that moment (not attacker-controlled) followed by a restart (deploy / OOM / crash).
Doubles only: a fake repo and day counter that outlive the "restart", a fake embedder, a one-shot DB fault. No network, no DB.
Expected today: FAILS (the refused version ends up embedded).
"""

from __future__ import annotations

from types import SimpleNamespace

from neo4j.exceptions import TransientError

from semigraph.uploads import embed_queue as eq
from semigraph.uploads import jobs, repo

WS = "w" * 32
DAY = "2026-11-26"


class DayCounter:
    """Stands in for ``store.reserve_daily_upload`` on the persistent ``SvcUploadDay`` node: survives the restart."""

    def __init__(self, limit: int, taken: int) -> None:
        self.limit, self.n = limit, taken

    def reserve(self, day: str) -> bool:
        if self.n >= self.limit:
            return False
        self.n += 1
        return True


class FakeRepo:
    """Pending chunks per (document, version); survives the restart like the database. ``finish_embedding`` fails once."""

    def __init__(self, docs: list[str]) -> None:
        self.pending = {(d, 1): [{"chunk_id": f"{d}-c{i}", "seq": i, "text": f"t{i}", "tokens": 10, "priority": 0}
                                 for i in range(3)] for d in docs}
        self.embedded_docs: set[str] = set()
        self.fail_finish_once = True

    def pending_chunks(self, driver, ws, doc, version):
        return list(self.pending.get((doc, version), []))

    def set_chunk_embedding(self, driver, ws, chunk_id, vec, tokens):
        doc = chunk_id.split("-c")[0]
        self.pending[(doc, 1)] = [c for c in self.pending[(doc, 1)] if c["chunk_id"] != chunk_id]
        self.embedded_docs.add(doc)
        return True

    def finish_embedding(self, driver, ws, doc, version):
        if self.fail_finish_once:
            self.fail_finish_once = False
            raise TransientError("simulated Neo4j fault while marking chunks skipped")
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
    return eq.EmbedQueue(object(), Embedder(), settings,
                         emit=lambda task, state, **kw: events.append((task.document_id, state, kw)),
                         reserve_day=counter.reserve, today=lambda: DAY)


def test_m2rtv02_refused_task_with_failed_degrade_is_not_embedded_after_restart(monkeypatch):
    fake = FakeRepo(["docA"])
    for name in ("pending_chunks", "set_chunk_embedding", "finish_embedding", "resumable_embeds"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    monkeypatch.setattr(repo, "quota", lambda driver, ws: {"embedded_tokens": 0})
    counter = DayCounter(limit=1, taken=1)              # the day's embedding-job cap is already full

    # ---- the upload: keyword-ready write done (docA chunks 'pending'), then the hand-off to the queue
    job_events: list = []
    events: list = []
    first = _queue(counter, events)
    task = eq.EmbedTask(workspace_id=WS, job_id="job-docA", document_id="docA", version=1, client_key="ip")
    jobs._hand_off_to_embedding(lambda state, **kw: job_events.append((state, kw)), first.submit, task,
                                summary={}, pending=3, has_vectors=False)
    assert counter.n == 1, "precondition: the day was not charged for docA (refused: cap full)"
    assert job_events and job_events[-1][0] == "ready" and job_events[-1][1]["stopped"] == "embed_error", \
        "precondition: the hand-off swallowed the degrade fault and told the client keyword-only"
    assert fake.pending[("docA", 1)], "precondition: docA's chunks are still 'pending' (never marked skipped)"

    # ---- a later restart: a new queue, the same database and the same (full) day counter
    second = _queue(counter, events)
    assert second.resume_pending() == 1
    while second.run_next():
        pass

    # Invariant (OD11): a version the day refused stays keyword-only; it is never embedded without a charge.
    assert "docA" not in fake.embedded_docs, (
        f"docA was refused by the full day cap (limit {counter.limit}, charged {counter.n}) but was embedded after the "
        "restart: the failed degrade left its chunks 'pending' and resume_pending skipped the day check"
    )
