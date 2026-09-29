"""Upload job state machine and the TTL sweeper (M4 Worker C, docs/v2/M4_PLAN.md 4.2, 5, 14.1, 14.6).

Neo4j (``uploads.repo``) and the document parser (``uploads.parse``) are faked; ``uploads.units`` and
``uploads.changes`` run for real (pure, cheap, no I/O) except where a test needs exact control over chunk
boundaries, in which case ``chunk_units`` is monkeypatched. ``_worker`` (the thread body) is called directly and
synchronously so tests never race a background thread; one test exercises the public, threaded
:func:`run_upload_job` entry point end to end.
"""

from __future__ import annotations

import json
import logging
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
        self.pages_by_document: dict[str, int] = {}     # C3 item 5 (docs/v2/M4_PLAN.md 15.8)
        self.jobs: list[dict] = []
        self.put_version_calls: list[dict] = []
        self.put_job_fail_times = 0       # the next N put_job calls raise a transient error
        self.put_job_calls = 0
        self.workspace_gone = False       # once true, put_version/put_job raise WorkspaceGone
        # C3 item 1 (docs/v2/M4_PLAN.md 15.4): a predicate over the job dict a put_job call is about to persist —
        # when it returns True, THAT call (and, being sticky, every later one) raises WorkspaceGone. Independent of
        # `workspace_gone` above (which only fires for the already-known `workspace_deleted` persist attempt) so a
        # test can simulate the workspace vanishing partway through an EARLIER, arbitrary stage.
        self.put_job_gone_when = None
        self._put_job_gone_triggered = False

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
        return {"embedded_tokens": self.workspace_tokens, "pages": self.workspace_pages,
               "pages_by_document": dict(self.pages_by_document)}

    def put_version(self, driver, ws, **kw):
        if self.workspace_gone:
            raise WorkspaceGone(ws)
        self.put_version_calls.append(kw)
        self.pages_by_document[kw["document_id"]] = kw["pages"]
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
        if self._put_job_gone_triggered or (self.put_job_gone_when is not None and self.put_job_gone_when(job)):
            self._put_job_gone_triggered = True     # sticky: once the workspace is "gone" it stays gone
            raise WorkspaceGone(ws)
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


def test_the_first_version_change_report_has_the_same_key_set_as_compare_versions_output(fake_repo, monkeypatch):
    """C3 item 3 (docs/v2/M4_PLAN.md 15.6/15): there being no PREVIOUS version to diff against must not make the
    first version's report a DIFFERENT shape from every other compare_versions output — including
    ``minor_rewordings: []`` — so a consumer (the page's changesHtml) never needs a special case for it."""
    from semigraph.uploads.changes import VersionView, compare_versions

    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    app = FakeApp(fake_repo)
    _run(app)
    first_version_report = fake_repo.put_version_calls[-1]["change_report"]

    # The real oracle: any compare_versions call (even one that hits a guard, like identical content here) always
    # returns the SAME key set — that invariant is exactly what this test pins the first-version report against.
    view = VersionView(text="same text", units=(), chunk_spans=(), method="text", chars_per_page=1000.0)
    real_report = compare_versions(view, view)
    assert set(first_version_report.keys()) == set(real_report.keys())
    assert first_version_report["minor_rewordings"] == []


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


def test_a_same_size_reversion_of_a_document_already_at_the_workspace_cap_is_accepted(fake_repo, monkeypatch):
    """C3 item 5 (docs/v2/M4_PLAN.md 15.8): ``repo.quota``'s ``pages_by_document`` lets the target document's OWN
    current pages be subtracted from the workspace total before the new version's pages are added back — a
    same-size new version of a document already sitting at the cap must be ACCEPTED, not refused."""
    cap = FakeSettings.upload_max_workspace_pages
    this_documents_pages = 20      # within upload_max_pages (30, the PER-VERSION cap) — only the workspace total is at its cap
    parsed = _parsed([_block("short")], pages=this_documents_pages)
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.workspace_pages = cap                              # the workspace total is already AT the cap...
    fake_repo.pages_by_document[DOC] = this_documents_pages       # ...entirely from THIS document's current pages
    app = FakeApp(fake_repo)
    _run(app)
    assert _events(fake_repo)[-1]["state"] == "ready"


def test_a_new_document_that_would_push_the_workspace_over_the_cap_is_still_refused(fake_repo, monkeypatch):
    """The flip side of the fix above: a document with ZERO current pages (brand new, or simply a different
    document than the one already occupying the cap) gets no subtraction, so it is refused exactly as before."""
    cap = FakeSettings.upload_max_workspace_pages
    parsed = _parsed([_block("short")], pages=5)
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.workspace_pages = cap                       # already at the cap, via a DIFFERENT document
    fake_repo.pages_by_document["some-other-document"] = cap
    app = FakeApp(fake_repo)                              # uploads for DOC, which has 0 current pages
    _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "workspace_quota"


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


def test_an_early_progress_write_that_hits_workspacegone_ends_locally_as_workspace_deleted(fake_repo, monkeypatch):
    """C3 item 1: WorkspaceGone from a put_job at ANY stage — not only the put_version call above — must end the
    job the same way. Triggered here at the very first non-terminal write ("validating"), well before parsing."""
    parsed = _parsed([_block("Alpha bravo charlie delta echo.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.put_job_gone_when = lambda job: job.get("state") == "validating"
    app = FakeApp(fake_repo)
    reg = _run(app)                                     # must not raise
    live_events, _ = reg.events_from(WS, "job1", 0)
    terminal = [e for e in live_events if e["state"] == "failed"]
    assert len(terminal) == 1 and terminal[0]["error"]["code"] == "workspace_deleted"
    assert fake_repo.put_version_calls == []
    assert app.state.upload_slots.released == 1


def test_a_known_failures_persist_that_discovers_the_workspace_gone_reports_workspace_deleted_not_the_original_code(
        fake_repo, monkeypatch):
    """C3 item 1 (docs/v2/M4_PLAN.md 15.4): ``_emit`` persists BEFORE appending to the live log — if PERSISTING a
    known failure (here ``too_many_pages``) is what discovers the workspace is gone, that misleading event must
    never reach a live watcher; only the accurate, local-only ``workspace_deleted`` terminal event should."""
    parsed = _parsed([_block("short")], pages=31)        # triggers too_many_pages
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)
    fake_repo.put_job_gone_when = lambda job: job.get("error", {}).get("code") == "too_many_pages"
    app = FakeApp(fake_repo)
    reg = _run(app)
    live_events, _ = reg.events_from(WS, "job1", 0)
    terminal = [e for e in live_events if e["state"] == "failed"]
    assert len(terminal) == 1 and terminal[0]["error"]["code"] == "workspace_deleted"
    assert [j for j in fake_repo.jobs if j["state"] == "failed"] == []    # too_many_pages itself was never persisted


def test_a_stale_version_view_read_during_comparing_ends_as_workspace_deleted_not_a_crash(
        fake_repo, monkeypatch, caplog):
    """R6 (docs/v2/M4_PLAN.md 15.4, round-4 reliability review): a benign user delete mid-'comparing' must be
    logged at INFO as workspace_deleted — never ERROR 'upload job crashed' with a traceback. ``version_view`` is a
    plain read (it never takes ``put_version``/``put_job``'s workspace lock), so it can start returning ``None``
    for a version a concurrent ``delete_workspace`` just removed; ``_compare_with_previous`` must treat that
    exactly like ``WorkspaceGone``, not let ``_version_view(None)`` crash with an unrelated ``TypeError`` first
    (which used to reach ``_worker``'s generic ``except Exception`` and log a full traceback for an ordinary user
    action)."""
    v1 = _parsed([_block("Alpha bravo charlie delta echo foxtrot.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: v1)
    app = FakeApp(fake_repo)
    _run(app, job_id="job1")                             # first version lands fine

    v2 = _parsed([_block("Golf hotel india juliet kilo lima mike november.")])
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: v2)
    # repo.version_view was bound (via monkeypatch, in the fake_repo fixture) to fake_repo's method AT FIXTURE
    # SETUP time, so the module attribute itself must be repatched here — reassigning fake_repo.version_view alone
    # would not change what `repo.version_view` resolves to.
    monkeypatch.setattr(repo, "version_view", lambda driver, ws, document_id, version: None)   # deleted mid-flight
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        reg = _run(app, job_id="job2", content_hash_hex="i" * 64)   # must not raise
    live_events, _ = reg.events_from(WS, "job2", 0)
    terminal = [e for e in live_events if e["state"] == "failed"]
    assert len(terminal) == 1 and terminal[0]["error"]["code"] == "workspace_deleted"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert not any("crashed" in r.getMessage() for r in caplog.records)
    persisted_codes = [j["error"]["code"] for j in _events(fake_repo, "job2") if j["state"] == "failed"]
    assert "internal_error" not in persisted_codes        # the internal_error write itself never landed
    assert app.state.upload_slots.released == 2           # once per job run on this shared app (job1 then job2)


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


def test_a_parse_error_is_logged_with_its_code_and_exc_type_never_the_message(fake_repo, monkeypatch, caplog):
    """C3 item 4 (docs/v2/M4_PLAN.md 15.7): the log line's detail must be ParseError.exc_type (the sandboxed
    child's own exception CLASS NAME, e.g. "PdfReadError") — never type(e).__name__ (always the constant string
    "ParseError", which tells an operator nothing) and never ParseError's own message."""
    def boom(*a, **kw):
        raise ParseError("parse_failed", "some internal detail that must never reach the log",
                         exc_type="PdfReadError")

    monkeypatch.setattr("semigraph.uploads.parse.parse_document", boom)
    app = FakeApp(fake_repo)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        _run(app)
    assert _events(fake_repo)[-1]["error"]["code"] == "parse_failed"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "(PdfReadError)" in logged
    assert "(ParseError)" not in logged
    assert "some internal detail" not in logged


def test_a_parse_error_with_no_exc_type_is_logged_without_it_and_never_crashes(fake_repo, monkeypatch, caplog):
    """``exc_type`` is ``None`` when the failure was not a sandboxed-child exception at all (e.g. a page-count
    guard raised directly in the parent) — logging must tolerate that, never format ``None`` into the message."""
    def boom(*a, **kw):
        raise ParseError("scanned", "internal detail")   # exc_type left at its default of None

    monkeypatch.setattr("semigraph.uploads.parse.parse_document", boom)
    app = FakeApp(fake_repo)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        _run(app)      # must not raise
    assert _events(fake_repo)[-1]["error"]["code"] == "scanned"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "None" not in logged


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


def test_registry_keys_snapshots_every_tracked_job_including_ones_in_their_grace_period():
    """C3 item 2 (docs/v2/M4_PLAN.md 15.4): the sweeper's exclusion set — a job just finished (grace period still
    counting down) is harmless to include too, since its persisted state is already terminal."""
    reg = jobs.JobRegistry()
    reg.create("ws1", "job1")
    reg.create("ws2", "job2")
    reg.append("ws2", "job2", {"state": "ready"})     # terminal, but still within its grace period
    assert reg.keys() == frozenset({("ws1", "job1"), ("ws2", "job2")})
    assert jobs.JobRegistry().keys() == frozenset()


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


# ---------------------------------------------------------------- sweeper: finding 26 / 27 (docs/v2/M4_PLAN.md 15.4)


def test_sweeper_calls_fail_interrupted_jobs_once_before_its_first_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver, **kw: (calls.append(kw), 2)[1])
    jobs._Sweeper(object())._safe_fail_interrupted()
    assert calls == [{}]          # no registry/settings: repo's own defaults apply


def test_a_broken_fail_interrupted_jobs_never_blocks_or_crashes_boot(monkeypatch):
    def boom(driver, **kw):
        raise RuntimeError("fail_interrupted_jobs exploded")

    monkeypatch.setattr(repo, "fail_interrupted_jobs", boom)
    jobs._Sweeper(object(), jobs.JobRegistry(), FakeSettings())._safe_fail_interrupted()      # must not raise


def test_sweeper_start_pass_uses_older_than_s_zero_regardless_of_settings(monkeypatch):
    """Finding C5 (round-4 reliability review, docs/v2/M4_PLAN.md 15.4, finding 27 residual): a job left
    non-terminal by the process that died must be marked interrupted on the VERY FIRST pass — this process's own
    JobRegistry is empty at start, and the deployment is single-machine, so there is nothing else it could be."""
    calls = []
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver, **kw: (calls.append(kw), 0)[1])
    monkeypatch.setattr(repo, "sweep_expired", lambda driver, now: 0)
    monkeypatch.setattr(repo, "sweep_orphans", lambda driver, now: 0)
    sweeper = jobs._Sweeper(object(), jobs.JobRegistry(), FakeSettings())
    jobs.SWEEP_INTERVAL_S, saved = 10, jobs.SWEEP_INTERVAL_S     # long enough that only the start pass fires
    try:
        sweeper.start()
        import time
        time.sleep(0.05)
    finally:
        jobs.SWEEP_INTERVAL_S = saved
        sweeper.stop(timeout=2)
    assert calls[0] == {"older_than_s": 0, "exclude": frozenset()}


def test_sweeper_prunes_the_registry_every_periodic_cycle(monkeypatch):
    """Finding 27 residual (round-4 reliability review): eager pruning must run every cycle so a finished job past
    its grace period actually leaves the registry (and fail_interrupted_jobs' own ``exclude``) even if nothing ever
    reconnects to watch it again."""
    reg = jobs.JobRegistry()
    calls = []
    monkeypatch.setattr(reg, "prune", lambda: calls.append(1))
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver, **kw: 0)
    monkeypatch.setattr(repo, "sweep_expired", lambda driver, now: 0)
    monkeypatch.setattr(repo, "sweep_orphans", lambda driver, now: 0)
    sweeper = jobs._Sweeper(object(), reg)
    jobs.SWEEP_INTERVAL_S, saved = 0.01, jobs.SWEEP_INTERVAL_S
    try:
        sweeper.start()
        import time
        time.sleep(0.05)
    finally:
        jobs.SWEEP_INTERVAL_S = saved
        sweeper.stop(timeout=2)
    assert len(calls) >= 1


def test_a_broken_registry_prune_never_blocks_or_crashes_the_sweeper(monkeypatch):
    reg = jobs.JobRegistry()

    def boom():
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(reg, "prune", boom)
    jobs._Sweeper(object(), reg)._safe_prune_registry()      # must not raise


def test_registry_prune_removes_a_finished_jobs_log_once_its_grace_period_has_elapsed():
    now = [1000.0]
    reg = jobs.JobRegistry(clock=lambda: now[0])
    reg.create("ws", "job1")
    reg.append("ws", "job1", {"state": "ready"})
    now[0] += jobs.REGISTRY_GRACE_PERIOD_S + 1
    assert ("ws", "job1") in reg.keys()          # still present: nothing has pruned it yet
    reg.prune()
    assert ("ws", "job1") not in reg.keys()


def test_registry_prune_leaves_a_fresh_or_unfinished_job_alone():
    reg = jobs.JobRegistry()
    reg.create("ws", "job1")
    reg.append("ws", "job1", {"state": "embedding"})     # non-terminal: never pruned
    reg.prune()
    assert ("ws", "job1") in reg.keys()


def test_sweeper_calls_fail_interrupted_jobs_on_every_cycle_not_only_at_start(monkeypatch):
    """A job whose owning process died mid-embed must not wait for the NEXT restart: the check repeats every cycle."""
    import time as time_mod

    calls = []
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver, **kw: (calls.append(1), 0)[1])
    monkeypatch.setattr(repo, "sweep_expired", lambda driver, now: 0)
    monkeypatch.setattr(repo, "sweep_orphans", lambda driver, now: 0)
    sweeper = jobs._Sweeper(object())
    jobs.SWEEP_INTERVAL_S, saved = 0.01, jobs.SWEEP_INTERVAL_S
    try:
        sweeper.start()
        time_mod.sleep(0.1)
    finally:
        jobs.SWEEP_INTERVAL_S = saved
        sweeper.stop(timeout=2)
    assert len(calls) >= 2      # the initial call, plus at least one more full cycle


def test_fail_interrupted_older_than_s_exceeds_the_parse_plus_embed_wall_budget_with_margin():
    """Pins the arithmetic: a config change to either budget can never shrink the threshold below the two wall budgets."""
    settings = FakeSettings()
    threshold = jobs._fail_interrupted_older_than_s(settings)
    assert threshold - (settings.upload_parse_timeout_s + settings.upload_embed_timeout_s) ==         jobs.FAIL_INTERRUPTED_STAGE_MARGIN_S > 0


def test_the_sweeper_excludes_every_job_this_process_still_has_open_and_passes_the_settings_threshold(monkeypatch):
    """The live-job guarantee: the jobs in THIS process's JobRegistry reach repo.fail_interrupted_jobs as ``exclude``
    (repo skips them whatever their stored age), with the threshold derived from the job budgets."""
    reg = jobs.JobRegistry()
    reg.create(WS, "live-job")
    calls = []
    monkeypatch.setattr(repo, "fail_interrupted_jobs", lambda driver, **kw: (calls.append(kw), 0)[1])
    jobs._Sweeper(object(), reg, FakeSettings())._safe_fail_interrupted()
    assert calls == [{"older_than_s": jobs._fail_interrupted_older_than_s(FakeSettings()), "exclude": reg.keys()}]
    assert (WS, "live-job") in calls[0]["exclude"]


def test_sweeper_sweeps_orphans_every_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "sweep_orphans", lambda driver, now: (calls.append("orphans"), 0)[1])
    jobs._Sweeper(object())._safe_sweep_orphans()
    assert calls == ["orphans"]


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
