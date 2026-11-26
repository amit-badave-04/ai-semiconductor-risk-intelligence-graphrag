"""The upload stage lines' CPU counters and the daily-upload store (docs/specs/upload-cap-and-abort.md, amended by OD12).

Since OD12 (docs/specs/m2-search.md) a time-out is not a failure and the projected-finish abort and its daily-count refund are gone:
the limits that replaced them (the 160,000-token cap, the guaranteed tokens, the day's embedding jobs) are pinned in
tests/test_upload_limits.py and tests/test_upload_embed_queue.py. What stays here: the cgroup counters on the embed stage line
(requirement 4, S3), and the store's ``reserve_daily_upload`` / ``release_daily_upload`` / ``today`` / ``ledger_summary`` tests, whose
functions are unchanged. Same doubles as tests/test_serve_upload_embed_timeout.py, imported from there. Nothing sleeps, no real CPU,
no Neo4j.
"""

from __future__ import annotations

import builtins
import dataclasses
import inspect
import json
import re

import pytest
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture
from test_serve_upload_embed_timeout import (  # noqa: F401 - helpers and the autouse fixtures of the harness
    DOC,
    WS,
    FakeEmbedder,
    World,
    chunk_texts,
    clean_environment,
    fields_of,
    last_event,
    no_cgroup_file,
    stage_lines,
    steps,
)

from semigraph.serve import store
from semigraph.uploads import jobs

DAY = "2026-11-06"
NEXT_DAY = "2026-11-07"


@pytest.fixture
def world(monkeypatch, fresh_drain):  # noqa: F811 - the fixture, requested by name
    return World(monkeypatch)




# ---------------------------------------------------------------- the store: release_daily_upload (query shape; the semantics run in Neo4j)


class FakeCypher:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, driver, query, **params):
        self.calls.append((query, params))
        return self.rows


def test_release_gives_the_named_day_back_and_says_whether_it_did(monkeypatch):
    fake = FakeCypher([{"n": 3}])
    monkeypatch.setattr(store, "run_cypher", fake)
    assert store.release_daily_upload(object(), DAY, "job-1") is True
    (_, params), = fake.calls
    assert params["day"] == DAY and params["job_id"] == "job-1"


def test_release_with_nothing_to_give_back_is_false(monkeypatch):
    monkeypatch.setattr(store, "run_cypher", FakeCypher([]))
    assert store.release_daily_upload(object(), DAY, "job-1") is False


def test_release_matches_an_existing_day_and_never_creates_one(monkeypatch):
    fake = FakeCypher([])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.release_daily_upload(object(), DAY, "job-1")
    query = fake.calls[0][0]
    assert "MATCH (c:SvcUploadDay {day: $day})" in query and "MERGE" not in query and "CREATE" not in query


def test_release_locks_the_counter_then_guards_the_floor_and_the_job_then_decrements(monkeypatch):
    fake = FakeCypher([])
    monkeypatch.setattr(store, "run_cypher", fake)
    store.release_daily_upload(object(), DAY, "job-1")
    q = fake.calls[0][0]
    lock, floor, once, dec = (q.index(s) for s in ("SET c._lock = true", "c.n > 0", "NOT $job_id IN coalesce(c.refunded, [])",
                                                   "c.n = c.n - 1"))
    assert lock < floor < dec and lock < once < dec
    assert "c.refunded = coalesce(c.refunded, []) + $job_id" in q       # the job is recorded in the same statement


@pytest.mark.parametrize("day,job_id", [("", "j"), ("2026-13-45", "j"), ("tomorrow", "j"), ("2026-11-06 ", "j"),
                                        ("2026-1-6", "j"), ("20261106", "j"), ("2026-W45-5", "j"), ("2026-02-30", "j"),
                                        (DAY, ""), (None, "j"), (DAY, None), (DAY, 5)])
def test_release_refuses_a_malformed_day_or_job_id_before_any_query(monkeypatch, day, job_id):
    fake = FakeCypher([{"n": 1}])
    monkeypatch.setattr(store, "run_cypher", fake)
    with pytest.raises(ValueError):
        store.release_daily_upload(object(), day, job_id)
    assert fake.calls == []


def test_reserve_takes_an_explicit_day_and_defaults_to_today(monkeypatch):
    fake = FakeCypher([{"n": 1}])
    monkeypatch.setattr(store, "run_cypher", fake)
    assert store.reserve_daily_upload(object(), 40, day=DAY) is True
    assert store.reserve_daily_upload(object(), 40) is True
    assert fake.calls[0][1]["day"] == DAY and fake.calls[1][1]["day"] == store.today()




# ---------------------------------------------------------------- the stage line


CPU_START = "usage_usec 1000\nuser_usec 600\nsystem_usec 400\nnr_periods 50\nnr_throttled 3\nthrottled_usec 777\n"
CPU_END = "usage_usec 2500\nuser_usec 1600\nsystem_usec 900\nnr_periods 90\nnr_throttled 11\nthrottled_usec 4242\n"

LINE = re.compile(
    r"^upload stages ws_hash=[0-9a-f]{12} job_id=[0-9a-z]+(?: parse_s=\d+\.\d+)?(?: chunk_s=\d+\.\d+)?"
    r"(?: embed_s=\d+\.\d+)?(?: compare_s=\d+\.\d+)?(?: index_s=\d+\.\d+)?(?: chunks=\d+)?(?: fresh=\d+)?"
    r"(?: done=\d+)?(?: tokens=\d+)?"
    r"(?: cpu_usage_usec_start=\d+)?(?: cpu_nr_throttled_start=\d+)?(?: cpu_throttled_usec_start=\d+)?"
    r"(?: cpu_usage_usec_end=\d+)?(?: cpu_nr_throttled_end=\d+)?(?: cpu_throttled_usec_end=\d+)?$")


class CpuEmbedder(FakeEmbedder):
    """Rewrites the fake cpu.stat on its second call, so the end reading differs from the start."""

    def __init__(self, clock, path, end_text):
        super().__init__(clock, lambda text: 1.0)
        self.path, self.end_text = path, end_text

    def encode_passages(self, texts):
        if self.calls == 1:
            self.path.write_text(self.end_text)
        return super().encode_passages(texts)


def test_the_cgroup_counters_appear_at_the_start_and_the_end_of_the_embed(world, caplog, monkeypatch, tmp_path):
    path = tmp_path / "cpu.stat"
    path.write_text(CPU_START)
    monkeypatch.setattr(jobs, "CGROUP_CPU_STAT_PATH", str(path))
    world.document(chunk_texts(4, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(CpuEmbedder(world.clock, path, CPU_END))

    line = stage_lines(caplog)[1]                       # the embed line carries the counters; the job line has none
    fields = fields_of(line)
    assert LINE.match(line)
    assert (fields["cpu_usage_usec_start"], fields["cpu_nr_throttled_start"], fields["cpu_throttled_usec_start"]) == (
        "1000", "3", "777")
    assert (fields["cpu_usage_usec_end"], fields["cpu_nr_throttled_end"], fields["cpu_throttled_usec_end"]) == (
        "2500", "11", "4242")


def test_without_a_cgroup_file_no_counter_is_written_and_the_job_is_unaffected(world, caplog):
    world.document(chunk_texts(4, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    line = stage_lines(caplog)[1]                       # the embed line carries the counters; the job line has none
    assert last_event(events)["state"] == "ready" and LINE.match(line) and "cpu_" not in line


def test_a_field_the_file_lacks_or_garbles_is_left_out_never_guessed(world, caplog, monkeypatch, tmp_path):
    path = tmp_path / "cpu.stat"
    path.write_text("usage_usec 1000\nnr_throttled lots\nuser_usec 5\n")        # no throttled_usec, a non-number
    monkeypatch.setattr(jobs, "CGROUP_CPU_STAT_PATH", str(path))
    world.document(chunk_texts(4, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    line = stage_lines(caplog)[1]                       # the embed line carries the counters; the job line has none
    fields = fields_of(line)
    assert last_event(events)["state"] == "ready" and LINE.match(line)
    assert fields["cpu_usage_usec_start"] == "1000" and "cpu_nr_throttled_start" not in fields
    assert "cpu_throttled_usec_start" not in fields and "user_usec" not in line


@pytest.mark.parametrize("kind", ["directory", "binary", "oserror"])
def test_an_unreadable_cgroup_file_never_fails_a_job(world, caplog, monkeypatch, tmp_path, kind):
    path = tmp_path / "cpu.stat"
    if kind == "directory":
        path.mkdir()
    elif kind == "binary":
        path.write_bytes(b"\xff\xfe\x00\x01 usage_usec")
    else:
        path.write_text(CPU_START)
        real_open = builtins.open

        def refuse(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError("not for you")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(jobs, "CGROUP_CPU_STAT_PATH", str(path))
    world.document(chunk_texts(4, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "ready" and len(stage_lines(caplog)) == 2 and world.slots.released == 1


def test_s3_the_cgroup_read_takes_no_input_and_opens_only_the_fixed_path(world, monkeypatch, tmp_path):
    assert not inspect.signature(jobs._read_cpu_stat).parameters
    monkeypatch.undo()                                                       # the constant itself, not the test double
    assert jobs.CGROUP_CPU_STAT_PATH == "/sys/fs/cgroup/cpu.stat"


def test_s3_a_job_opens_no_file_but_the_one_constant(world, monkeypatch, tmp_path):
    import semigraph.retrieval.ids  # noqa: F401 - what a job imports lazily, loaded BEFORE open() is patched
    import semigraph.retrieval.workspace  # noqa: F401
    import semigraph.uploads.embed_queue  # noqa: F401
    import semigraph.uploads.sections  # noqa: F401
    import semigraph.uploads.versions  # noqa: F401

    world.document(chunk_texts(10, 5))
    opened: list = []

    def refuse(file, *args, **kwargs):
        opened.append(file)
        raise OSError("no file may be opened here")

    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(builtins, "open", refuse)
        events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "ready"
    assert opened and set(opened) == {jobs.CGROUP_CPU_STAT_PATH}


def test_today_is_the_utc_day_not_the_local_day(monkeypatch):
    """The daily counters key on the UTC day. At 23:30 UTC a host five and a half hours ahead is already on the next local day:
    ``store.today()`` must still give the UTC one, whichever clock call (``datetime.now`` or ``date.today``) it uses."""
    import datetime as dt

    utc_now = dt.datetime(2026, 11, 6, 23, 30, tzinfo=dt.UTC)

    class LocalAheadDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return utc_now.astimezone(tz)
            return (utc_now + dt.timedelta(hours=5, minutes=30)).replace(tzinfo=None)

    class LocalAheadDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 11, 7)

    monkeypatch.setattr(store, "datetime", LocalAheadDatetime)
    monkeypatch.setattr(store, "date", LocalAheadDate)
    assert store.today() == DAY


def test_the_ledger_summary_counts_today_and_all_time_rows(monkeypatch):
    """Regression (CI neo4j-state, 2026-11-08): after ``store._today`` became ``store.today``, a local named ``today`` in
    ``ledger_summary`` shadowed it and the summary raised UnboundLocalError. Only the real-Neo4j job reached it."""
    calls = []

    def fake_run_cypher(driver, query, **params):
        calls.append(params)
        if "day" in params:
            return [{"cached": False, "n": 2, "cost": 0.5}, {"cached": True, "n": 1, "cost": None}]
        return [{"cached": False, "n": 5, "cost": 1.25}]

    monkeypatch.setattr(store, "run_cypher", fake_run_cypher)
    monkeypatch.setattr(store, "today", lambda: DAY)
    assert store.ledger_summary(object()) == {"today": {"paid": 2, "cached": 1, "cost_usd": 0.5},
                                              "all_time": {"paid": 5, "cached": 0, "cost_usd": 1.25}}
    assert calls[0] == {"day": DAY} and len(calls) == 2


def test_no_function_binds_a_local_today_while_calling_today():
    """The class of the bug above, anywhere in the package: a function that assigns a name ``today`` and calls a bare
    ``today()`` makes that call reach the unassigned local (UnboundLocalError)."""
    import ast
    import pathlib

    package = pathlib.Path(store.__file__).resolve().parents[1]
    hits = []
    for path in sorted(package.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nodes = list(ast.walk(fn))
            binds = any(isinstance(n, ast.Name) and n.id == "today" and isinstance(n.ctx, ast.Store) for n in nodes)
            calls = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "today" for n in nodes)
            if binds and calls:
                hits.append(f"{path.name}:{fn.lineno} {fn.name}")
    assert hits == []

