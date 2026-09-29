"""Upload job state machine and the TTL sweeper (M4 Worker C, docs/v2/M4_PLAN.md 4.2, 5, 14.1, 14.6).

Neo4j (``uploads.repo``) and the document parser (``uploads.parse``) are faked; ``uploads.units`` and
``uploads.changes`` run for real (pure, cheap, no I/O) except where a test needs exact control over chunk
boundaries, in which case ``chunk_units`` is monkeypatched. ``_worker`` (the thread body) is called directly and
synchronously so tests never race a background thread; one test exercises the public, threaded
:func:`run_upload_job` entry point end to end.
"""

from __future__ import annotations

import threading

import pytest

from semigraph.uploads import jobs, repo
from semigraph.uploads.parse import Block, ParsedDoc, ParseError
from semigraph.uploads.units import Chunk

WS = "b" * 32
DOC = "0123456789ab"


class FakeSettings:
    upload_parse_timeout_s = 5
    upload_max_pages = 30
    upload_max_tokens = 16000
    upload_max_chunk_tokens = 512
    upload_max_chunks = 120
    upload_max_workspace_tokens = 48000
    upload_max_workspace_pages = 120
    upload_embed_timeout_s = 1200
    uploads_enabled = True


class FakeEmbedder:
    name = "fake-embedder"

    def __init__(self, tokens_per_text=None):
        self.tokens_per_text = tokens_per_text or {}
        self.encode_calls: list[str] = []

    def count_tokens(self, text):
        return self.tokens_per_text.get(text, max(1, len(text) // 4))

    def encode_passages(self, texts):
        self.encode_calls.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeRepo:
    """Minimal in-memory stand-in for uploads.repo's Neo4j surface."""

    def __init__(self):
        self.versions: dict[str, list[dict]] = {}
        self.embedded: dict[str, dict[str, list[float]]] = {}
        self.workspace_tokens = 0
        self.workspace_pages = 0
        self.jobs: list[dict] = []
        self.put_version_calls: list[dict] = []
        self.put_job_fail_times = 0       # the next N put_job calls raise a transient error
        self.put_job_calls = 0
        self.workspace_gone = False       # once true, put_version/put_job raise WorkspaceGone

    def latest_version(self, driver, ws, document_id):
        rows = self.versions.get(document_id)
        return {"version": rows[-1]["version"]} if rows else None

    def version_view(self, driver, ws, document_id, version):
        for row in self.versions.get(document_id, []):
            if row["version"] == version:
                return row["view"]
        return None

    def embedded_chunks(self, driver, ws, document_id):
        return dict(self.embedded.get(document_id, {}))

    def quota(self, driver, ws):
        return {"embedded_tokens": self.workspace_tokens, "pages": self.workspace_pages}

    def put_version(self, driver, ws, **kw):
        if self.workspace_gone:
            raise WorkspaceGone(ws)
        self.put_version_calls.append(kw)
        rows = self.versions.setdefault(kw["document_id"], [])
        view = {"text": kw["text"], "units": kw["units"],
               "chunk_spans": [(c["chunk_id"], c["char_start"], c["char_end"]) for c in kw["chunks"]],
               "method": kw["method"], "chars_per_page": kw["chars_per_page"]}
        rows.append({"version": kw["version"], "view": view})
        bucket = self.embedded.setdefault(kw["document_id"], {})
        for c in kw["chunks"]:
            if c.get("embedded"):
                bucket[c["text_hash"]] = c["embedding"]
                self.workspace_tokens += c["tokens"]

    def put_job(self, driver, ws, job):
        self.put_job_calls += 1
        if self.workspace_gone and job.get("state") == "failed" and job["error"]["code"] == "workspace_deleted":
            # put_version already refused: a real repo.put_job guarded the same way would refuse too.
            raise WorkspaceGone(ws)
        if self.put_job_fail_times > 0:
            self.put_job_fail_times -= 1
            raise TransientRepoError("simulated transient Neo4j error")
        self.jobs.append(dict(job))

    def sweep_expired(self, driver, now):
        return 0


class WorkspaceGone(Exception):
    """Stands in for the future ``uploads.repo.WorkspaceGone`` (docs/v2/M4_PLAN.md 15.4) — not yet defined on the
    real ``repo`` module (out of this worker's file ownership), so it is injected here via ``monkeypatch`` with
    ``raising=False`` rather than imported."""


class TransientRepoError(Exception):
    """Stands in for a flaky Neo4j write (e.g. a driver ``TransientError``) that a retry should absorb."""


class FakeSlots:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class State:
    pass


class FakeApp:
    def __init__(self, fake_repo, embedder=None, settings=None):
        self.state = State()
        self.state.driver = object()
        self.state.embedder = embedder or FakeEmbedder()
        self.state.settings = settings or FakeSettings()
        self.state.upload_slots = FakeSlots()


@pytest.fixture
def fake_repo(monkeypatch):
    fr = FakeRepo()
    for name in ("latest_version", "version_view", "embedded_chunks", "quota", "put_version", "put_job",
                "sweep_expired"):
        monkeypatch.setattr(repo, name, getattr(fr, name))
    # WorkspaceGone does not exist on the real `repo` module yet (another worker's seam, docs/v2/M4_PLAN.md 15.4):
    # injected here so jobs.py's `getattr(repo, "WorkspaceGone", _NeverRaised)` resolves to something real in tests.
    monkeypatch.setattr(repo, "WorkspaceGone", WorkspaceGone, raising=False)
    return fr


def _block(text, page=1, size=10.0, bold=False, kind_hint="paragraph"):
    return Block(text=text, page=page, size=size, bold=bold, kind_hint=kind_hint)


def _parsed(blocks, pages=1, method="text", chars_per_page=1000.0):
    return ParsedDoc(method=method, pages=pages, blocks=tuple(blocks), chars_per_page=chars_per_page, warnings=())


def _run(app, *, document_id=DOC, title="doc.txt", data=b"raw bytes", kind="txt", content_hash_hex="h" * 64,
        job_id="job1"):
    reg = jobs.registry(app)
    reg.create(WS, job_id)
    jobs._worker(app, WS, job_id, document_id, title, data, kind, content_hash_hex)
    return reg


def _events(fake_repo, job_id="job1"):
    return [j for j in fake_repo.jobs if j["job_id"] == job_id]


# ---------------------------------------------------------------- happy path


def test_a_plain_text_upload_reaches_ready_and_releases_the_slot(fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    app = FakeApp(fake_repo)
    _run(app)
    events = _events(fake_repo)
    states = [e["state"] for e in events]
    collapsed = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]   # embedding repeats with progress
    assert collapsed == ["received", "validating", "parsing", "chunking", "embedding", "comparing", "indexing", "ready"]
    assert events[-1]["chunks"] >= 1 and events[-1]["units"] >= 1
    assert events[-1]["not_compared_reason"] == "first_version"
    assert events[-1]["items_compared"] is False
    assert app.state.upload_slots.released == 1
    # The registry entry is KEPT (not discarded) once the job ends — only marked terminal — so a watcher that
    # reconnects moments later still gets a live replay (finding 1/14/22, grace-period pruning).
    assert jobs.registry(app).is_terminal(WS, "job1") is True
    result = jobs.registry(app).events_from(WS, "job1", 0)
    assert result is not None and result[0][-1]["state"] == "ready"


def test_run_upload_job_starts_a_real_thread_and_reaches_a_terminal_state(fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    app = FakeApp(fake_repo)
    job_id = jobs.run_upload_job(app, workspace_id=WS, document_id=DOC, title="t", data=b"x", kind="txt",
                                 content_hash_hex="h" * 64)
    for _ in range(200):
        events = _events(fake_repo, job_id)
        if events and events[-1]["state"] in ("ready", "failed"):
            break
        threading.Event().wait(0.01)
    assert events[-1]["state"] == "ready"
    assert app.state.upload_slots.released == 1


# ---------------------------------------------------------------- caps


def test_too_many_pages_fails_before_any_write(fake_repo, monkeypatch):
    parsed = _parsed([_block("short")], pages=31)
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    app = FakeApp(fake_repo, settings=FakeSettings())
    _run(app)
    events = _events(fake_repo)
    assert events[-1] == {"job_id": "job1", "state": "failed", "document_id": DOC, "version": 1,
                          "error": {"code": "too_many_pages",
                                    "message": jobs.JOB_ERROR_MESSAGES["too_many_pages"]}}
    assert fake_repo.put_version_calls == []
    assert app.state.upload_slots.released == 1


def test_too_many_tokens_fails_before_chunking(fake_repo, monkeypatch):
    text = "a very long document"
    parsed = _parsed([_block(text)])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    embedder = FakeEmbedder(tokens_per_text={text: 20000})
    app = FakeApp(fake_repo, embedder=embedder)
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "too_many_tokens"
    assert fake_repo.put_version_calls == []


def test_workspace_quota_exceeded_fails_without_embedding(fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo foxtrot golf.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    embedder = FakeEmbedder()
    fake_repo.workspace_tokens = FakeSettings.upload_max_workspace_tokens  # already full
    app = FakeApp(fake_repo, embedder=embedder)
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "workspace_quota"
    assert embedder.encode_calls == []
    assert fake_repo.put_version_calls == []


def test_the_120_page_workspace_cap_fails_before_the_token_check(fake_repo, monkeypatch):
    """Known item (docs/v2/M4_PLAN.md 15.8): distinct from the per-version ``upload_max_pages`` (30) cap above —
    this is the WHOLE-WORKSPACE cap (``upload_max_workspace_pages``, 120), checked via ``repo.quota``'s total."""
    parsed = _parsed([_block("short")], pages=25)
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.workspace_pages = FakeSettings.upload_max_workspace_pages - 10   # 10 pages of headroom left
    app = FakeApp(fake_repo)
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "workspace_quota"
    assert fake_repo.put_version_calls == []


def test_the_120_page_workspace_cap_allows_a_document_that_fits(fake_repo, monkeypatch):
    parsed = _parsed([_block("short")], pages=5)
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.workspace_pages = FakeSettings.upload_max_workspace_pages - 10
    app = FakeApp(fake_repo)
    _run(app)
    assert _events(fake_repo)[-1]["state"] == "ready"


def test_too_many_chunks_fails_before_any_embedding(fake_repo, monkeypatch):
    """Finding 8 (security, MEDIUM): ``upload_max_chunks`` (120) was defined in config.py but never enforced — a
    whitespace- or short-paragraph-heavy document under the token cap could still produce hundreds of chunks."""
    parsed = _parsed([_block("word ") for _ in range(200)])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    monkeypatch.setattr(jobs, "_build_units_and_chunks",
                        lambda text, blocks, kind, embedder, settings: ([], [object()] * 200, []))
    embedder = FakeEmbedder()
    app = FakeApp(fake_repo, embedder=embedder)
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "too_many_chunks"
    assert embedder.encode_calls == []
    assert fake_repo.put_version_calls == []


# ---------------------------------------------------------------- workspace deletion mid-job (finding 26, 15.4)


def test_a_job_that_hits_workspacegone_at_put_version_fails_workspace_deleted_and_writes_nothing_further(
        fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.workspace_gone = True
    app = FakeApp(fake_repo)
    reg = _run(app)
    assert fake_repo.put_version_calls == []
    # The persisted job log (fake_repo.jobs, via put_job) never gets the workspace_deleted event: the workspace is
    # already gone, so nothing is written for it, not even the failure itself (docs/v2/M4_PLAN.md 15.4).
    persisted_codes = [j["error"]["code"] for j in _events(fake_repo) if j["state"] == "failed"]
    assert "workspace_deleted" not in persisted_codes
    # ...but a live watcher (the in-memory log) DOES see it, so the client is not left hanging.
    live_events, _ = reg.events_from(WS, "job1", 0)
    assert live_events[-1] == {"job_id": "job1", "state": "failed", "document_id": DOC, "version": 1,
                               "error": {"code": "workspace_deleted",
                                         "message": jobs.JOB_ERROR_MESSAGES["workspace_deleted"]}}
    assert app.state.upload_slots.released == 1


# ---------------------------------------------------------------- persisted-progress retry (finding 27)


def test_a_transient_error_on_a_non_terminal_progress_write_never_kills_the_job(fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.put_job_fail_times = 1     # the very first put_job call (the "received" event) raises once
    app = FakeApp(fake_repo)
    _run(app)
    # The job still reaches ready: a non-terminal write failure is logged and swallowed, never retried, never fatal.
    assert _events(fake_repo)[-1]["state"] == "ready"
    assert fake_repo.put_job_calls >= 2      # the failed attempt, plus every later call


def test_a_transient_error_on_the_terminal_write_is_retried_and_still_succeeds(fake_repo, monkeypatch):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    app = FakeApp(fake_repo)

    # Make ONLY the terminal (ready) write fail once, by counting non-terminal put_job calls first.
    real_put_job = fake_repo.put_job
    state = {"ready_attempts": 0}

    def flaky_put_job(driver, ws, job):
        if job.get("state") == "ready" and state["ready_attempts"] == 0:
            state["ready_attempts"] += 1
            raise TransientRepoError("simulated transient error on the terminal write")
        return real_put_job(driver, ws, job)

    monkeypatch.setattr(repo, "put_job", flaky_put_job)
    _run(app)
    assert _events(fake_repo)[-1]["state"] == "ready"     # the retry succeeded


def test_the_terminal_write_gives_up_after_its_retry_budget_and_logs_but_never_crashes(fake_repo, monkeypatch, caplog):
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.put_job_fail_times = 99     # every put_job call fails
    app = FakeApp(fake_repo)
    monkeypatch.setattr(jobs, "PUT_JOB_RETRY_BACKOFF_S", 0)      # keep the test fast
    with caplog.at_level("ERROR", logger="semigraph.uploads.jobs"):
        _run(app)      # must not raise
    assert fake_repo.jobs == []                                  # nothing was ever actually persisted
    assert any("progress write failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- parse failure / crash


def test_a_parse_error_is_reported_with_its_own_code_never_the_raw_message(fake_repo, monkeypatch):
    def boom(*a, **kw):
        raise ParseError("scanned", "some internal detail that must never reach the client")

    monkeypatch.setattr("semigraph.uploads.parse.parse_document", boom)
    app = FakeApp(fake_repo)
    _run(app)
    error = _events(fake_repo)[-1]["error"]
    assert error["code"] == "scanned"
    assert "internal detail" not in error["message"]
    assert app.state.upload_slots.released == 1


def test_an_unexpected_exception_still_releases_the_slot_and_reports_internal_error(fake_repo, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("semigraph.uploads.parse.parse_document", boom)
    app = FakeApp(fake_repo)
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "internal_error"
    assert app.state.upload_slots.released == 1
    assert jobs.registry(app).is_terminal(WS, "job1") is True


# ---------------------------------------------------------------- embedding: reuse, quota, progress (direct helper tests)


def test_embed_chunks_reuses_already_embedded_text_hashes(fake_repo):
    embedder = FakeEmbedder()
    fake_repo.embedded["doc"] = {"hash-a": [9.0, 9.0]}
    chunk_texts = ["text a", "text b"]
    chunk_hashes = ["hash-a", "hash-b"]
    chunks = [Chunk(seq=0, char_start=0, char_end=6, tokens=10), Chunk(seq=1, char_start=6, char_end=12, tokens=10)]
    events = []
    result = jobs._embed_chunks(object(), WS, "doc", chunk_texts, chunk_hashes, chunks, embedder, FakeSettings(),
                                lambda state, **kw: events.append((state, kw)))
    assert embedder.encode_calls == ["text b"]                 # only the NOT-already-embedded chunk was encoded
    assert result["vectors"] == {1: [0.1, 0.2, 0.3]}
    assert result["already"] == {"hash-a": [9.0, 9.0]}
    assert events and events[-1][1]["progress"]["done"] == 1 and events[-1][1]["progress"]["total"] == 1


def test_embed_chunks_returns_none_when_the_workspace_budget_would_be_exceeded():
    embedder = FakeEmbedder()
    settings = FakeSettings()
    settings.upload_max_workspace_tokens = 5
    chunks = [Chunk(seq=0, char_start=0, char_end=4, tokens=10)]

    class QuotaRepo:
        def embedded_chunks(self, driver, ws, document_id):
            return {}

        def quota(self, driver, ws):
            return {"embedded_tokens": 0}

    import semigraph.uploads.repo as repo_mod
    orig_embedded, orig_quota = repo_mod.embedded_chunks, repo_mod.quota
    repo_mod.embedded_chunks, repo_mod.quota = QuotaRepo().embedded_chunks, QuotaRepo().quota
    try:
        result = jobs._embed_chunks(object(), WS, "doc", ["abcd"], ["h1"], chunks, embedder, settings, lambda *a, **kw: None)
    finally:
        repo_mod.embedded_chunks, repo_mod.quota = orig_embedded, orig_quota
    assert result is None
    assert embedder.encode_calls == []


# ---------------------------------------------------------------- version-to-version comparison


def test_a_second_version_is_compared_against_the_first(fake_repo, monkeypatch):
    v1 = _parsed([_block("Alpha bravo charlie delta echo foxtrot.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: v1)
    app = FakeApp(fake_repo)
    _run(app, job_id="job1")
    assert _events(fake_repo, "job1")[-1]["state"] == "ready"

    v2 = _parsed([_block("Golf hotel india juliet kilo lima mike november.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: v2)
    _run(app, job_id="job2", content_hash_hex="i" * 64)
    ready = _events(fake_repo, "job2")[-1]
    assert ready["state"] == "ready" and ready["version"] == 2
    assert ready["not_compared_reason"] != "first_version"
    assert fake_repo.put_version_calls[-1]["version"] == 2


def test_identical_content_between_versions_is_reported_by_compare_versions(fake_repo, monkeypatch):
    same_text_parsed = _parsed([_block("Repeated content, word for word.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: same_text_parsed)
    app = FakeApp(fake_repo)
    _run(app, job_id="job1")
    _run(app, job_id="job2", content_hash_hex="i" * 64)
    ready = _events(fake_repo, "job2")[-1]
    assert ready["not_compared_reason"] == "identical_content"
    assert ready["items_compared"] is False


# ---------------------------------------------------------------- JobRegistry (findings 1/14/22, docs/v2/M4_PLAN.md 15.3)


def test_job_registry_is_keyed_by_workspace_so_one_workspace_cannot_see_another():
    reg = jobs.JobRegistry()
    reg.create("ws1", "job1")
    reg.create("ws2", "job1")
    reg.append("ws1", "job1", {"state": "parsing", "n": 1})
    assert reg.events_from("ws1", "job1", 0)[0] != reg.events_from("ws2", "job1", 0)[0]
    assert reg.events_from("ws1", "does-not-exist", 0) is None


def test_job_registry_fans_out_every_event_to_every_watcher():
    """Root cause of the SSE leak (finding 1/14/22): a ``queue.Queue`` hands each item to exactly ONE consumer, so a
    second watcher on the same job never sees the terminal event and blocks forever. The append-only log instead
    lets every watcher read from its own cursor."""
    reg = jobs.JobRegistry()
    reg.create("ws", "job1")
    reg.append("ws", "job1", {"state": "parsing"})
    a_events, a_cursor = reg.events_from("ws", "job1", 0)
    b_events, b_cursor = reg.events_from("ws", "job1", 0)
    assert a_events == b_events == [{"state": "parsing"}]
    reg.append("ws", "job1", {"state": "ready"})
    a_events2, _ = reg.events_from("ws", "job1", a_cursor)
    b_events2, _ = reg.events_from("ws", "job1", b_cursor)
    assert a_events2 == b_events2 == [{"state": "ready"}]


def test_try_watch_caps_live_watchers_and_release_watch_frees_the_slot():
    reg = jobs.JobRegistry()
    reg.create("ws", "job1")
    assert reg.try_watch("ws", "job1") is True
    assert reg.try_watch("ws", "job1") is True
    assert reg.try_watch("ws", "job1") is True
    assert reg.try_watch("ws", "job1") is False           # 4th watcher over MAX_LIVE_WATCHERS_PER_JOB
    reg.release_watch("ws", "job1")
    assert reg.try_watch("ws", "job1") is True             # a slot freed up
    assert reg.try_watch("ws", "does-not-exist") is None   # unknown job: fall back to the persisted replay


def test_release_watch_is_idempotent_and_never_goes_negative():
    reg = jobs.JobRegistry()
    reg.create("ws", "job1")
    reg.release_watch("ws", "job1")          # never watched: must not raise or underflow
    reg.release_watch("ws", "does-not-exist")
    assert reg.try_watch("ws", "job1") is True


def test_a_finished_jobs_log_is_kept_for_the_grace_period_then_pruned_lazily():
    now = [1000.0]
    reg = jobs.JobRegistry(clock=lambda: now[0])
    reg.create("ws", "job1")
    reg.append("ws", "job1", {"state": "ready"})
    assert reg.is_terminal("ws", "job1") is True
    now[0] += jobs.REGISTRY_GRACE_PERIOD_S - 1
    assert reg.events_from("ws", "job1", 0) is not None      # still within the grace period
    now[0] += 2
    assert reg.events_from("ws", "job1", 0) is None          # pruned lazily, on the next access
    assert reg.try_watch("ws", "job1") is None


def test_append_to_an_unknown_job_is_a_safe_no_op():
    reg = jobs.JobRegistry()
    reg.append("ws", "does-not-exist", {"state": "parsing"})     # must not raise


def test_count_tokens_available_is_false_for_a_backend_that_cannot_count():
    class NoCount:
        def count_tokens(self, text):
            raise NotImplementedError

    assert jobs.count_tokens_available(NoCount()) is False
    assert jobs.count_tokens_available(FakeEmbedder()) is True


def test_sweeper_stops_promptly_and_sweeps_on_its_own_thread(fake_repo):
    sweeper = jobs._Sweeper(object())
    jobs.SWEEP_INTERVAL_S, saved = 0.01, jobs.SWEEP_INTERVAL_S
    try:
        sweeper.start()
        import time
        time.sleep(0.05)
    finally:
        jobs.SWEEP_INTERVAL_S = saved
        sweeper.stop(timeout=2)
    assert not sweeper._thread.is_alive()


# ---------------------------------------------------------------- sweeper: finding 26 (docs/v2/M4_PLAN.md 15.4)


def test_sweeper_calls_fail_interrupted_jobs_once_before_its_first_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver: (calls.append("fail_interrupted"), 2)[1],
                        raising=False)
    sweeper = jobs._Sweeper(object())
    sweeper._safe_fail_interrupted()
    assert calls == ["fail_interrupted"]


def test_sweeper_skips_fail_interrupted_jobs_gracefully_when_repo_does_not_implement_it_yet(monkeypatch):
    monkeypatch.delattr(repo, "fail_interrupted_jobs", raising=False)
    jobs._Sweeper(object())._safe_fail_interrupted()      # must not raise — a reported seam, not a crash


def test_a_broken_fail_interrupted_jobs_never_blocks_or_crashes_boot(monkeypatch):
    def boom(driver):
        raise RuntimeError("fail_interrupted_jobs exploded")

    monkeypatch.setattr(repo, "fail_interrupted_jobs", boom, raising=False)
    jobs._Sweeper(object())._safe_fail_interrupted()      # must not raise


def test_sweeper_sweeps_orphans_every_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "sweep_orphans", lambda driver, now: (calls.append("orphans"), 0)[1], raising=False)
    jobs._Sweeper(object())._safe_sweep_orphans()
    assert calls == ["orphans"]


def test_sweeper_skips_sweep_orphans_gracefully_when_repo_does_not_implement_it_yet(monkeypatch):
    monkeypatch.delattr(repo, "sweep_orphans", raising=False)
    jobs._Sweeper(object())._safe_sweep_orphans()          # must not raise — a reported seam, not a crash


def test_a_broken_sweep_orphans_never_stops_the_expired_workspace_sweep_from_running(monkeypatch):
    """The two sweeps run in SEPARATE try/except blocks: a missing or broken ``sweep_orphans`` must never prevent
    the already-shipped, load-bearing ``sweep_expired`` from running every cycle."""
    def boom(driver, now):
        raise RuntimeError("sweep_orphans exploded")

    calls = []
    monkeypatch.setattr(repo, "sweep_expired", lambda driver, now: (calls.append("expired"), 0)[1])
    monkeypatch.setattr(repo, "sweep_orphans", boom, raising=False)
    sweeper = jobs._Sweeper(object())
    sweeper._safe_sweep()
    sweeper._safe_sweep_orphans()      # raises internally, caught, never propagates
    assert calls == ["expired"]


def test_start_if_enabled_starts_the_sweeper_even_when_uploads_are_disabled():
    """Finding 26: a soft rollback (``UPLOADS_ENABLED=false``) must not silently stop the 24h deletion of
    workspaces created before the rollback."""
    class DisabledSettings:
        uploads_enabled = False

    app = FakeApp(FakeRepo())
    app.state.settings = DisabledSettings()
    try:
        jobs.start_if_enabled(app)
        assert app.state.upload_sweeper is not None
        assert app.state.uploads_ready is False
    finally:
        jobs.stop(app)


def test_start_if_enabled_sets_uploads_ready_only_when_enabled_and_the_embedder_can_count():
    """Finding 29: ``uploads_ready`` is the ONE flag ``routes.uploads_available`` reads; it must be false whenever
    either half of the condition is false."""
    class EnabledSettings:
        uploads_enabled = True

    app = FakeApp(FakeRepo())
    app.state.settings = EnabledSettings()
    try:
        jobs.start_if_enabled(app)
        assert app.state.uploads_ready is True       # FakeEmbedder can count tokens
    finally:
        jobs.stop(app)


def test_start_if_enabled_uploads_ready_is_false_when_the_embedder_cannot_count_tokens():
    class EnabledSettings:
        uploads_enabled = True

    class NoCountEmbedder(FakeEmbedder):
        def count_tokens(self, text):
            raise NotImplementedError

    app = FakeApp(FakeRepo(), embedder=NoCountEmbedder())
    app.state.settings = EnabledSettings()
    try:
        jobs.start_if_enabled(app)
        assert app.state.uploads_ready is False
    finally:
        jobs.stop(app)


# ---------------------------------------------------------------- run_upload_job: the thread-start failure window


def test_run_upload_job_discards_its_registry_entry_and_propagates_when_the_thread_fails_to_start(monkeypatch):
    """Findings 5/17/23: once the thread has started, IT owns the upload slot's release. The only window
    ``run_upload_job`` itself must handle is a ``thread.start()`` failure — here it must not leave a registry entry
    behind for a watcher to find, and must leave the slot for the CALLER to release (it never touches it)."""
    monkeypatch.setattr(jobs.secrets, "token_hex", lambda n: "deadbeef")

    class BoomThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(jobs.threading, "Thread", BoomThread)
    app = FakeApp(FakeRepo())
    with pytest.raises(RuntimeError, match="can't start new thread"):
        jobs.run_upload_job(app, workspace_id=WS, document_id=DOC, title="t", data=b"x", kind="txt",
                            content_hash_hex="h" * 64)
    assert jobs.registry(app).try_watch(WS, "deadbeef") is None    # discarded — nothing left for a watcher to find
    assert app.state.upload_slots.released == 0                    # run_upload_job never touches the slot itself
