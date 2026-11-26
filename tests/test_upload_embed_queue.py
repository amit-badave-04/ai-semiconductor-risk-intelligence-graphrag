"""I4a / R16, OD11, OD12: the single FIFO embedding worker (src/semigraph/uploads/embed_queue.py).

Doubles only: a fake clock, a fake embedder that advances it, a fake repo, a recorded emitter. No thread is needed except in the
FIFO test, no database, no network, no sleeping.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from semigraph.uploads import embed_queue as eq
from semigraph.uploads import repo
from semigraph.uploads.sections import NON_PRIORITY

WS, DOC = "w" * 32, "0123456789ab"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Embedder:
    def __init__(self, clock: Clock, seconds: float = 1.0, fail_on: int | None = None) -> None:
        self.clock, self.seconds, self.fail_on, self.calls = clock, seconds, fail_on, 0

    def encode_passages(self, texts):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("native failure")
        self.clock.now += self.seconds
        return [[0.1, 0.2] for _ in texts]


def pending(*specs):
    """``(tokens, priority)`` per chunk, in the order the repo returns them (priority, then seq)."""
    return [{"chunk_id": f"c{i}", "seq": i, "text": f"text {i}", "tokens": t, "priority": p} for i, (t, p) in enumerate(specs)]


class World:
    def __init__(self, monkeypatch, chunks, *, workspace_tokens=0, ceiling=3600, guaranteed=100, budget=10_000, embedder_s=1.0,
                 fail_on=None, set_result=True):
        self.clock = Clock()
        self.events, self.written, self.finished, self.logged = [], [], [], []
        self.settings = SimpleNamespace(upload_embed_timeout_s=ceiling, upload_guaranteed_embed_tokens=guaranteed,
                                        upload_max_workspace_tokens=budget, upload_embed_jobs_per_day=23)
        self.embedder = Embedder(self.clock, embedder_s, fail_on)
        self.set_result, self.chunk_count = set_result, len(chunks)
        monkeypatch.setattr(repo, "pending_chunks", lambda d, ws, doc, v: list(chunks))
        monkeypatch.setattr(repo, "quota", lambda d, ws: {"embedded_tokens": workspace_tokens})
        monkeypatch.setattr(repo, "set_chunk_embedding", self._set)
        monkeypatch.setattr(repo, "finish_embedding", self._finish)
        self.sleeps = []
        self.queue = eq.EmbedQueue(object(), self.embedder, self.settings, emit=self._emit, clock=self.clock,
                                   sleep=self._sleep, reserve_day=self._reserve, log_stages=lambda *a: self.logged.append(a))
        self.reserved = 0

    def _set(self, driver, ws, chunk_id, vec, tokens):
        if isinstance(self.set_result, Exception):
            raise self.set_result
        self.written.append((chunk_id, tokens))
        return self.set_result

    def _finish(self, driver, ws, doc, version):
        self.finished.append((ws, doc, version))
        return self.chunk_count - len(self.written)

    def _emit(self, task, state, **extra):
        self.events.append((state, extra))

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.now += seconds
        if len(self.sleeps) >= 2:
            self.queue.parse_gate.leave()

    def _reserve(self, day):
        self.reserved += 1
        return self.reserved <= 23

    def task(self, **kw):
        return eq.EmbedTask(workspace_id=WS, job_id="job1", document_id=DOC, version=1, **kw)

    def last(self):
        return self.events[-1]


def test_chunks_are_embedded_in_the_order_the_repo_returns_them_and_the_job_ends_ready_keyword_semantic(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (20, 0), (30, NON_PRIORITY)))
    w.queue.run_task(w.task())
    assert [c for c, _ in w.written] == ["c0", "c1", "c2"]
    state, extra = w.last()
    assert state == "ready" and extra["embedded_chunks"] == 3 and extra["keyword_only_chunks"] == 0
    assert extra["search"] == "keyword_semantic" and extra["stopped"] is None
    assert w.finished == [(WS, DOC, 1)]


def test_progress_events_carry_exactly_done_total_and_eta(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0)))
    w.queue.run_task(w.task())
    progress = [e["progress"] for s, e in w.events if s == "embedding"]
    assert progress[0] == {"done": 0, "total": 2, "eta_s": None}
    assert all(set(p) == {"done", "total", "eta_s"} for p in progress) and progress[-1]["done"] == 2


def test_a_time_out_leaves_the_unfinished_chunks_keyword_only_and_is_not_a_failure(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0), (10, 0), (10, 0)), ceiling=2.5, guaranteed=10_000, embedder_s=1.0)
    w.queue.run_task(w.task())
    state, extra = w.last()
    assert state == "ready" and extra["stopped"] == "timeout"
    assert (extra["embedded_chunks"], extra["keyword_only_chunks"]) == (3, 1)
    assert w.finished and not any(s == "failed" for s, _ in w.events)


def test_the_workspace_token_budget_degrades_to_keyword_only_instead_of_failing(monkeypatch):
    w = World(monkeypatch, pending((30, 0), (30, 0), (30, 0)), workspace_tokens=40, budget=100, guaranteed=10_000)
    w.queue.run_task(w.task())
    state, extra = w.last()
    assert state == "ready" and extra["stopped"] == "budget" and extra["embedded_chunks"] == 2


def test_non_priority_chunks_stop_at_the_guaranteed_tokens_while_priority_chunks_continue(monkeypatch):
    w = World(monkeypatch, pending((60, 0), (60, 0), (60, NON_PRIORITY), (60, NON_PRIORITY)), guaranteed=100)
    w.queue.run_task(w.task())
    assert [c for c, _ in w.written] == ["c0", "c1"]            # priority chunks pass 100 tokens; the first non-priority does not
    _, extra = w.last()
    assert extra["stopped"] == "guaranteed" and extra["keyword_only_chunks"] == 2


def test_the_first_non_priority_chunk_inside_the_guaranteed_tokens_is_embedded(monkeypatch):
    w = World(monkeypatch, pending((30, NON_PRIORITY), (30, NON_PRIORITY), (60, NON_PRIORITY)), guaranteed=100)
    w.queue.run_task(w.task())
    assert [c for c, _ in w.written] == ["c0", "c1"]


def test_a_cancelled_job_leaves_a_valid_keyword_only_document(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0), (10, 0)))
    original = w.embedder.encode_passages

    def cancel_after_first(texts):
        out = original(texts)
        w.queue.cancel(WS, "job1")
        return out

    w.embedder.encode_passages = cancel_after_first
    w.queue.run_task(w.task())
    state, extra = w.last()
    assert state == "ready" and extra["stopped"] == "cancelled" and extra["embedded_chunks"] == 1
    assert w.finished, "what is left is marked keyword-only so a restart does not requeue it"


def test_cancel_is_idempotent_and_false_for_an_unknown_job(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    assert w.queue.cancel(WS, "nope") is False
    w.queue.submit(w.task())
    assert w.queue.cancel(WS, "job1") is True and w.queue.cancel(WS, "job1") is True


def test_a_chunk_that_is_no_longer_pending_ends_the_job_as_superseded(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0)), set_result=False)
    w.queue.run_task(w.task())
    assert w.last()[1]["stopped"] == "superseded" and len(w.written) == 1


def test_a_deleted_workspace_ends_quietly_with_no_further_write(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0)), set_result=repo.WorkspaceGone("gone"))
    w.queue.run_task(w.task())
    assert w.finished == [] and not any(s == "ready" for s, _ in w.events)


def test_an_embedder_error_is_keyword_only_not_a_failed_upload(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0), (10, 0)), fail_on=2)
    w.queue.run_task(w.task())
    state, extra = w.last()
    assert state == "ready" and extra["stopped"] == "embed_error" and extra["embedded_chunks"] == 1


def test_a_shutdown_leaves_the_chunks_pending_for_the_restart(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0)))
    w.queue.stop_requested.set()
    w.queue.run_task(w.task())
    assert w.finished == [] and w.written == [] and not any(s == "ready" for s, _ in w.events)


def test_the_queue_pauses_while_an_upload_parses_and_that_time_does_not_count_against_the_ceiling(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (10, 0)), ceiling=5, guaranteed=10_000, embedder_s=1.0)
    w.queue.parse_gate.enter()
    w.queue.run_task(w.task())
    assert len(w.sleeps) == 2, "it waited until the parse ended"
    assert w.last()[1]["stopped"] is None and w.last()[1]["embedded_chunks"] == 2


def test_the_parse_gate_counts_nested_uploads():
    gate = eq.ParseGate()
    assert not gate.busy
    with gate:
        assert gate.busy
        with gate:
            assert gate.busy
        assert gate.busy
    assert not gate.busy
    gate.leave()
    assert not gate.busy, "an extra leave never goes below zero"


def test_a_parse_waits_for_the_encode_that_is_running(monkeypatch):
    """MEDIUM 3: the parse child must never start beside a running encode (the 1 GiB child plus the model peak)."""
    gate = eq.ParseGate()
    entered = threading.Event()
    with gate.encoding():
        t = threading.Thread(target=lambda: (gate.enter(), entered.set()))
        t.start()
        assert not entered.wait(0.3), "enter() returned while an encode was still running"
        assert gate.busy, "the worker must see the parse at once, so it starts no further chunk"
    assert entered.wait(5)
    t.join()


def test_the_worker_does_not_start_an_encode_once_a_parse_has_entered(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    w.queue.parse_gate.enter()                      # the _sleep hook leaves again after two polls
    w.queue.run_task(w.task())
    assert w.sleeps and w.embedder.calls == 1 and not w.queue.parse_gate.busy


def test_embed_jobs_past_the_day_limit_are_keyword_only(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    outcomes = [w.queue.submit(eq.EmbedTask(workspace_id=f"w{i}", job_id=f"j{i}", document_id=DOC, version=1))
                for i in range(24)]
    assert [o.queued for o in outcomes] == [True] * 23 + [False]
    assert outcomes[-1].reason == "daily_limit"
    assert w.finished and w.events[-1][1]["search"] == "keyword_only" and w.events[-1][1]["stopped"] == "daily_limit"


def test_one_active_embedding_job_per_workspace_and_per_client(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    first = w.queue.submit(w.task(client_key="ip-a"))
    same_workspace = w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    same_client = w.queue.submit(eq.EmbedTask(workspace_id="w2" * 16, job_id="j3", document_id=DOC, version=1, client_key="ip-a"))
    other_client = w.queue.submit(eq.EmbedTask(workspace_id="w3" * 16, job_id="j4", document_id=DOC, version=1, client_key="ip-b"))
    assert [first.queued, same_workspace.queued, same_client.queued, other_client.queued] == [True, True, True, True]
    assert same_workspace.reason == same_client.reason == "held"
    assert w.queue.pending_count() == 2, "the held tasks wait outside the FIFO until the running job of their workspace/client ends"
    assert w.reserved == 2, "a held task is not charged to the day until it is admitted"


def test_a_held_task_is_admitted_and_charged_when_the_running_job_ends(monkeypatch):
    """MEDIUM 2: a second upload (or new version) of the same workspace waits, then gets its semantic search."""
    w = World(monkeypatch, pending((10, 0)))
    w.queue.submit(w.task())
    held = w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    assert held.queued and held.reason == "held" and w.queue.pending_count() == 1 and w.reserved == 1
    w.queue.run_next()
    assert w.queue.pending_count() == 1 and w.reserved == 2
    w.queue.run_next()
    assert [e[1]["search"] for e in w.events if e[0] == "ready"] == ["keyword_semantic", "keyword_semantic"]


def test_a_held_task_cancelled_before_admission_ends_keyword_only_without_a_day_charge(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    w.queue.submit(w.task())
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    assert w.queue.cancel(WS, "j2") is True
    w.queue.run_next()
    assert w.queue.pending_count() == 0 and w.reserved == 1
    assert w.events[-1][1]["stopped"] == "cancelled" and w.events[-1][1]["search"] == "keyword_only"


def test_a_held_task_past_the_day_limit_is_keyword_only_when_admitted(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    w.reserved = 22
    w.queue.submit(w.task())                                             # takes the 23rd
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    w.queue.run_next()
    assert w.queue.pending_count() == 0 and w.events[-1][1]["stopped"] == "daily_limit"


def test_a_finished_task_frees_its_workspace_and_client_for_the_next_one(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    w.queue.submit(w.task(client_key="ip-a"))
    w.queue.run_next()
    assert w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j9", document_id=DOC, version=1, client_key="ip-a")).queued


def _marks(w, monkeypatch, fail=False):
    marks = []

    def mark(driver, ws, doc, version, day):
        if fail:
            raise RuntimeError("db down")
        marks.append((ws, doc, version, day))

    monkeypatch.setattr(repo, "mark_embed_charged", mark)
    return marks


def test_resume_requeues_charged_versions_without_counting_the_day_or_the_client(monkeypatch):
    # updated for M2RT-01: the rows carry the charge marker; before, any pending version was assumed charged
    w = World(monkeypatch, pending((10, 0)))
    rows = [{"workspace_id": WS, "document_id": DOC, "version": 2, "job_id": "jobX", "embed_charged_day": "2026-11-26"},
            {"workspace_id": "z" * 32, "document_id": DOC, "version": 1, "job_id": None, "embed_charged_day": "2026-11-25"}]
    monkeypatch.setattr(repo, "resumable_embeds", lambda d: rows)
    assert w.queue.resume_pending() == 2
    assert w.reserved == 0
    w.queue.run_next()
    assert w.queue.pending_count() == 1


def test_resume_charges_an_unmarked_version_and_never_embeds_it_free_when_the_cap_is_full(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    _marks(w, monkeypatch)
    w.reserved = 23                                           # the day is full
    monkeypatch.setattr(repo, "resumable_embeds",
                        lambda d: [{"workspace_id": WS, "document_id": DOC, "version": 1, "job_id": "j", "embed_charged_day": None}])
    assert w.queue.resume_pending() == 1
    assert w.queue.pending_count() == 0 and w.written == [] and w.finished == [(WS, DOC, 1)]
    assert w.last()[1]["stopped"] == "daily_limit"


def test_resume_of_an_unmarked_version_is_charged_once_and_runs_when_the_cap_has_room(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    marks = _marks(w, monkeypatch)
    monkeypatch.setattr(repo, "resumable_embeds",
                        lambda d: [{"workspace_id": WS, "document_id": DOC, "version": 1, "job_id": "j"}])   # pre-marker row
    w.queue.resume_pending()
    assert w.reserved == 1 and len(marks) == 1 and w.queue.pending_count() == 1


def test_a_fault_on_one_resumed_row_does_not_stop_the_resume_of_the_others(monkeypatch):
    """An unmarked row on a full day degrades; if that write fails, the charged rows after it must still be requeued."""
    w = World(monkeypatch, pending((10, 0)))
    _marks(w, monkeypatch)
    w.reserved = 23

    def broken_finish(*args, **kwargs):
        raise RuntimeError("neo4j blip")

    monkeypatch.setattr(repo, "finish_embedding", broken_finish)
    rows = [{"workspace_id": WS, "document_id": DOC, "version": 1, "job_id": "a", "embed_charged_day": None},
            {"workspace_id": "z" * 32, "document_id": DOC, "version": 2, "job_id": "b", "embed_charged_day": "2026-11-26"}]
    monkeypatch.setattr(repo, "resumable_embeds", lambda d: rows)
    assert w.queue.resume_pending() == 2
    assert w.queue.pending_count() == 1                       # the charged row was still requeued


def test_a_charged_submit_records_the_day_and_a_refused_one_does_not(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    marks = _marks(w, monkeypatch)
    w.queue.submit(w.task())
    assert marks == [(WS, DOC, 1, w.queue._today())]
    w.reserved = 23
    marks.clear()
    w.queue.submit(eq.EmbedTask(workspace_id="y" * 32, job_id="j2", document_id=DOC, version=1))
    assert marks == []


def test_a_held_task_is_marked_charged_only_when_admitted(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    marks = _marks(w, monkeypatch)
    w.queue.submit(w.task())
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    assert [m[1] for m in marks] == [DOC]
    w.queue.run_next()
    assert [m[1] for m in marks] == [DOC, "other"]


def test_a_failed_marker_write_does_not_stop_the_task_or_crash_the_queue(monkeypatch, caplog):
    w = World(monkeypatch, pending((10, 0)))
    _marks(w, monkeypatch, fail=True)
    assert w.queue.submit(w.task()) == eq.Admission(True)
    assert w.queue.pending_count() == 1 and w.reserved == 1
    assert "recording the day's charge failed" in caplog.text


def test_the_worker_thread_runs_tasks_first_in_first_out(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    order, done = [], threading.Event()

    def record(task, state, **extra):
        if state == "ready":
            order.append(task.job_id)
            done.set() if len(order) == 3 else None

    w.queue._emit = record
    for i in range(3):
        w.queue.submit(eq.EmbedTask(workspace_id=f"w{i}" * 8, job_id=f"j{i}", document_id=DOC, version=1))
    w.queue.start()
    try:
        assert done.wait(5)
    finally:
        w.queue.stop()
    assert order == ["j0", "j1", "j2"]


def test_the_stage_line_gets_what_the_embed_reached(monkeypatch):
    w = World(monkeypatch, pending((10, 0), (20, 0)))
    w.queue.run_task(w.task())
    (ws, job_id, stages), = w.logged
    assert (ws, job_id) == (WS, "job1")
    assert stages["fresh"] == 2 and stages["done"] == 2 and stages["tokens"] == 30 and "embed_s" in stages


@pytest.mark.parametrize("reason", ["timeout", "budget", "guaranteed", "cancelled", "superseded", "embed_error", "daily_limit"])
def test_every_stop_reason_is_a_known_constant(reason):
    assert reason in eq.STOP_REASONS


# ---------------------------------------------------------------- security review S-01, S-04


def test_s01_an_error_while_admitting_a_held_task_does_not_escape_the_worker_loop(monkeypatch, caplog):
    """S-01: _release -> _admit_held -> _degrade -> finish_embedding raising must be logged, never kill the worker thread."""
    w = World(monkeypatch, pending((10, 0)))
    w.queue.submit(w.task())
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    w.queue.cancel(WS, "j2")                                 # admission will degrade it -> finish_embedding
    real_finish = w._finish
    calls = []

    def finish(driver, ws, doc, version):
        calls.append(doc)
        if doc == "other":
            raise RuntimeError("db down SECRET-UPLOAD-TEXT")
        return real_finish(driver, ws, doc, version)

    monkeypatch.setattr(repo, "finish_embedding", finish)
    with caplog.at_level("ERROR", logger="semigraph.uploads.embed_queue"):
        w.queue._run_registered(w.queue._fifo.get_nowait())  # must not raise
    assert "other" in calls
    assert any("admit" in r.getMessage() for r in caplog.records)
    # the loop keeps going: a later task still runs
    w.queue.submit(eq.EmbedTask(workspace_id="z" * 32, job_id="j3", document_id=DOC, version=1))
    assert w.queue.run_next() is True


def test_s04_the_day_reservation_is_taken_without_the_queue_lock_held(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    held_during = []

    def reserve(day):
        held_during.append(w.queue._lock.locked())
        return True

    w.queue._reserve_day = reserve
    w.queue.submit(w.task())
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))   # held
    w.queue.run_next()                                                                            # admits j2 -> reserve
    assert len(held_during) == 2 and not any(held_during)


def test_s04_a_refused_reservation_frees_the_workspace_again(monkeypatch):
    w = World(monkeypatch, pending((10, 0)))
    w.queue._reserve_day = lambda day: False
    assert w.queue.submit(w.task()).reason == "daily_limit"
    w.queue._reserve_day = lambda day: True
    assert w.queue.submit(w.task()).reason is None            # the refused task left no active registration


def test_s01_one_held_task_failing_to_degrade_does_not_strand_the_other_held_tasks(monkeypatch, caplog):
    """Verify follow-up: the error is caught per held task, so the next admissible held task is still admitted."""
    w = World(monkeypatch, pending((10, 0)))
    w.queue.submit(w.task())
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j2", document_id="other", version=1))
    w.queue.submit(eq.EmbedTask(workspace_id=WS, job_id="j3", document_id="third", version=1))
    w.queue.cancel(WS, "j2")                                 # j2 degrades -> finish_embedding raises
    real_finish = w._finish

    def finish(driver, ws, doc, version):
        if doc == "other":
            raise RuntimeError("db down")
        return real_finish(driver, ws, doc, version)

    monkeypatch.setattr(repo, "finish_embedding", finish)
    with caplog.at_level("ERROR", logger="semigraph.uploads.embed_queue"):
        w.queue._run_registered(w.queue._fifo.get_nowait())
    assert not w.queue._held                                   # j3 was not stranded behind j2's failure
    assert w.queue._fifo.get_nowait().job_id == "j3"
