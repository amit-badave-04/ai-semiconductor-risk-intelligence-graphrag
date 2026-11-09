"""The upload embed ceiling, sized from the measured CPU throttle (docs/specs/upload-embed-timeout.md, M5a follow-up to decision 21).

A throttled CPU is simulated with two doubles, no sleeping and no real CPU: a fake clock (a callable returning a float) and an
embedder whose ``encode_passages([text])`` advances that clock by ``core_seconds(text) / share(now)``, where ``core_seconds`` is
``tokens / (85.8 x 1.18)`` (the conservative class factor: 1.18 gives the longer time) and ``share(now)`` is a constant sustained
share, or 1.0 until the burst ends (115 s, ``w0/report.txt``) and a constant after. The clock is handed to
``jobs._process(..., clock=fake)`` (worker-level tests wrap ``_process`` with ``functools.partial``): the global
``time.monotonic`` is never patched, and a 1,580 s job runs in milliseconds on any OS. The cap job is 10,000 tokens as 25 chunks
of 400 tokens (``jobs._build_units_and_chunks`` is the one seam replaced, as in tests/test_serve_upload_jobs.py).

Covered: the ceiling's boundary and last-chunk rules, the failure event (S2), the exits through ``_worker`` (S4), the
``upload stages`` log line (S7), and the sweeper age. Since docs/specs/upload-cap-and-abort.md a job whose own rate projects past
the ceiling stops at its third chunk as ``too_slow`` (tests/test_serve_upload_cap_and_abort.py), so a plain ``timeout`` here is a
job whose elapsed time passes the ceiling on chunks 1 or 2, or at once on the third: the ``steps`` doubles below use 1,050 s a
chunk (2,100 s after two chunks: not over; 3,150 s after three: over). The production validator is pinned in
tests/test_serve_config_production.py and tests/test_check_env_fly.py. Imports: ``semigraph`` modules (including
``serve.workspace_routes``, for the real upload slot) and ``tests/serve_state_fakes.py`` (the drain fixture); all of them are in
the serve-shipped CI job, which runs this file.
"""

from __future__ import annotations

import builtins
import functools
import json
import math
import re

import pytest
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture

from semigraph.config import Settings
from semigraph.hashing import content_hash
from semigraph.serve import workspace_routes as wr
from semigraph.uploads import jobs, repo
from semigraph.uploads.parse import Block, ParsedDoc, ParseError
from semigraph.uploads.units import Chunk

WS = "c" * 32
DOC = "0123456789ab"
TITLE = "confidential-roadmap.pdf"
FULL_CORE_TOKENS_PER_S = 85.8            # w1/rss_fp32.json: one performance-class core
CLASS_FACTOR = 1.18                      # the conservative end of the shared-core / performance-core range (1.18 to 1.31)
BURST_END_S = 115.0                      # w0/report.txt: the throttle sets in at about this second on a fresh machine
CAP_CHUNKS, CHUNK_TOKENS = 25, 400       # 10,000 tokens: the cap (upload_max_tokens)
CAP_TOKENS = CAP_CHUNKS * CHUNK_TOKENS
DEFAULT_CEILING = Settings.model_fields["upload_embed_timeout_s"].default
DEFAULT_CAP = Settings.model_fields["upload_max_tokens"].default
WORST_RATE = 5.54                        # tokens a second: the worst rate measured (W1d, docs/v2/M5_DECISIONS.md 10.11)
MAX_UPLOADS_PER_DAY = Settings.model_fields["max_uploads_per_day"].default
SECONDS_PER_DAY = 86_400
FAILED_TIMEOUT_EVENT = {"job_id": "job1", "state": "failed", "document_id": DOC, "version": 1,
                        "error": {"code": "timeout", "message": "processing took too long and was stopped"}}
NOT_COMPARED = {"items_compared": False, "not_compared_reason": "first_version"}
ORIGINAL_PROCESS = jobs._process         # captured once: a test that runs two jobs must not wrap a wrapper


def cap_job_seconds(share: float) -> float:
    """What the cap job needs on a core that delivers ``share`` of itself, by the spec's arithmetic."""
    return CAP_TOKENS / (FULL_CORE_TOKENS_PER_S * CLASS_FACTOR * share)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell must not change a default these tests read."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture(autouse=True)
def no_cgroup_file(monkeypatch, tmp_path):
    """The stage line reads the kernel's cgroup counters: a Linux runner has a real file, this machine may not. Every test here
    sees none unless it writes its own (the cgroup tests in tests/test_serve_upload_cap_and_abort.py)."""
    if hasattr(jobs, "CGROUP_CPU_STAT_PATH"):
        monkeypatch.setattr(jobs, "CGROUP_CPU_STAT_PATH", str(tmp_path / "absent" / "cpu.stat"))


# ---------------------------------------------------------------- the doubles


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeEmbedder:
    """``count_tokens`` is the number of words; ``encode_passages`` advances the clock by ``seconds_for(text)``."""

    name = "fake-embedder"

    def __init__(self, clock: FakeClock, seconds_for, *, fail_on_call: int | None = None) -> None:
        self.clock = clock
        self.seconds_for = seconds_for
        self.fail_on_call = fail_on_call
        self.calls = 0

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def encode_passages(self, texts):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("the native call failed")
        self.clock.advance(sum(self.seconds_for(t) for t in texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


def constant_share(share: float):
    return lambda now: share


def burst_then(share: float):
    return lambda now: 1.0 if now < BURST_END_S else share


def throttled(clock: FakeClock, share) -> FakeEmbedder:
    """A chunk costs its core-seconds divided by the share the machine delivers when the call starts."""
    return FakeEmbedder(clock, lambda text: len(text.split()) / (FULL_CORE_TOKENS_PER_S * CLASS_FACTOR) / share(clock.now))


def steps(clock: FakeClock, seconds: float, **kwargs) -> FakeEmbedder:
    return FakeEmbedder(clock, lambda text: seconds, **kwargs)


class JobSettings:
    upload_parse_timeout_s = 90
    upload_max_pages = 30
    upload_max_tokens = 10000
    upload_max_chunk_tokens = 512
    upload_max_chunks = 120
    upload_max_workspace_tokens = 48000
    upload_max_workspace_pages = 120
    upload_compare_timeout_s = 120
    uploads_enabled = True

    def __init__(self, ceiling) -> None:
        self.upload_embed_timeout_s = ceiling


class FakeRepo:
    """The slice of ``uploads.repo`` a job touches, in memory; ``put_version`` advances the clock by ``index_s``."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.index_s = 0.0
        self.jobs: list[dict] = []
        self.put_version_calls: list[dict] = []
        self.versions: dict[str, list[int]] = {}
        self.embedded: dict[str, dict[str, list[float]]] = {}
        self.workspace_tokens = 0
        self.workspace_pages = 0
        self.workspace_gone = False

    def latest_version(self, driver, ws, document_id):
        rows = self.versions.get(document_id)
        return {"version": rows[-1]} if rows else None

    def embedded_chunks(self, driver, ws, document_id):
        return dict(self.embedded.get(document_id, {}))

    def quota(self, driver, ws):
        return {"embedded_tokens": self.workspace_tokens, "pages": self.workspace_pages, "pages_by_document": {}}

    def put_version(self, driver, ws, **kw):
        if self.workspace_gone:
            raise repo.WorkspaceGone(ws)
        self.clock.advance(self.index_s)
        self.put_version_calls.append(kw)
        self.versions.setdefault(kw["document_id"], []).append(kw["version"])
        bucket = self.embedded.setdefault(kw["document_id"], {})
        for chunk in kw["chunks"]:
            if chunk["embedded"]:
                bucket[chunk["text_hash"]] = chunk["embedding"]
                self.workspace_tokens += chunk["tokens"]

    def put_job(self, driver, ws, job):
        self.jobs.append(dict(job))


class State:
    pass


class FakeApp:
    def __init__(self, embedder, settings, slots) -> None:
        self.state = State()
        self.state.driver = object()
        self.state.embedder = embedder
        self.state.settings = settings
        self.state.upload_slots = slots


class CountingSlot(wr.DrainCountedSlot):
    """The real upload slot (a ``BoundedSemaphore`` raises on a double release), counting the releases it sees."""

    def __init__(self) -> None:
        super().__init__(1)
        self.released = 0

    def release(self, n: int = 1) -> None:
        super().release(n)
        self.released += 1


class World:
    """One clock, one fake repo, and the seams of ``jobs`` a job reads: the parser and the chunker."""

    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.clock = FakeClock()
        self.repo = FakeRepo(self.clock)
        for name in ("latest_version", "embedded_chunks", "quota", "put_version", "put_job"):
            monkeypatch.setattr(repo, name, getattr(self.repo, name))
        self.app: FakeApp | None = None

    def document(self, texts, *, pages=1, parse_s=0.0, chunk_s=0.0, parse=None) -> list[Chunk]:
        """A parsed document whose chunks are exactly ``texts`` (one block each); the parse and chunk doubles advance
        the clock by ``parse_s`` and ``chunk_s``. ``parse`` replaces the parser (a failure)."""
        blocks = tuple(Block(text=t, page=1, size=10.0, bold=False, kind_hint="paragraph") for t in texts)
        parsed = ParsedDoc(method="text", pages=pages, blocks=blocks, chars_per_page=1000.0, warnings=())
        chunks, position = [], 0
        for seq, text in enumerate(texts):
            chunks.append(Chunk(seq=seq, char_start=position, char_end=position + len(text), tokens=len(text.split())))
            position += len(text) + 2                                   # canonical_text joins blocks with a blank line

        def parse_document(*args, **kwargs):
            self.clock.advance(parse_s)
            return parsed

        def build_units_and_chunks(text, blocks, kind, embedder, settings):
            self.clock.advance(chunk_s)
            return [], chunks, []

        self.monkeypatch.setattr("semigraph.uploads.parse.parse_document", parse or parse_document)
        self.monkeypatch.setattr(jobs, "_build_units_and_chunks", build_units_and_chunks)
        return chunks

    def run(self, embedder, *, ceiling=DEFAULT_CEILING, job_id="job1", slots=None, title=TITLE,
            charged_day=None) -> list[dict]:
        """Runs one job through ``_worker`` (the thread body, called directly) on this world's clock; returns the live
        event log of the job."""
        slots = slots or CountingSlot()
        self.app = FakeApp(embedder, JobSettings(ceiling), slots)
        self.monkeypatch.setattr(jobs, "_process", functools.partial(ORIGINAL_PROCESS, clock=self.clock))
        reg = jobs.registry(self.app)
        reg.create(WS, job_id)
        slots.acquire(blocking=False)                                  # the route's acquire; the job's finally releases
        extra = {} if charged_day is None else {"charged_day": charged_day}
        jobs._worker(self.app, WS, job_id, DOC, title, b"raw bytes", "txt", "h" * 64, **extra)
        self.slots, self.reg = slots, reg
        return reg.events_from(WS, job_id, 0)[0]

    def persisted(self, job_id="job1") -> list[dict]:
        return [j for j in self.repo.jobs if j["job_id"] == job_id]


@pytest.fixture
def world(monkeypatch, fresh_drain):  # noqa: F811 - the fixture, requested by name
    return World(monkeypatch)


def chunk_texts(n: int = CAP_CHUNKS, tokens: int = CHUNK_TOKENS, *, variant: str = "a", changed=()) -> list[str]:
    """``n`` distinct texts of ``tokens`` words each; the first word starts with ``secret`` (it must never reach a log)."""
    return [f"secret{'b' if i in changed else variant}x{i} " + " ".join(["w"] * (tokens - 1)) for i in range(n)]


def stage_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("upload stages ")]


def fields_of(line: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in line.split()[2:])


# The allow-list S7 asks for: every field in the spec's order, each optional (a stage not reached is omitted), nothing else.
STAGE_LINE = re.compile(
    r"^upload stages ws_hash=[0-9a-f]{12} job_id=[0-9a-z]+(?: parse_s=\d+\.\d+)?(?: chunk_s=\d+\.\d+)?"
    r"(?: embed_s=\d+\.\d+)?(?: compare_s=\d+\.\d+)?(?: index_s=\d+\.\d+)?(?: chunks=\d+)?(?: fresh=\d+)?"
    r"(?: done=\d+)?(?: tokens=\d+)?(?: projected_s=\d+\.\d)?(?: aborted=too_slow)?"
    r"(?: cpu_usage_usec_start=\d+)?(?: cpu_nr_throttled_start=\d+)?(?: cpu_throttled_usec_start=\d+)?"
    r"(?: cpu_usage_usec_end=\d+)?(?: cpu_nr_throttled_end=\d+)?(?: cpu_throttled_usec_end=\d+)?$")


def last_event(events: list[dict]) -> dict:
    return events[-1]


# ---------------------------------------------------------------- 1. the cap job under the throttle


@pytest.mark.parametrize("share", [0.17, 0.12, 0.10, 0.08])
def test_a_cap_job_reaches_ready_under_the_default_ceiling_from_the_start_of_the_throttle(world, caplog, share):
    """17% (930 s), 12% (1,317 s), 10% (1,580 s) and 8% (1,975 s): the ceiling carries the cap job to ``ready``."""
    world.document(chunk_texts())
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(throttled(world.clock, constant_share(share)))

    assert last_event(events)["state"] == "ready" and last_event(events)["chunks"] == CAP_CHUNKS
    assert len(world.repo.put_version_calls) == 1
    assert float(fields_of(stage_lines(caplog)[0])["embed_s"]) == pytest.approx(cap_job_seconds(share), abs=1e-3)


def assert_too_slow_is_in_the_operator_log(caplog) -> None:
    """UC-1: a job stopped as ``too_slow`` leaves one INFO line the operator can grep, ``code=too_slow``, that names the
    workspace only by its short hash and never by the workspace id (the capability), and carries no token."""
    lines = [r for r in caplog.records if r.name == "semigraph.uploads.jobs" and r.getMessage().startswith("upload job failed ")]
    assert len(lines) == 1
    record = lines[0]
    message = record.getMessage()
    assert record.levelname == "INFO"
    assert re.fullmatch(r"upload job failed ws_hash=[0-9a-f]{12} job_id=job1 code=too_slow", message)
    assert WS not in message and "token" not in message.lower()


def test_a_cap_job_at_4_percent_is_stopped_early_as_too_slow_not_run_to_the_ceiling(world, caplog):
    """A 4% share needs 2,469 s for the cap job, over the ceiling: since the cap moved to 10,000 tokens the job stops at its third
    chunk (about 300 s in) with ``too_slow``, where it used to run to the ceiling and fail as ``timeout``."""
    world.document(chunk_texts())
    embedder = throttled(world.clock, constant_share(0.04))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(embedder)

    assert last_event(events)["error"]["code"] == "too_slow"
    assert embedder.calls == 3 and world.clock.now < DEFAULT_CEILING / 2     # stopped with chunks remaining, long before the ceiling
    assert world.repo.put_version_calls == []
    assert_too_slow_is_in_the_operator_log(caplog)


def test_a_cap_job_at_6_percent_reaches_ready_now_that_the_cap_is_10000(world):
    """1,646 s of embedding: what the 16,000-token cap could not do below a 7.5% share, the 10,000-token cap does."""
    world.document(chunk_texts())
    assert last_event(world.run(throttled(world.clock, constant_share(0.06))))["state"] == "ready"


def test_a_cap_job_at_10_percent_is_stopped_early_under_a_ceiling_below_its_need(world):
    """The cap job needs 988 s at a 10% share: under a 900 s ceiling it is stopped as ``too_slow`` at its third chunk."""
    world.document(chunk_texts())
    embedder = throttled(world.clock, constant_share(0.10))
    events = world.run(embedder, ceiling=900)

    assert last_event(events)["error"]["code"] == "too_slow" and embedder.calls == 3


def test_a_burst_before_the_throttle_shortens_the_job_but_not_the_verdict(world, caplog, monkeypatch):
    """The shape W0 measured: a full core until second 115, then 12%. A 10,000-token job ends inside the 115 s burst, so this
    models a 16,000-token job (the cap raised for the test) to put the throttle in the middle of the run."""
    monkeypatch.setattr(JobSettings, "upload_max_tokens", 16000)
    world.document(chunk_texts(40))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(throttled(world.clock, burst_then(0.12)))

    embed_s = float(fields_of(stage_lines(caplog)[0])["embed_s"])
    assert last_event(events)["state"] == "ready"
    assert BURST_END_S < embed_s < 16000 / (FULL_CORE_TOKENS_PER_S * CLASS_FACTOR * 0.12)


def test_the_default_cap_is_sized_from_the_kept_ceiling_at_the_worst_measured_rate():
    """The derivation in config.py, now run from the ceiling: 2,100 s x 5.54 tokens/s = 11,634 >= the 10,000-token cap, which a
    steady 5.54 tokens/s embeds in 1,805 s; the embed ceilings of a day's uploads stay in proportion to the day (2,100 x 40 <=
    86,400 s)."""
    assert DEFAULT_CEILING == 2100 and DEFAULT_CAP == CAP_TOKENS == 10000
    assert math.floor(DEFAULT_CEILING * WORST_RATE) == 11_634 >= DEFAULT_CAP
    assert DEFAULT_CAP / WORST_RATE == pytest.approx(1805, abs=1)
    assert DEFAULT_CAP / WORST_RATE < DEFAULT_CEILING
    assert DEFAULT_CEILING * MAX_UPLOADS_PER_DAY <= SECONDS_PER_DAY
    assert cap_job_seconds(0.12) * 1.5 < DEFAULT_CEILING                  # the share-based margin only grew with the smaller cap


# ---------------------------------------------------------------- 2 and 3. the boundary and the last chunk


def test_a_chunk_that_lands_exactly_on_the_ceiling_does_not_stop_the_job_but_the_next_one_does(world):
    """Integer steps (exactly representable): two chunks of 1,050 s make 2,100 s, equal to the ceiling, and the job goes on
    (strict ``>``; the projection never stops chunks 1 and 2); after the third the elapsed time is 3,150 s and it stops with
    chunks remaining, as ``timeout`` (checked before the projection)."""
    world.document(chunk_texts(10, 5))
    embedder = steps(world.clock, 1050)
    events = world.run(embedder, ceiling=2100)

    assert embedder.calls == 3
    assert last_event(events) == FAILED_TIMEOUT_EVENT
    progress = [e["progress"] for e in events if e["state"] == "embedding" and e["progress"]["done"]]
    assert [p["done"] for p in progress] == [1, 2, 3] and {p["total"] for p in progress} == {10}


def test_a_final_chunk_that_crosses_the_ceiling_still_completes(world):
    """No chunk remains, so nothing is left to stop: the job is never failed after it has embedded its last chunk."""
    world.document(chunk_texts(3, 5))
    events = world.run(steps(world.clock, 1000), ceiling=2100)       # 2,000 s after two chunks, 3,000 s after the third

    assert last_event(events)["state"] == "ready"
    assert len(world.repo.put_version_calls) == 1


# ---------------------------------------------------------------- 4. S2: nothing new in a response


def test_the_ceiling_failure_event_is_one_constant_and_every_progress_event_has_exactly_three_keys(world):
    world.document(chunk_texts(10, 5))
    events = world.run(steps(world.clock, 1050), ceiling=2100)

    assert last_event(events) == FAILED_TIMEOUT_EVENT
    assert world.persisted()[-1] == FAILED_TIMEOUT_EVENT
    progress = [e["progress"] for e in events if "progress" in e]
    assert progress and all(set(p) == {"done", "total", "eta_s"} for p in progress)


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


@pytest.mark.parametrize("ceiling,expected", [(2100, "ready"), (700, "failed")], ids=["ready", "failed"])
def test_no_event_and_no_persisted_job_carries_a_stage_time_a_limit_or_a_cause(world, ceiling, expected):
    world.document(chunk_texts(4, 5), parse_s=7.0, chunk_s=3.0)
    world.repo.index_s = 5.0
    events = world.run(steps(world.clock, 400), ceiling=ceiling)

    assert last_event(events)["state"] == expected
    keys = set(_keys(json.loads(json.dumps([*events, *world.persisted()]))))
    assert [k for k in keys if k.endswith("_s") and k != "eta_s"] == []
    assert "stage" not in keys and not any("limit" in k or "share" in k or "ceiling" in k for k in keys)


# ---------------------------------------------------------------- 5. S4: no orphaned job or slot


def test_a_ceiling_stop_writes_nothing_stores_nothing_and_releases_the_slot_once(world):
    world.document(chunk_texts(10, 5))
    events = world.run(steps(world.clock, 1050), ceiling=2100)

    assert last_event(events) == FAILED_TIMEOUT_EVENT
    assert world.repo.put_version_calls == [] and world.repo.workspace_tokens == 0
    assert world.persisted()[-1]["state"] == "failed" and world.persisted()[-1]["error"]["code"] == "timeout"
    assert world.slots.released == 1 and world.reg.is_terminal(WS, "job1") is True
    assert world.slots.acquire(blocking=False) is True                  # the slot is free again, and the drain count is 0
    world.slots.release()


def test_the_drain_count_is_back_to_zero_after_a_ceiling_stop(world, fresh_drain):
    world.document(chunk_texts(10, 5))
    world.run(steps(world.clock, 1050), ceiling=2100)

    assert fresh_drain.active == 0


def test_an_embedder_that_raises_inside_the_loop_ends_internal_error_with_the_slot_released_once(world, caplog):
    world.document(chunk_texts(10, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 10, fail_on_call=3))

    assert last_event(events)["state"] == "failed" and last_event(events)["error"]["code"] == "internal_error"
    assert world.repo.put_version_calls == []
    assert world.slots.released == 1 and world.reg.is_terminal(WS, "job1") is True
    assert len(stage_lines(caplog)) == 1                                # the line is written on a raise too


# ---------------------------------------------------------------- 6. the stage line (S7)


def test_a_ready_job_writes_one_stage_line_whose_values_are_the_simulated_durations(world, caplog):
    world.document(chunk_texts(), parse_s=7.0, chunk_s=3.0)
    world.repo.index_s = 5.0
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(throttled(world.clock, constant_share(0.12)))

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert last_event(events)["state"] == "ready" and STAGE_LINE.match(line)
    assert fields["ws_hash"] == jobs.ws_hash_for_log(WS) and fields["job_id"] == "job1"
    assert float(fields["parse_s"]) == 7.0 and float(fields["chunk_s"]) == 3.0 and float(fields["index_s"]) == 5.0
    assert float(fields["compare_s"]) == 0.0                            # a first version: "not compared", no subprocess
    assert float(fields["embed_s"]) == pytest.approx(cap_job_seconds(0.12), abs=1e-3)
    assert [fields[k] for k in ("chunks", "fresh", "done", "tokens")] == [str(CAP_CHUNKS)] * 3 + [str(CAP_TOKENS)]


def test_the_stage_line_names_the_workspace_by_its_hash_and_carries_no_content(world, caplog):
    world.document(chunk_texts(5, 20))
    with caplog.at_level("DEBUG", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 1.0))

    everything = " ".join(r.getMessage() for r in caplog.records)
    assert len(stage_lines(caplog)) == 1
    assert WS not in everything and "confidential" not in everything and "secret" not in everything
    assert "raw bytes" not in everything


def test_a_second_version_that_reuses_chunks_logs_the_fresh_tokens_only(world, caplog):
    world.document(chunk_texts())
    world.run(steps(world.clock, 1.0), job_id="job0")
    world.monkeypatch.setattr(jobs, "_compare_with_previous", lambda *a, **kw: NOT_COMPARED)
    world.document(chunk_texts(changed=range(10)))                      # 10 of the 25 chunks are new text
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0), job_id="job1")

    fields = fields_of(stage_lines(caplog)[-1])
    assert last_event(events)["state"] == "ready"
    assert (fields["chunks"], fields["fresh"], fields["done"], fields["tokens"]) == (str(CAP_CHUNKS), "10", "10", "4000")


def test_a_comparison_that_takes_time_shows_in_compare_s(world, caplog):
    world.document(chunk_texts(3, 5))
    world.monkeypatch.setattr(jobs, "_compare_with_previous",
                              lambda *a, **kw: (world.clock.advance(11.0), NOT_COMPARED)[1])
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 1.0))

    assert float(fields_of(stage_lines(caplog)[0])["compare_s"]) == 11.0


def test_a_ceiling_stop_after_two_chunks_keeps_embed_s_and_logs_the_plan_not_the_embedded_tokens(world, caplog):
    world.document(chunk_texts(), chunk_s=2.0)
    embedder = steps(world.clock, 1100)                                 # 2,200 s after the second chunk: over 2,100
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(embedder, ceiling=2100)

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert last_event(events) == FAILED_TIMEOUT_EVENT and STAGE_LINE.match(line)
    assert float(fields["embed_s"]) == 2200.0 and float(fields["chunk_s"]) == 2.0
    assert (fields["chunks"], fields["fresh"], fields["done"], fields["tokens"]) == (
        str(CAP_CHUNKS), str(CAP_CHUNKS), "2", str(CAP_TOKENS))
    assert "compare_s" not in fields and "index_s" not in fields


def test_an_embedder_that_raises_on_the_third_chunk_logs_done_2_and_embed_s(world, caplog):
    world.document(chunk_texts(10, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 10, fail_on_call=3))

    fields = fields_of(stage_lines(caplog)[0])
    assert (fields["fresh"], fields["done"]) == ("10", "2") and float(fields["embed_s"]) == 20.0


def test_too_many_chunks_keeps_chunk_s_and_has_no_embed_s(world, caplog):
    world.document(chunk_texts(5, 5))

    def two_hundred_chunks(text, blocks, kind, embedder, settings):
        world.clock.advance(4.0)
        return [], [object()] * 200, []

    world.monkeypatch.setattr(jobs, "_build_units_and_chunks", two_hundred_chunks)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert last_event(events)["error"]["code"] == "too_many_chunks" and STAGE_LINE.match(line)
    assert float(fields["chunk_s"]) == 4.0 and fields["chunks"] == "200"
    assert "embed_s" not in fields and "fresh" not in fields and "compare_s" not in fields


def test_the_embedded_token_budget_exit_writes_a_line_without_embed_s(world, caplog):
    world.document(chunk_texts())
    world.repo.workspace_tokens = 40_000                               # 40,000 + 10,000 planned > 48,000
    embedder = steps(world.clock, 1.0)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(embedder)

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert last_event(events)["error"]["code"] == "workspace_quota" and embedder.calls == 0 and STAGE_LINE.match(line)
    assert (fields["chunks"], fields["fresh"], fields["tokens"]) == (str(CAP_CHUNKS), str(CAP_CHUNKS), str(CAP_TOKENS))
    assert "embed_s" not in fields and "index_s" not in fields


def test_a_workspace_deleted_at_indexing_writes_a_line_with_index_s(world, caplog):
    world.document(chunk_texts(3, 5))
    world.repo.workspace_gone = True
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    [line] = stage_lines(caplog)
    assert last_event(events)["error"]["code"] == "workspace_deleted" and STAGE_LINE.match(line)
    assert "index_s" in fields_of(line) and fields_of(line)["done"] == "3"


def test_a_stage_line_that_cannot_be_formatted_never_changes_how_a_ready_job_ends(world, monkeypatch):
    """S4: the diagnostic line sits in a ``finally`` after ``emit("ready")``; if building it raised, ``_worker`` would add a
    second terminal event (``internal_error``) to a job whose version is already stored. A format that cannot be applied
    (the TypeError is raised INSIDE ``_log_stages``, so its guard is what is under test) must change nothing."""
    world.document(chunk_texts(3, 5))
    monkeypatch.setattr(jobs, "_STAGE_FIELDS", (("parse_s", "%d %d"),))
    events = world.run(steps(world.clock, 1.0))

    assert [e["state"] for e in events].count("failed") == 0 and last_event(events)["state"] == "ready"
    assert world.persisted()[-1]["state"] == "ready" and len(world.repo.put_version_calls) == 1
    assert world.slots.released == 1


def _parse_error(*args, **kwargs):
    raise ParseError("parse_failed", "internal detail", exc_type="PdfReadError")


def _parse_crash(*args, **kwargs):
    raise RuntimeError("the parser crashed")


@pytest.mark.parametrize("case", ["parse_failed", "too_many_pages", "too_many_tokens", "page_quota", "parse_raises"])
def test_a_job_that_does_not_get_past_the_parse_stage_writes_no_stage_line(world, caplog, case):
    """A parse failure, ``too_many_pages``, ``too_many_tokens``, the PAGE quota (``workspace_quota`` from the parse stage) and
    an exception inside the parse stage write only the failure lines that exist today."""
    texts = chunk_texts(CAP_CHUNKS + 1) if case == "too_many_tokens" else chunk_texts(3, 5)    # 10,400 words: over the 10,000 cap
    world.document(texts, pages=31 if case == "too_many_pages" else 5,
                   parse={"parse_failed": _parse_error, "parse_raises": _parse_crash}.get(case))
    if case == "page_quota":
        world.repo.workspace_pages = JobSettings.upload_max_workspace_pages     # no room for another page
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "failed" and stage_lines(caplog) == []
    assert world.slots.released == 1


# ---------------------------------------------------------------- the stage record, below the job


def test_embed_chunks_called_without_a_stage_record_keeps_a_local_one(world):
    """Existing positional callers are unchanged: ``clock`` and ``stages`` are keyword-only and default."""
    texts = chunk_texts(2, 5)
    chunks = [Chunk(seq=i, char_start=0, char_end=1, tokens=5) for i in range(2)]
    result = jobs._embed_chunks(object(), WS, DOC, texts, [content_hash(t) for t in texts], chunks,
                                steps(world.clock, 1.0), JobSettings(2100), lambda *a, **kw: None)

    assert sorted(result["vectors"]) == [0, 1]


def test_embed_chunks_writes_what_it_reached_into_the_record_on_every_exit(world):
    texts = chunk_texts(5, 5)
    hashes = [content_hash(t) for t in texts]
    chunks = [Chunk(seq=i, char_start=0, char_end=1, tokens=5) for i in range(5)]
    stages: dict = {}

    outcome = jobs._embed_chunks(object(), WS, DOC, texts, hashes, chunks, steps(world.clock, 1050), JobSettings(2100),
                                 lambda *a, **kw: None, clock=world.clock, stages=stages)

    assert outcome == "timeout"                                          # the bare string still travels through the return
    assert stages == {"fresh": 5, "tokens": 25, "done": 3, "embed_s": 3150.0}


# ---------------------------------------------------------------- S1: no file I/O


def test_the_embed_loop_and_the_stage_line_open_no_file_but_the_cgroup_constant(world, caplog):
    import semigraph.retrieval.ids  # noqa: F401 - what a job imports lazily, loaded BEFORE open() is patched
    import semigraph.retrieval.workspace  # noqa: F401
    import semigraph.uploads.versions  # noqa: F401

    world.document(chunk_texts(10, 5))
    opened: list[tuple] = []

    def refuse(*args, **kwargs):
        opened.append(args[0])
        raise OSError("this path must open no file")

    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(builtins, "open", refuse)
        events = world.run(steps(world.clock, 1050), ceiling=2100)

    # S3 (docs/specs/upload-cap-and-abort.md): the only open() is the cgroup counter path (twice: start and end), which fails here
    assert last_event(events) == FAILED_TIMEOUT_EVENT and len(stage_lines(caplog)) == 1
    assert set(opened) == {jobs.CGROUP_CPU_STAT_PATH} and "cpu_" not in stage_lines(caplog)[0]


# ---------------------------------------------------------------- 7. the sweeper age


def test_the_sweeper_age_is_parse_plus_ceiling_plus_the_stage_margin_at_the_defaults():
    s = Settings(_env_file=None)
    assert s.upload_embed_timeout_s == 2100
    assert jobs._fail_interrupted_older_than_s(s) == 90 + 2100 + 600 == 2790


def test_the_repo_default_age_covers_the_settings_derived_one_and_the_two_budgets():
    s = Settings(_env_file=None)
    assert repo.FAIL_INTERRUPTED_AFTER_S >= jobs._fail_interrupted_older_than_s(s) == 2790
    assert repo.FAIL_INTERRUPTED_AFTER_S > s.upload_parse_timeout_s + s.upload_embed_timeout_s
    assert repo.FAIL_INTERRUPTED_AFTER_S == 50 * 60
