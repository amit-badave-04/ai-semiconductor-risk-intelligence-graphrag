"""I4a / R16: a restart requeues the chunks that are still pending instead of failing the upload.

The process dies after ``keyword_ready`` (the queue is never drained); a NEW queue on a new app, as after a deploy, finds the
pending chunks through ``repo.resumable_embeds`` and finishes them. The fake repo outlives the "restart", as the database does.
"""

from __future__ import annotations

import functools

from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture
from test_serve_upload_embed_timeout import (  # noqa: F401 - the harness and its autouse fixtures
    DEFAULT_CEILING,
    DOC,
    ORIGINAL_PROCESS,
    WS,
    CountingSlot,
    FakeApp,
    JobSettings,
    World,
    chunk_texts,
    clean_environment,
    no_cgroup_file,
    steps,
)

from semigraph.uploads import jobs, repo


def test_embed_job_resumes_after_restart(monkeypatch, fresh_drain):  # noqa: F811
    world = World(monkeypatch)
    world.document(chunk_texts(6, 5))
    embedder = steps(world.clock, 1.0)
    world.app = FakeApp(embedder, JobSettings(DEFAULT_CEILING), CountingSlot())
    monkeypatch.setattr(jobs, "_process", functools.partial(ORIGINAL_PROCESS, clock=world.clock))
    jobs.registry(world.app).create(WS, "job1")
    world.app.state.upload_slots.acquire(blocking=False)
    jobs._worker(world.app, WS, "job1", DOC, "t.txt", [b"raw"], "txt", "h" * 64)
    assert embedder.calls == 0 and len(world.pending.pending_chunks(None, WS, DOC, 1)) == 6, "keyword-ready, nothing embedded yet"

    # ---- the restart: a new app and queue, the same database
    monkeypatch.setattr(repo, "resumable_embeds",
                        lambda driver: [{"workspace_id": WS, "document_id": DOC, "version": 1, "job_id": "job1",
                                           "embed_charged_day": "2026-11-26"}])
    restarted = FakeApp(embedder, JobSettings(DEFAULT_CEILING), CountingSlot())
    queue = jobs.embed_queue_for(restarted)
    queue._clock = world.clock
    assert queue.resume_pending() == 1
    while queue.run_next():
        pass

    assert world.pending.pending_chunks(None, WS, DOC, 1) == []
    assert embedder.calls == 6 and world.pending.finished == [(DOC, 1)]
    assert world.persisted()[-1]["state"] == "ready" and world.persisted()[-1]["embedded_chunks"] == 6


def test_nothing_pending_resumes_nothing(monkeypatch, fresh_drain):  # noqa: F811
    world = World(monkeypatch)
    monkeypatch.setattr(repo, "resumable_embeds", lambda driver: [])
    app = FakeApp(steps(world.clock, 1.0), JobSettings(DEFAULT_CEILING), CountingSlot())
    assert jobs.embed_queue_for(app).resume_pending() == 0


def test_a_cancelled_then_restarted_document_is_not_requeued(monkeypatch, fresh_drain):  # noqa: F811
    """A cancel (or a time-out) marks the rest keyword-only (``finish_embedding``), so ``resumable_embeds`` no longer lists it."""
    world = World(monkeypatch)
    world.document(chunk_texts(4, 5))
    world.run(steps(world.clock, 1050), ceiling=2100)                       # times out after 3 of 4 chunks
    assert world.pending.pending_chunks(None, WS, DOC, 1) == [] and world.pending.finished == [(DOC, 1)]
