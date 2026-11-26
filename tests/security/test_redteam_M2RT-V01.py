"""Red-team PoC M2RT-V01 (variant of M2RT-01): a REFUSED held embedding task is embedded anyway after a restart.

A held task is admitted in ``EmbedQueue._admit_held`` once its workspace is free. The day's embedding-job cap refuses it
(``_refusal`` -> ``daily_limit``: the day is full, or the day counter itself fails and fails closed). ``_degrade`` then calls
``repo.finish_embedding`` to mark its chunks keyword-only; when that write fails with a non-``WorkspaceGone`` error (a transient
Neo4j fault, the same outage that can make the counter fail) the ``except Exception`` in ``_admit_held`` logs "its chunks stay
keyword-only", but they are still ``embed_state='pending'``. At the next start ``resume_pending`` lists the version and submits
it with ``resumed=True``, which skips ``_refusal``: a version the day cap explicitly refused gets vectors past the cap.

Doubles only: a fake repo and a fake day counter that outlive the "restart" (as Neo4j does), a fake embedder. No network, no DB.
Expected today: FAILS for both parametrized cases (docB is embedded after the restart).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from semigraph.uploads import embed_queue as eq
from semigraph.uploads import repo

WS = "w" * 32
DAY = "2026-11-26"


class TransientDbError(Exception):
    """Stands in for a neo4j ServiceUnavailable / TransientError: not ``repo.WorkspaceGone``."""


class DayCounter:
    """Stands in for ``store.reserve_daily_upload`` on the persistent ``SvcUploadDay`` node: survives the restart."""

    def __init__(self, limit: int) -> None:
        self.limit, self.n, self.down = limit, 0, False

    def reserve(self, day: str) -> bool:
        if self.down:
            raise TransientDbError("counter unavailable")
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
        self.finish_fails_for: set[str] = set()

    def pending_chunks(self, driver, ws, doc, version):
        return list(self.pending.get((doc, version), []))

    def set_chunk_embedding(self, driver, ws, chunk_id, vec, tokens):
        doc = chunk_id.split("-c")[0]
        self.pending[(doc, 1)] = [c for c in self.pending[(doc, 1)] if c["chunk_id"] != chunk_id]
        self.embedded_docs.add(doc)
        return True

    def finish_embedding(self, driver, ws, doc, version):
        if doc in self.finish_fails_for:
            raise TransientDbError("finish_embedding write failed")
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


@pytest.mark.parametrize("refusal", ["day_full", "counter_down"])
def test_m2rtv01_refused_held_task_whose_degrade_failed_is_not_embedded_after_restart(monkeypatch, refusal):
    fake = FakeRepo(["docA", "docB"])
    for name in ("pending_chunks", "set_chunk_embedding", "finish_embedding", "resumable_embeds"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    monkeypatch.setattr(repo, "quota", lambda driver, ws: {"embedded_tokens": 0})
    counter = DayCounter(limit=1)

    # ---- before the restart: A takes the day's only job; B (same workspace) is held
    events: list = []
    first = _queue(counter, events)
    a = first.submit(eq.EmbedTask(workspace_id=WS, job_id="job-docA", document_id="docA", version=1, client_key="ip"))
    b = first.submit(eq.EmbedTask(workspace_id=WS, job_id="job-docB", document_id="docB", version=1, client_key="ip"))
    assert a == eq.Admission(True) and b == eq.Admission(True, eq.HELD)

    # A runs to the end; when it is released B is admitted and REFUSED by the day cap (full, or the counter fails closed),
    # and B's keyword-only write fails with a transient DB error.
    if refusal == "counter_down":
        counter.down = True
    fake.finish_fails_for.add("docB")
    assert first.run_next() is True
    assert "docB" not in fake.embedded_docs and first.pending_count() == 0, "precondition: B was refused, not queued"
    assert fake.pending[("docB", 1)], "precondition: B's chunks are still 'pending' after the failed degrade"

    # ---- the restart: the DB is healthy again; a new queue, the same database and the same day counter
    counter.down = False
    fake.finish_fails_for.clear()
    second = _queue(counter, events)
    second.resume_pending()
    while second.run_next():
        pass

    # Invariant (OD11, and the log line "its chunks stay keyword-only"): a version the day cap refused gets no vectors.
    assert "docB" not in fake.embedded_docs, (
        f"docB was refused by the day cap ({refusal}) but embedded after the restart; day charged {counter.n} time(s) "
        f"with a cap of {counter.limit}: the refused task bypassed the daily embed-job cap via resume_pending"
    )
