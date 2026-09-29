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
    upload_max_workspace_tokens = 48000
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
        self.jobs: list[dict] = []
        self.put_version_calls: list[dict] = []

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
        return {"embedded_tokens": self.workspace_tokens}

    def put_version(self, driver, ws, **kw):
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
        self.jobs.append(dict(job))

    def sweep_expired(self, driver, now):
        return 0


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
    assert jobs.registry(app).get(WS, "job1") is None            # discarded once the job ends


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
    assert jobs.registry(app).get(WS, "job1") is None


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


# ---------------------------------------------------------------- JobRegistry / sweeper


def test_job_registry_is_keyed_by_workspace_so_one_workspace_cannot_see_another():
    reg = jobs.JobRegistry()
    reg.create("ws1", "job1")
    reg.create("ws2", "job1")
    assert reg.get("ws1", "job1") is not reg.get("ws2", "job1")
    assert reg.get("ws1", "does-not-exist") is None


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
