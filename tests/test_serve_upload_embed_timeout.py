"""The upload embed time limit, OD12, and the keyword-ready split (docs/specs/m2-search.md R15, R16; was the M5a embed ceiling).

A throttled CPU is simulated with two doubles, no sleeping and no real CPU: a fake clock and an embedder whose
``encode_passages([text])`` advances that clock by ``core_seconds(text) / share(now)`` (``tokens / (85.8 x 1.18)``, the conservative
class factor, over a constant sustained share, or 1.0 until the burst ends at 115 s). A job now ends at ``keyword_ready`` (the
version stored with null vectors, the slot released, the raw bytes dropped) and its chunks go to the embedding queue
(``uploads.embed_queue``); ``World.run`` runs the job through ``jobs._worker`` and then drains that queue on the same thread with the
same clock. Since OD12 a time-out is not a failure: the unfinished chunks stay keyword-only and the job ends ``ready``. The
production validator is pinned in tests/test_upload_limits.py. Imports: ``semigraph`` modules and ``tests/serve_state_fakes.py``;
all of them are in the serve-shipped CI job, which runs this file.
"""

from __future__ import annotations

import builtins
import functools
import json
import re

import pytest
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture
from upload_queue_fakes import install

from semigraph.config import Settings
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
CAP_CHUNKS, CHUNK_TOKENS = 25, 400       # a 10,000-token document
CAP_TOKENS = CAP_CHUNKS * CHUNK_TOKENS
DEFAULT_CEILING = Settings.model_fields["upload_embed_timeout_s"].default
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
    upload_max_tokens = 160_000
    upload_max_chunk_tokens = 512
    upload_max_chunks = 600
    upload_max_workspace_tokens = 48000
    upload_max_workspace_pages = 300
    upload_guaranteed_embed_tokens = 19_900
    upload_embed_jobs_per_day = 23
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

    def embedded_chunks(self, driver, ws, document_id, text_hashes=None):
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
        self.pending = install(monkeypatch, self.repo)
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

    def run(self, embedder, *, ceiling=DEFAULT_CEILING, job_id="job1", slots=None, title=TITLE) -> list[dict]:
        """Runs one job through ``_worker`` (the thread body, called directly) on this world's clock; returns the live
        event log of the job."""
        slots = slots or CountingSlot()
        self.app = FakeApp(embedder, JobSettings(ceiling), slots)
        self.monkeypatch.setattr(jobs, "_process", functools.partial(ORIGINAL_PROCESS, clock=self.clock))
        reg = jobs.registry(self.app)
        reg.create(WS, job_id)
        slots.acquire(blocking=False)                                  # the route's acquire; the job's finally releases
        extra = {}
        jobs._worker(self.app, WS, job_id, DOC, title, [b"raw bytes"], "txt", "h" * 64, **extra)
        self.slots, self.reg = slots, reg
        self.after_keyword_ready = reg.events_from(WS, job_id, 0)[0]
        self.drain()
        return reg.events_from(WS, job_id, 0)[0]

    def drain(self) -> None:
        """The embedding queue's worker, run here on the world's clock."""
        queue = jobs.embed_queue_for(self.app)
        queue._clock = self.clock
        while queue.run_next():
            pass

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
    r"(?: done=\d+)?(?: tokens=\d+)?"
    r"(?: cpu_usage_usec_start=\d+)?(?: cpu_nr_throttled_start=\d+)?(?: cpu_throttled_usec_start=\d+)?"
    r"(?: cpu_usage_usec_end=\d+)?(?: cpu_nr_throttled_end=\d+)?(?: cpu_throttled_usec_end=\d+)?$")


def last_event(events: list[dict]) -> dict:
    return events[-1]




def job_line_and_embed_line(caplog) -> tuple[str, str]:
    lines = stage_lines(caplog)
    assert len(lines) == 2, "one line for the job (up to keyword_ready) and one for its embedding"
    return lines[0], lines[1]


# ---------------------------------------------------------------- 1. keyword-ready: the version is searchable before any vector


def test_keyword_ready_before_any_embedding(world):
    world.document(chunk_texts())
    embedder = throttled(world.clock, constant_share(0.12))
    world.run(embedder)

    states = [e["state"] for e in world.reg.events_from(WS, "job1", 0)[0]]
    assert states.index("keyword_ready") < states.index("embedding") and states[-1] == "ready"
    (call,) = world.repo.put_version_calls
    assert all(c["embedding"] is None and c["embedded"] is False for c in call["chunks"]), "written with null vectors"
    assert world.pending.writes, "the vectors arrived afterwards, one chunk at a time"


def start_job(world, embedder, box):
    """Runs the job body up to the hand-off to the queue (no draining); returns the counting slot."""
    slots = CountingSlot()
    world.app = FakeApp(embedder, JobSettings(DEFAULT_CEILING), slots)
    world.monkeypatch.setattr(jobs, "_process", functools.partial(ORIGINAL_PROCESS, clock=world.clock))
    jobs.registry(world.app).create(WS, "job1")
    slots.acquire(blocking=False)
    jobs._worker(world.app, WS, "job1", DOC, TITLE, box, "txt", "h" * 64)
    return slots


def test_the_slot_is_released_at_keyword_ready_exactly_once_and_a_second_upload_is_not_busy(world):
    world.document(chunk_texts(3, 5))
    embedder = steps(world.clock, 1.0)
    slots = start_job(world, embedder, [b"raw"])

    assert slots.released == 1 and embedder.calls == 0, "released before the embedding started"
    assert slots.acquire(blocking=False) is True                         # a second upload is accepted now
    slots.release()
    world.drain()
    assert slots.released == 2                                           # the job's one release, plus the second upload's above


def test_the_raw_bytes_are_dropped_after_the_parse(world):
    world.document(chunk_texts(3, 5))
    box = [b"x" * 1000]
    start_job(world, steps(world.clock, 1.0), box)
    assert box == [], "the job popped the bytes; nothing holds the up-to-40 MiB body once the parse is done"


def test_compare_timeout_writes_version_not_compared(world):
    world.document(chunk_texts(3, 5))
    world.repo.versions[DOC] = [1]                                        # a previous version exists: the job compares
    world.monkeypatch.setattr(jobs, "_compare_with_previous", lambda *a, **kw: {
        **NOT_COMPARED, "not_compared_reason": "comparison_timeout", "added": [], "removed": [], "changed": [],
        "minor_rewordings": [], "unchanged_count": 0, "negation_check_skipped": 0})
    events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "ready" and last_event(events)["not_compared_reason"] == "comparison_timeout"
    assert len(world.repo.put_version_calls) == 1


# ---------------------------------------------------------------- 2. the cap job under the throttle


@pytest.mark.parametrize("share", [0.17, 0.12, 0.10, 0.08, 0.04])
def test_a_10000_token_job_is_fully_embedded_under_the_3600_second_limit_from_the_start_of_the_throttle(world, caplog, share):
    world.document(chunk_texts())
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"):
        events = world.run(throttled(world.clock, constant_share(share)))

    assert last_event(events)["state"] == "ready" and last_event(events)["stopped"] is None
    assert last_event(events)["embedded_chunks"] == CAP_CHUNKS and last_event(events)["search"] == "keyword_semantic"
    _, embed_line = job_line_and_embed_line(caplog)
    assert float(fields_of(embed_line)["embed_s"]) == pytest.approx(cap_job_seconds(share), abs=1e-3)


def test_a_very_slow_machine_ends_with_the_unfinished_chunks_keyword_only_not_a_failed_upload(world):
    """OD12: 1% of a core needs about 9,900 s for the 10,000 tokens; the 3,600 s limit stops the loop with chunks remaining."""
    world.document(chunk_texts())
    events = world.run(throttled(world.clock, constant_share(0.01)))

    last = last_event(events)
    assert last["state"] == "ready" and last["stopped"] == "timeout" and "failed" not in [e["state"] for e in events]
    assert 0 < last["embedded_chunks"] < CAP_CHUNKS and last["embedded_chunks"] + last["keyword_only_chunks"] == CAP_CHUNKS
    assert len(world.repo.put_version_calls) == 1 and world.pending.finished == [(DOC, 1)]


def test_a_chunk_that_lands_exactly_on_the_limit_does_not_stop_the_job_but_the_next_one_does(world):
    world.document(chunk_texts(10, 5))
    embedder = steps(world.clock, 1050)
    events = world.run(embedder, ceiling=2100)

    assert embedder.calls == 3 and last_event(events)["stopped"] == "timeout"       # strict ``>``: 2,100 is not over, 3,150 is


def test_a_final_chunk_that_crosses_the_limit_still_completes(world):
    world.document(chunk_texts(3, 5))
    events = world.run(steps(world.clock, 1000), ceiling=2100)
    assert last_event(events)["stopped"] is None and last_event(events)["embedded_chunks"] == 3


# ---------------------------------------------------------------- 3. S2: nothing new in a response


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_every_progress_event_has_exactly_three_keys(world):
    world.document(chunk_texts(10, 5))
    events = world.run(steps(world.clock, 1.0))
    progress = [e["progress"] for e in events if "progress" in e]
    assert progress and all(set(p) == {"done", "total", "eta_s"} for p in progress)


@pytest.mark.parametrize("ceiling", [3600, 700])
def test_no_event_and_no_persisted_job_carries_a_stage_time_a_limit_or_a_cause(world, ceiling):
    world.document(chunk_texts(4, 5), parse_s=7.0, chunk_s=3.0)
    world.repo.index_s = 5.0
    events = world.run(steps(world.clock, 400), ceiling=ceiling)

    assert last_event(events)["state"] == "ready"
    keys = set(_keys(json.loads(json.dumps([*events, *world.persisted()]))))
    assert [k for k in keys if k.endswith("_s") and k != "eta_s"] == []
    assert not any("limit" in k or "share" in k or "ceiling" in k for k in keys)


# ---------------------------------------------------------------- 4. S4: no orphaned job or slot


def test_a_time_out_keeps_the_stored_version_and_the_vectors_made_so_far_and_releases_the_slot_once(world):
    world.document(chunk_texts(10, 5))
    events = world.run(steps(world.clock, 1050), ceiling=2100)

    assert last_event(events)["stopped"] == "timeout"
    assert len(world.repo.put_version_calls) == 1 and world.repo.workspace_tokens == 15      # three 5-token chunks were embedded
    assert world.persisted()[-1]["state"] == "ready"
    assert world.slots.released == 1 and world.reg.is_terminal(WS, "job1") is True
    assert world.slots.acquire(blocking=False) is True
    world.slots.release()


def test_the_drain_count_is_back_to_zero_after_a_time_out(world, fresh_drain):
    world.document(chunk_texts(10, 5))
    world.run(steps(world.clock, 1050), ceiling=2100)
    assert fresh_drain.active == 0


def test_an_embedder_that_raises_inside_the_loop_leaves_the_upload_keyword_only_not_failed(world, caplog):
    world.document(chunk_texts(10, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"):
        events = world.run(steps(world.clock, 10, fail_on_call=3))

    last = last_event(events)
    assert last["state"] == "ready" and last["stopped"] == "embed_error" and last["embedded_chunks"] == 2
    assert len(world.repo.put_version_calls) == 1 and world.slots.released == 1
    assert len(stage_lines(caplog)) == 2                                  # both lines are written on a raise too


# ---------------------------------------------------------------- 5. the stage lines (S7)


def test_a_ready_job_writes_a_job_line_and_an_embed_line_whose_values_are_the_simulated_durations(world, caplog):
    world.document(chunk_texts(), parse_s=7.0, chunk_s=3.0)
    world.repo.index_s = 5.0
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"):
        events = world.run(throttled(world.clock, constant_share(0.12)))

    job_line, embed_line = job_line_and_embed_line(caplog)
    job, embed = fields_of(job_line), fields_of(embed_line)
    assert last_event(events)["state"] == "ready" and STAGE_LINE.match(job_line) and STAGE_LINE.match(embed_line)
    assert job["ws_hash"] == jobs.ws_hash_for_log(WS) and job["job_id"] == "job1"
    assert float(job["parse_s"]) == 7.0 and float(job["chunk_s"]) == 3.0 and float(job["index_s"]) == 5.0
    assert float(job["compare_s"]) == 0.0 and "embed_s" not in job          # a first version: "not compared", no subprocess
    assert float(embed["embed_s"]) == pytest.approx(cap_job_seconds(0.12), abs=1e-3)
    assert [embed[k] for k in ("fresh", "done", "tokens")] == [str(CAP_CHUNKS)] * 2 + [str(CAP_TOKENS)]


def test_the_stage_lines_name_the_workspace_by_its_hash_and_carry_no_content(world, caplog):
    world.document(chunk_texts(5, 20))
    with caplog.at_level("DEBUG"):
        world.run(steps(world.clock, 1.0))

    everything = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("semigraph.uploads"))
    assert len(stage_lines(caplog)) == 2
    assert WS not in everything and "confidential" not in everything and "secret" not in everything
    assert "raw bytes" not in everything


def test_a_second_version_that_reuses_chunks_embeds_the_fresh_ones_only(world, caplog):
    world.document(chunk_texts())
    world.run(steps(world.clock, 1.0), job_id="job0")
    world.monkeypatch.setattr(jobs, "_compare_with_previous", lambda *a, **kw: NOT_COMPARED)
    world.document(chunk_texts(changed=range(10)))                      # 10 of the 25 chunks are new text
    caplog.clear()
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"):
        events = world.run(steps(world.clock, 1.0), job_id="job1")

    job_line, embed_line = stage_lines(caplog)[-2:]
    assert last_event(events)["state"] == "ready" and last_event(events)["embedded_chunks"] == 10
    assert (fields_of(job_line)["chunks"], fields_of(job_line)["fresh"], fields_of(job_line)["tokens"]) == (
        str(CAP_CHUNKS), "10", "4000")
    assert (fields_of(embed_line)["fresh"], fields_of(embed_line)["done"], fields_of(embed_line)["tokens"]) == ("10", "10", "4000")


def test_a_comparison_that_takes_time_shows_in_compare_s(world, caplog):
    world.document(chunk_texts(3, 5))
    world.monkeypatch.setattr(jobs, "_compare_with_previous",
                              lambda *a, **kw: (world.clock.advance(11.0), NOT_COMPARED)[1])
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 1.0))

    assert float(fields_of(stage_lines(caplog)[0])["compare_s"]) == 11.0


def test_an_embedder_that_raises_on_the_third_chunk_logs_done_2_and_embed_s(world, caplog):
    world.document(chunk_texts(10, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"):
        world.run(steps(world.clock, 10, fail_on_call=3))

    fields = fields_of(stage_lines(caplog)[1])
    assert (fields["fresh"], fields["done"]) == ("10", "2") and float(fields["embed_s"]) == 20.0


def test_too_many_chunks_keeps_chunk_s_and_has_no_embed_s(world, caplog):
    world.document(chunk_texts(5, 5))

    def too_many(text, blocks, kind, embedder, settings):
        world.clock.advance(4.0)
        return [], [object()] * 700, []

    world.monkeypatch.setattr(jobs, "_build_units_and_chunks", too_many)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert last_event(events)["error"]["code"] == "too_many_chunks" and STAGE_LINE.match(line)
    assert float(fields["chunk_s"]) == 4.0 and fields["chunks"] == "700"
    assert "embed_s" not in fields and "fresh" not in fields and "compare_s" not in fields
    assert world.repo.put_version_calls == []


def test_a_full_workspace_budget_embeds_nothing_and_the_upload_is_still_ready(world):
    world.document(chunk_texts())
    world.repo.workspace_tokens = 48_000                               # no room for a single chunk
    embedder = steps(world.clock, 1.0)
    events = world.run(embedder)

    assert last_event(events)["state"] == "ready" and last_event(events)["stopped"] == "budget"
    assert embedder.calls == 0 and last_event(events)["search"] == "keyword_only"
    assert len(world.repo.put_version_calls) == 1


def test_a_workspace_deleted_at_indexing_writes_a_line_with_index_s(world, caplog):
    world.document(chunk_texts(3, 5))
    world.repo.workspace_gone = True
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    [line] = stage_lines(caplog)
    assert last_event(events)["error"]["code"] == "workspace_deleted" and STAGE_LINE.match(line)
    assert "index_s" in fields_of(line) and fields_of(line)["fresh"] == "3"


def test_a_stage_line_that_cannot_be_formatted_never_changes_how_a_ready_job_ends(world, monkeypatch):
    """S4: building the diagnostic line must never turn a stored job into ``internal_error``."""
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
    texts = chunk_texts(3, 5)
    if case == "too_many_tokens":
        world.monkeypatch.setattr(JobSettings, "upload_max_tokens", 100, raising=False)
        texts = chunk_texts(3, 50)                                      # 150 words, over a 100-token cap
    world.document(texts, pages=301 if case == "too_many_pages" else 5,
                   parse={"parse_failed": _parse_error, "parse_raises": _parse_crash}.get(case))
    if case == "page_quota":
        world.repo.workspace_pages = JobSettings.upload_max_workspace_pages     # no room for another page
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "failed" and stage_lines(caplog) == []
    assert world.slots.released == 1


def test_char_precheck_before_tokenizing(world):
    """R14: a text far past the token cap is refused by a character count; ``count_tokens`` is never called."""
    world.monkeypatch.setattr(JobSettings, "upload_max_tokens", 100, raising=False)
    world.document(["w " * 5000])                                       # 10,000 characters > 100 x 8
    embedder = steps(world.clock, 1.0)

    def never(text):
        raise AssertionError("the tokenizer must not run")

    embedder.count_tokens = never
    events = world.run(embedder)
    assert last_event(events)["error"]["code"] == "too_many_tokens" and world.repo.put_version_calls == []


# ---------------------------------------------------------------- S1: no file I/O


def test_the_embed_loop_and_the_stage_lines_open_no_file_but_the_cgroup_constant(world, caplog):
    import semigraph.retrieval.ids  # noqa: F401 - what a job imports lazily, loaded BEFORE open() is patched
    import semigraph.retrieval.workspace  # noqa: F401
    import semigraph.uploads.embed_queue  # noqa: F401
    import semigraph.uploads.sections  # noqa: F401
    import semigraph.uploads.versions  # noqa: F401

    world.document(chunk_texts(10, 5))
    opened: list[tuple] = []

    def refuse(*args, **kwargs):
        opened.append(args[0])
        raise OSError("this path must open no file")

    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"), caplog.at_level("INFO", logger="semigraph.uploads.embed_queue"), \
            pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(builtins, "open", refuse)
        events = world.run(steps(world.clock, 1050), ceiling=2100)

    assert last_event(events)["stopped"] == "timeout" and len(stage_lines(caplog)) == 2
    assert set(opened) == {jobs.CGROUP_CPU_STAT_PATH} and "cpu_" not in " ".join(stage_lines(caplog))


# ---------------------------------------------------------------- 6. the sweeper age


def test_the_sweeper_age_is_parse_plus_limit_plus_the_stage_margin_at_the_defaults():
    s = Settings(_env_file=None)
    assert s.upload_embed_timeout_s == 3600
    assert jobs._fail_interrupted_older_than_s(s) == 90 + 3600 + 600 == 4290


def test_the_repo_default_age_covers_the_settings_derived_one_and_the_two_budgets():
    s = Settings(_env_file=None)
    assert repo.FAIL_INTERRUPTED_AFTER_S >= jobs._fail_interrupted_older_than_s(s) == 4290
    assert repo.FAIL_INTERRUPTED_AFTER_S > s.upload_parse_timeout_s + s.upload_embed_timeout_s
    assert repo.FAIL_INTERRUPTED_AFTER_S == 75 * 60
