"""The upload cap and the projected-finish abort (docs/specs/upload-cap-and-abort.md, council 9).

Same doubles as tests/test_serve_upload_embed_timeout.py (a fake clock and an embedder that advances it; ``jobs._worker`` called
directly), imported from there. Nothing sleeps, no real CPU, no Neo4j: the daily-count refund is a recorded call here, and its
Cypher semantics (idempotent per job, never below zero) run against a real Neo4j in tests/integration/test_state_neo4j.py.

Covered: the abort (requirement 2: slow, burst-then-throttle, normal, the strict boundary, chunks 1 and 2), the refund calls
(requirement 3: only ``too_slow``, the charged day), the cap (requirement 1: 10,000 and 10,001; the production validator and its
boundary), the stage line (requirement 4), the client copy (requirement 5), and S1 to S5.
"""

from __future__ import annotations

import builtins
import dataclasses
import inspect
import json
import random
import re

import pytest
from pydantic import ValidationError
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture
from test_serve_config_production import EMBED_TIMEOUT, live, named
from test_serve_upload_embed_timeout import (  # noqa: F401 - helpers and the autouse fixtures of the harness
    BURST_END_S,
    CAP_CHUNKS,
    CAP_TOKENS,
    CHUNK_TOKENS,
    DEFAULT_CEILING,
    DOC,
    FAILED_TIMEOUT_EVENT,
    WS,
    FakeClock,
    FakeEmbedder,
    JobSettings,
    World,
    burst_then,
    chunk_texts,
    clean_environment,
    constant_share,
    fields_of,
    last_event,
    no_cgroup_file,
    stage_lines,
    steps,
    throttled,
)

from semigraph import config
from semigraph.config import Settings
from semigraph.hashing import content_hash
from semigraph.serve import store
from semigraph.uploads import jobs
from semigraph.uploads.units import Chunk

TOO_SLOW_TEXT = "Uploads are slow right now. Please try again in a few minutes, or upload a shorter PDF."
FAILED_TOO_SLOW_EVENT = {"job_id": "job1", "state": "failed", "document_id": DOC, "version": 1,
                         "error": {"code": "too_slow", "message": TOO_SLOW_TEXT}}
WORST_RATE = 5.54                        # tokens a second: the worst measured (docs/v2/M5_DECISIONS 10.11, w1d)
DAY = "2026-11-06"
NEXT_DAY = "2026-11-07"


@pytest.fixture
def world(monkeypatch, fresh_drain):  # noqa: F811 - the fixture, requested by name
    return World(monkeypatch)


@pytest.fixture
def refunds(monkeypatch):
    """Records every ``store.release_daily_upload`` call as ``(day, job_id)``; the day ``store.today`` reports is NEXT_DAY, so a
    refund that used 'today' instead of the charged day shows."""
    calls: list[tuple] = []

    def release(driver, day, job_id):
        calls.append((day, job_id))
        return True

    monkeypatch.setattr(store, "release_daily_upload", release, raising=False)
    monkeypatch.setattr(store, "today", lambda: NEXT_DAY)
    return calls


def rate(tokens_per_s: float):
    """An embedder double: a chunk of N words takes N / tokens_per_s seconds."""
    return lambda clock: FakeEmbedder(clock, lambda text: len(text.split()) / tokens_per_s)


# ---------------------------------------------------------------- 2. the abort


def test_a_slow_rate_aborts_at_the_third_chunk_with_too_slow(world, refunds):
    world.document(chunk_texts())
    embedder = throttled(world.clock, constant_share(0.03))                # 132 s a chunk: 3,300 s projected
    events = world.run(embedder, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT
    assert embedder.calls == 3                                              # stopped at once, not at the ceiling
    assert world.repo.put_version_calls == [] and world.repo.workspace_tokens == 0
    assert world.clock.now < 500                                            # minutes, not 35 of them


def test_a_burst_then_throttle_rate_aborts_mid_run(world, refunds, monkeypatch):
    monkeypatch.setattr(JobSettings, "upload_max_tokens", 100_000)         # a 40,000-token job: the burst covers 29 chunks
    world.document(chunk_texts(100))
    embedder = throttled(world.clock, burst_then(0.03))
    events = world.run(embedder, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT
    assert 29 < embedder.calls < 45 and world.clock.now > BURST_END_S       # not in the burst, not at the ceiling either


def test_a_normal_rate_completes(world, refunds):
    world.document(chunk_texts())
    events = world.run(throttled(world.clock, constant_share(0.12)), charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []


def test_the_worst_measured_rate_completes_a_cap_job(world, refunds):
    """5.54 tok/s: 10,000 tokens in 1,805 s, inside the 2,100 s ceiling. Every projection of a steady rate is 1,805 s."""
    world.document(chunk_texts())
    events = world.run(rate(WORST_RATE)(world.clock), charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []
    assert world.clock.now == pytest.approx(CAP_TOKENS / WORST_RATE)


def test_a_projection_exactly_at_the_ceiling_does_not_abort(world, refunds):
    """Exact binary steps: 10 chunks of 400 tokens at 4 s each, so every projection from the third chunk on (1,200 tokens, over the
    1,024 floor) is exactly 40 s."""
    world.document(chunk_texts(10, 400))
    events = world.run(steps(world.clock, 4), ceiling=40, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []


def test_a_projection_one_tick_over_the_ceiling_aborts(world, refunds):
    world.document(chunk_texts(10, 400))
    embedder = steps(world.clock, 4)
    events = world.run(embedder, ceiling=39, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and embedder.calls == 3


def test_the_first_two_chunks_never_abort(world, refunds):
    """Chunks of 600 tokens: after chunk 2 the 1,200 embedded tokens are over the floor and the projection is 400 s against a 200 s
    ceiling, but fewer than three chunks never abort; the third does."""
    world.document(chunk_texts(10, 600))
    embedder = steps(world.clock, 40)
    events = world.run(embedder, ceiling=200, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and embedder.calls == 3


def test_a_three_chunk_job_whose_early_projections_are_over_the_ceiling_still_completes(world, refunds):
    """Chunks of 600 tokens: chunk 2 (floor met) projects 120 s against 100 s and is skipped (two chunks); chunk 3 is the last, so
    no projection is made."""
    world.document(chunk_texts(3, 600))
    embedder = steps(world.clock, 40)
    events = world.run(embedder, ceiling=100, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and embedder.calls == 3 and refunds == []


def test_the_projection_counts_the_fresh_chunks_of_this_job_only(world, refunds):
    """A re-upload with 3 of 10 chunks already embedded plans 7 chunks (2,800 tokens): after 3 fresh chunks (12 s) the projection
    is 12 x 2,800 / 1,200 = 28 s, under a 30 s ceiling, so it completes; counting all 10 chunks (4,000 tokens) it would be 40 s."""
    texts = chunk_texts(10, 400)
    world.document(texts)
    world.repo.embedded[DOC] = {content_hash(t): [0.1] for t in texts[:3]}
    embedder = steps(world.clock, 4)
    events = world.run(embedder, ceiling=30, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and embedder.calls == 7 and refunds == []


def test_the_chunk_projection_counts_the_total_of_fresh_chunks_only(world, refunds):
    """A re-upload: 6 chunks already embedded, then 9 one-token and five 400-token fresh chunks (fixed 10 s a call plus 0.1 s a
    token, ceiling 400 s; true finish 351 s). At fresh chunk 12 the token projection is 400.3 s (past) and the chunk projection
    is 240.9 x 14 / 12 = 281 s (not past); counting the 6 embedded chunks in the total it would be 240.9 x 20 / 12 = 401.5 s and
    the job would be aborted."""
    old = texts_of([400] * 6)
    world.document(old + [f"t{i}" for i in range(9)] + chunk_texts(5, 400))
    world.repo.embedded[DOC] = {content_hash(t): [0.1] for t in old}
    embedder = fixed_cost(world.clock, 10.0, 0.1)
    events = world.run(embedder, ceiling=400, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and embedder.calls == 14 and refunds == []


def test_the_chunk_projection_counts_the_done_fresh_chunks_only(world, refunds):
    """A slow re-upload: 3 of 10 chunks already embedded, 7 fresh at 10 s each, ceiling 60 s: after 3 fresh chunks both projections
    are 70 s, so it aborts there; counting the 3 embedded chunks as done the chunk projection would be 30 x 10 / 6 = 50 s."""
    texts = chunk_texts(10, 400)
    world.document(texts)
    world.repo.embedded[DOC] = {content_hash(t): [0.1] for t in texts[:3]}
    embedder = steps(world.clock, 10)
    events = world.run(embedder, ceiling=60, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and embedder.calls == 3 and refunds == [(DAY, "job1")]


def test_a_stop_past_the_ceiling_stays_a_timeout_and_keeps_the_charge(world, refunds):
    """Elapsed is over the ceiling on chunk 1 (and, at chunk 3, both rules hold): the existing ``timeout`` is checked first."""
    world.document(chunk_texts(10, 5))
    first = steps(world.clock, 2200)
    assert last_event(world.run(first, ceiling=2100, charged_day=DAY)) == FAILED_TIMEOUT_EVENT and first.calls == 1

    world2 = World(world.monkeypatch)
    world2.document(chunk_texts(10, 5))
    third = steps(world2.clock, 1050)                                       # 2,100 s after chunk 2 (not over), 3,150 s after 3
    assert last_event(world2.run(third, ceiling=2100, charged_day=DAY)) == FAILED_TIMEOUT_EVENT and third.calls == 3
    assert refunds == []


def test_a_stalled_clock_never_aborts(world, refunds):
    """A clock that never moves projects a finish of zero seconds: the job runs to the end (the projection is multiplied out, so no
    division by the elapsed time exists to fail)."""
    world.document(chunk_texts(10, 400))
    events = world.run(steps(world.clock, 0.0), ceiling=10, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []


def test_chunks_with_no_tokens_never_abort_and_never_divide_by_zero(world, refunds):
    """The first three chunks carry no tokens (so ``embedded_tokens`` is 0 at the third) while the clock has moved: the guard
    ``embedded_tokens > 0`` skips the decision. From the fourth chunk the rate is a fair 4 s for 400 tokens, projecting 56 s once
    the floor is met, inside the 200 s ceiling."""
    chunks = world.document(chunk_texts(10, 400))
    chunks[:3] = [dataclasses.replace(c, tokens=0) for c in chunks[:3]]
    embedder = steps(world.clock, 4)
    events = world.run(embedder, ceiling=200, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and embedder.calls == 10 and refunds == []


@pytest.mark.parametrize("elapsed,total_tokens,embedded,total_chunks,done,ceiling,past", [
    (12, 80, 24, 10, 3, 40, False),    # tokens 12 x 80 = 960 = 40 x 24 and chunks 12 x 10 = 120 = 40 x 3: both at the ceiling
    (12, 81, 24, 10, 3, 40, False),    # tokens one above, chunks at: no abort
    (12, 80, 24, 11, 3, 40, False),    # tokens at, chunks one above: no abort
    (12, 81, 24, 11, 3, 40, True),     # both one above: abort
    (13, 80, 24, 10, 3, 40, True),     # a second more elapsed lifts both
    (12, 80, 24, 10, 3, 39, True),     # a ceiling a second shorter puts both above
    (12, 80, 24, 10, 3, 41, False),
    (12, 80, 25, 10, 3, 40, False),    # a token more embedded already: the token form is under, so no abort
    (12, 80, 24, 10, 4, 40, False),    # a chunk more done already: the chunk form is under, so no abort
    (12, 200, 24, 10, 3, 40, False),   # tokens far over (tiny leading chunks) but chunks exactly at the ceiling: no abort
    (12, 80, 24, 40, 3, 40, False)])   # chunks far over (a few huge chunks then many small) but tokens at the ceiling: no abort
def test_the_abort_decision_needs_both_projections_strictly_past_the_ceiling(elapsed, total_tokens, embedded, total_chunks, done,
                                                                             ceiling, past):
    assert jobs._projects_past_ceiling(elapsed, total_tokens, embedded, total_chunks, done, ceiling) is past


@pytest.mark.parametrize("total_tokens,total_chunks,projected", [
    (80, 10, 40.0),     # both 40 s
    (100, 10, 40.0),    # tokens 50 s, chunks 40 s: the smaller
    (80, 20, 40.0)])    # tokens 40 s, chunks 80 s: the smaller
def test_the_deciding_projection_is_the_smaller_of_the_two(total_tokens, total_chunks, projected):
    assert jobs._deciding_projection(12, total_tokens, 24, total_chunks, 3) == projected


@pytest.mark.parametrize("done,embedded,max_chunk,may", [
    (3, 1024, 512, True),        # exactly at the floor (2 x 512) with three chunks: projects
    (3, 1023, 512, False),       # one token below the floor: does not
    (3, 1025, 512, True),
    (2, 5000, 512, False),       # fewer than three chunks never project, whatever the tokens
    (1, 5000, 512, False),
    (4, 1023, 512, False),       # the floor counts tokens, not chunks
    (3, 200, 100, True),         # the floor follows the setting: 2 x 100
    (3, 199, 100, False),
    (3, 0, 0, False)])           # no tokens: never (a floor of 0 does not let an empty run project)
def test_the_projection_starts_at_three_chunks_and_two_chunk_sizes_of_tokens(done, embedded, max_chunk, may):
    assert jobs._may_project(done, embedded, max_chunk) is may


def test_the_floor_is_two_max_chunks_of_the_setting_and_1024_at_the_default():
    assert JobSettings.upload_max_chunk_tokens == 512 == Settings.model_fields["upload_max_chunk_tokens"].default
    assert jobs.MIN_CHUNKS_BEFORE_ABORT == 3 and jobs.MIN_CHUNKS_WORTH_OF_TOKENS_BEFORE_ABORT * 512 == 1024


def texts_of(sizes) -> list[str]:
    """One distinct text per size, each ``size`` words (the embedder double and the chunk table both count words)."""
    return [f"s{i}x " + " ".join(["w"] * (n - 1)) for i, n in enumerate(sizes)]


@pytest.mark.parametrize("sizes,calls", [
    ([400, 400, 224] + [400] * 7, 3),      # 1,024 tokens at chunk 3: exactly the floor, aborts there
    ([400, 400, 223] + [400] * 7, 4),      # 1,023: one below, the decision waits for chunk 4
    ([300] * 10, 4),                       # 900 at chunk 3, 1,200 at chunk 4
    ([341] * 10, 4),                       # 1,023 at chunk 3
    ([342] * 10, 3),                       # 1,026 at chunk 3
    ([600] * 10, 3)])                      # floor met at chunk 2, three chunks still required
def test_a_slow_rate_with_large_chunks_aborts_as_soon_as_the_floor_is_reached(world, refunds, sizes, calls):
    """10 s a chunk against a 60 s ceiling: every projection is far over it, so the abort lands on the first allowed chunk."""
    world.document(texts_of(sizes))
    embedder = steps(world.clock, 10)
    events = world.run(embedder, ceiling=60, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and embedder.calls == calls and refunds == [(DAY, "job1")]


def fixed_cost(clock, call_s: float, token_s: float) -> FakeEmbedder:
    """An embedder double whose every encoder call costs ``call_s`` seconds plus ``token_s`` a token (a real encoder has a fixed
    per-call cost, which makes a run of tiny chunks look slow)."""
    return FakeEmbedder(clock, lambda text: call_s + len(text.split()) * token_s)


def test_tiny_leading_fresh_chunks_with_a_fixed_per_call_cost_do_not_abort_a_normal_rate(world, refunds):
    """The finding: 6 one-token chunks (10.1 s each), then 5 chunks of 400 tokens (50 s each) at a rate that finishes in 310.6 s,
    well inside the 400 s ceiling. Token-weighted from the third chunk the job looked like 3 tokens in 30 s (a projection of
    about 20,000 s) and was aborted as too_slow; with the 1,024-token floor it is not projected until 3 large chunks are in."""
    world.document([f"t{i}" for i in range(6)] + chunk_texts(5, 400))
    embedder = fixed_cost(world.clock, 10.0, 0.1)
    events = world.run(embedder, ceiling=400, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []
    assert embedder.calls == 11 and world.clock.now == pytest.approx(310.6)


@pytest.mark.parametrize("tiny", range(9, 16))
def test_n_tiny_leading_fresh_chunks_then_five_large_ones_finish_ready_with_no_refund(world, refunds, tiny):
    """The verifier's reproduction of the residual false abort (token-weighted rule plus the floor only): ``tiny`` 1-token fresh
    chunks (10.1 s each), then five 400-token chunks (50 s each), 10 s a call plus 0.1 s a token, ceiling 400 s. The true finish
    is 10 x (tiny + 5) + 0.1 x (tiny + 2,000) = 250 + 10.1 x tiny s: under 400 s up to N = 14 (401.5 s at N = 15, still ready: the timeout is
    only checked while chunks remain). The token-weighted
    projection alone aborted it as too_slow for N = 9..15 (the chunk-weighted projection does not)."""
    world.document([f"t{i}" for i in range(tiny)] + chunk_texts(5, 400))
    embedder = fixed_cost(world.clock, 10.0, 0.1)
    events = world.run(embedder, ceiling=400, charged_day=DAY)

    assert last_event(events)["state"] == "ready" and refunds == []
    assert embedder.calls == tiny + 5 and world.clock.now == pytest.approx(250 + 10.1 * tiny)


# The property over the model ``time = c x calls + t x tokens`` (c per encoder call, t per token), whole numbers so every
# projection is exact. Chunk-size sequences: tiny-first, large-first, mixed (seeded) and uniform.
PROPERTY_MAX_CHUNK = 512
PROPERTY_COSTS = [(c, t) for c in (0, 1, 3, 10, 50, 200) for t in (0, 1, 2, 3, 7)]


def size_sequences() -> list[list[int]]:
    rng = random.Random(20261108)
    tiny_first = [[1] * a + [400] * b for a in (0, 1, 3, 9, 15, 40) for b in (3, 5, 8)]
    large_first = [[400] * b + [1] * a for a in (1, 3, 9, 15, 40) for b in (3, 5, 8)]
    mixed = [[rng.choice((0, 1, 2, 5, 50, 200, 400, 512)) for _ in range(rng.randint(4, 30))] for _ in range(60)]
    uniform = [[s] * n for s in (1, 100, 400, 512) for n in (4, 10, 25)]
    return tiny_first + large_first + mixed + uniform


def decisions(sizes: list[int], c: int, t: int, ceiling: int, *, token_only=False, chunk_only=False) -> list[tuple[int, bool]]:
    """``(chunks done, would the job stop)`` at every projection point the floor gate allows, under the model."""
    total_tokens, total_chunks, out, embedded = sum(sizes), len(sizes), [], 0
    for done in range(1, total_chunks):                                     # the last chunk has nothing remaining
        embedded += sizes[done - 1]
        if not jobs._may_project(done, embedded, PROPERTY_MAX_CHUNK):
            continue
        elapsed = c * done + t * embedded
        if token_only:
            fired = elapsed * total_tokens > ceiling * embedded
        elif chunk_only:
            fired = elapsed * total_chunks > ceiling * done
        else:
            fired = jobs._projects_past_ceiling(elapsed, total_tokens, embedded, total_chunks, done, ceiling)
        out.append((done, fired))
    return out


def test_property_the_abort_never_fires_when_the_true_finish_is_within_the_ceiling():
    cases = points = 0
    for sizes in size_sequences():
        for c, t in PROPERTY_COSTS:
            finish = c * len(sizes) + t * sum(sizes)
            for ceiling in (finish, finish + 1, finish + finish // 2 + 1, finish * 2 + 5):
                cases += 1
                for done, fired in decisions(sizes, c, t, ceiling):
                    points += 1
                    assert not fired, (sizes, c, t, ceiling, done)
    assert cases > 10_000 and points > 50_000


def test_property_each_projection_alone_does_fire_falsely_so_both_are_needed():
    """The grid is not vacuous: each single form would stop a job that finishes within the ceiling (token form: tiny chunks
    first; chunk form: large chunks first), and the pair never does."""
    token_false = chunk_false = 0
    for sizes in size_sequences():
        for c, t in PROPERTY_COSTS:
            finish = c * len(sizes) + t * sum(sizes)
            token_false += any(f for _, f in decisions(sizes, c, t, finish, token_only=True))
            chunk_false += any(f for _, f in decisions(sizes, c, t, finish, chunk_only=True))
    assert token_false > 100 and chunk_false > 100


@pytest.mark.parametrize("size", [400, 512, 600])
def test_property_uniform_chunks_whose_true_finish_is_past_the_ceiling_stop_at_the_first_allowed_point(size):
    stops = 0
    for c, t in PROPERTY_COSTS:
        sizes = [size] * 20
        finish = c * 20 + t * 20 * size
        if finish == 0:
            continue
        for ceiling in (finish - 1, finish // 2, finish // 10):
            points = decisions(sizes, c, t, ceiling)
            assert points[0] == (3, True), (size, c, t, ceiling)            # three chunks, over 1,024 tokens: the first point
            stops += 1
    assert stops == 3 * (len(PROPERTY_COSTS) - 1)


# ---------------------------------------------------------------- 3. the refund


def test_a_too_slow_job_refunds_the_charged_day_once(world, refunds):
    world.document(chunk_texts(10, 400))
    events = world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT
    assert refunds == [(DAY, "job1")]


def test_a_job_charged_before_midnight_that_aborts_after_it_refunds_the_day_it_was_charged(world, refunds):
    """``store.today`` says NEXT_DAY (the fixture): the refund still names DAY, the day the route charged."""
    world.document(chunk_texts(10, 400))
    world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    assert [day for day, _ in refunds] == [DAY] and store.today() == NEXT_DAY


@pytest.mark.parametrize("case", ["timeout", "parse_failed", "too_many_pages", "too_many_tokens", "too_many_chunks",
                                  "workspace_quota", "internal_error", "workspace_deleted", "ready"])
def test_no_other_ending_gives_the_charge_back(world, refunds, case):
    from semigraph.uploads.parse import ParseError

    texts = chunk_texts(10, 5)
    parse = None
    embedder = steps(world.clock, 1.0)
    if case == "parse_failed":
        def parse(*a, **kw):
            raise ParseError("parse_failed", "x", exc_type="E")
    elif case == "too_many_tokens":
        texts = chunk_texts(25, 401)
    elif case == "internal_error":
        embedder = steps(world.clock, 1.0, fail_on_call=2)
    elif case == "timeout":
        embedder = steps(world.clock, 2200)
    world.document(texts, pages=31 if case == "too_many_pages" else 1, parse=parse)
    if case == "too_many_chunks":
        world.monkeypatch.setattr(jobs, "_build_units_and_chunks", lambda *a, **kw: ([], [object()] * 200, []))
    if case == "workspace_quota":
        world.repo.workspace_tokens = 48_000
    if case == "workspace_deleted":
        world.repo.workspace_gone = True
    events = world.run(embedder, charged_day=DAY)

    assert last_event(events)["state"] == ("ready" if case == "ready" else "failed")
    assert refunds == []


def test_a_refund_that_raises_never_changes_how_the_job_ends(world, monkeypatch, caplog):
    def explode(driver, day, job_id):
        raise RuntimeError("neo4j is down")

    monkeypatch.setattr(store, "release_daily_upload", explode, raising=False)
    world.document(chunk_texts(10, 400))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    assert last_event(events) == FAILED_TOO_SLOW_EVENT
    assert [e["state"] for e in events].count("failed") == 1 and world.slots.released == 1
    assert "neo4j is down" not in " ".join(r.getMessage() for r in caplog.records)


def test_a_job_without_a_charged_day_asks_for_no_refund_and_still_ends_too_slow(world, refunds):
    world.document(chunk_texts(10, 400))
    events = world.run(steps(world.clock, 4), ceiling=39)                  # no charged_day: a caller that never charged

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and refunds == []


def test_the_refund_is_made_even_when_the_workspace_vanishes_during_the_failure_write(world, refunds, monkeypatch):
    from semigraph.uploads import repo

    def gone_on_failed(driver, ws, job):
        if job["state"] == "failed":
            raise repo.WorkspaceGone(ws)

    monkeypatch.setattr(repo, "put_job", gone_on_failed)
    world.document(chunk_texts(10, 400))
    events = world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    assert last_event(events)["error"]["code"] == "workspace_deleted"
    assert refunds == [(DAY, "job1")]


def test_run_upload_job_hands_the_charged_day_and_a_server_made_job_id_to_the_worker(monkeypatch):
    seen = {}

    class App:
        class state:  # noqa: N801
            pass

    def worker(app, ws, job_id, document_id, title, data, kind, content_hash_hex, **kw):
        seen.update(job_id=job_id, kw=kw)

    monkeypatch.setattr(jobs, "_worker", worker)
    job_id = jobs.run_upload_job(App, workspace_id=WS, document_id=DOC, title="t", data=b"x", kind="txt",
                                 content_hash_hex="h" * 64, charged_day=DAY)
    import threading
    for thread in threading.enumerate():
        if thread.name == f"upload-{job_id}":
            thread.join(5)

    assert re.fullmatch(r"[0-9a-f]{16}", job_id) and seen == {"job_id": job_id, "kw": {"charged_day": DAY}}


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


# ---------------------------------------------------------------- 1. the cap


def test_the_default_cap_is_10000_and_the_page_limit_stays_30():
    s = Settings(_env_file=None)
    assert (s.upload_max_tokens, s.upload_max_pages) == (10000, 30)


def test_the_workspace_limits_report_the_setting(monkeypatch):
    from semigraph.serve import workspace_routes as wr

    assert wr._limits(Settings(_env_file=None))["max_tokens_per_version"] == 10000
    assert wr._limits(Settings(_env_file=None, upload_max_tokens=7777))["max_tokens_per_version"] == 7777


def test_a_10000_token_document_is_accepted(world):
    world.document(chunk_texts(25, 400))
    events = world.run(steps(world.clock, 1.0))

    assert last_event(events)["state"] == "ready"


def test_a_10001_token_document_is_refused_before_any_chunking_or_embedding(world, caplog):
    texts = chunk_texts(25, 400)
    texts[-1] += " extra"                                                   # 10,001 words
    world.document(texts)

    def never(*a, **kw):
        raise AssertionError("the document must be refused before chunking")

    world.monkeypatch.setattr(jobs, "_build_units_and_chunks", never)
    embedder = steps(world.clock, 1.0)
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(embedder)

    assert last_event(events)["error"]["code"] == "too_many_tokens" and embedder.calls == 0 and stage_lines(caplog) == []


@pytest.mark.parametrize("cap,ceiling,boots", [
    (10_000, 1_200, True),       # a cap at the threshold is not checked by this rule, whatever the ceiling
    (10_000, 1_805, True),
    (10_001, 1_805, False),      # one past the threshold: floor(1,805 x 5.54) = 9,999 does not cover it
    (11_634, 2_100, True),       # 2,100 s x 5.54 = 11,634 exactly
    (11_635, 2_100, False)])
def test_the_validator_checks_only_a_cap_above_the_threshold_against_the_ceiling_at_the_worst_rate(cap, ceiling, boots):
    """Requirement 1 as the spec words it (owner decision 2026-11-07): refuse a cap ABOVE 10,000 unless the ceiling covers it at 5.54
    tokens/s; a cap of 10,000 or less is not checked by this rule."""
    if boots:
        s = live(upload_max_tokens=cap, upload_embed_timeout_s=ceiling)
        assert (s.upload_max_tokens, s.upload_embed_timeout_s) == (cap, ceiling)
        return
    with pytest.raises(ValidationError) as refused:
        live(upload_max_tokens=cap, upload_embed_timeout_s=ceiling)
    assert named(refused) == [EMBED_TIMEOUT]


def test_the_threshold_is_the_settings_default_cap():
    assert config.PRODUCTION_UPLOAD_CAP_UNCHECKED_TOKENS == 10_000
    assert Settings.model_fields["upload_max_tokens"].default == config.PRODUCTION_UPLOAD_CAP_UNCHECKED_TOKENS


def test_the_live_defaults_pass_the_rule_and_it_uses_one_named_constant():
    s = live()
    assert (s.upload_max_tokens, s.upload_embed_timeout_s) == (10000, 2100)
    assert str(config.PRODUCTION_WORST_EMBED_TOKENS_PER_S) == "277/50"       # 5.54 as an exact fraction
    assert 2100 * config.PRODUCTION_WORST_EMBED_TOKENS_PER_S >= 10000


def test_the_cap_refusal_names_the_ceiling_first_and_prints_no_value():
    with pytest.raises(ValidationError) as refused:
        live(upload_max_tokens=11_635)
    text = str(refused.value)
    message = refused.value.errors()[0]["msg"].removeprefix("Value error, ")
    assert message.startswith(EMBED_TIMEOUT + " ") and "input_value" not in text
    assert [v for v in ("11635", "11,635", "11634", "11,634", "2100", "2,100") if v in text] == []


def test_outside_production_any_cap_boots():
    assert Settings(_env_file=None, environment="development", upload_max_tokens=10**7).upload_max_tokens == 10**7


# ---------------------------------------------------------------- 4. the stage line


CPU_START = "usage_usec 1000\nuser_usec 600\nsystem_usec 400\nnr_periods 50\nnr_throttled 3\nthrottled_usec 777\n"
CPU_END = "usage_usec 2500\nuser_usec 1600\nsystem_usec 900\nnr_periods 90\nnr_throttled 11\nthrottled_usec 4242\n"

LINE = re.compile(
    r"^upload stages ws_hash=[0-9a-f]{12} job_id=[0-9a-z]+(?: parse_s=\d+\.\d+)?(?: chunk_s=\d+\.\d+)?"
    r"(?: embed_s=\d+\.\d+)?(?: compare_s=\d+\.\d+)?(?: index_s=\d+\.\d+)?(?: chunks=\d+)?(?: fresh=\d+)?"
    r"(?: done=\d+)?(?: tokens=\d+)?(?: projected_s=\d+\.\d)?(?: aborted=too_slow)?"
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

    [line] = stage_lines(caplog)
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

    [line] = stage_lines(caplog)
    assert last_event(events)["state"] == "ready" and LINE.match(line) and "cpu_" not in line


def test_a_field_the_file_lacks_or_garbles_is_left_out_never_guessed(world, caplog, monkeypatch, tmp_path):
    path = tmp_path / "cpu.stat"
    path.write_text("usage_usec 1000\nnr_throttled lots\nuser_usec 5\n")        # no throttled_usec, a non-number
    monkeypatch.setattr(jobs, "CGROUP_CPU_STAT_PATH", str(path))
    world.document(chunk_texts(4, 5))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(steps(world.clock, 1.0))

    [line] = stage_lines(caplog)
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

    assert last_event(events)["state"] == "ready" and len(stage_lines(caplog)) == 1 and world.slots.released == 1


def test_s3_the_cgroup_read_takes_no_input_and_opens_only_the_fixed_path(world, monkeypatch, tmp_path):
    assert not inspect.signature(jobs._read_cpu_stat).parameters
    monkeypatch.undo()                                                       # the constant itself, not the test double
    assert jobs.CGROUP_CPU_STAT_PATH == "/sys/fs/cgroup/cpu.stat"


def test_s3_a_job_opens_no_file_but_the_one_constant(world, monkeypatch, tmp_path):
    import semigraph.retrieval.ids  # noqa: F401 - what a job imports lazily, loaded BEFORE open() is patched
    import semigraph.retrieval.workspace  # noqa: F401
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


def test_the_stage_line_projection_is_the_smaller_of_the_two(world, caplog):
    """9 one-token chunks then five of 400 at 10 s a call plus 0.1 s a token, ceiling 400 s: the last projection (after chunk 13,
    290.9 s) is 363.2 s token-weighted (2,009 of 1,609 tokens) and 313.3 s chunk-weighted (14 of 13 chunks): the line shows 313.3."""
    world.document([f"t{i}" for i in range(9)] + chunk_texts(5, 400))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        events = world.run(fixed_cost(world.clock, 10.0, 0.1), ceiling=400)

    fields = fields_of(stage_lines(caplog)[0])
    assert last_event(events)["state"] == "ready" and "aborted" not in fields and fields["projected_s"] == "313.3"


def test_the_abort_is_written_on_the_line_with_its_last_projection(world, caplog, refunds):
    world.document(chunk_texts(10, 400))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    [line] = stage_lines(caplog)
    fields = fields_of(line)
    assert LINE.match(line) and fields["aborted"] == "too_slow" and fields["done"] == "3"
    assert fields["projected_s"] == "40.0"


def test_a_job_that_was_not_aborted_has_no_aborted_field_and_a_short_job_no_projection(world, caplog):
    world.document(chunk_texts(10, 400))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world.run(steps(world.clock, 4), ceiling=40)
    ready_fields = fields_of(stage_lines(caplog)[0])
    assert "aborted" not in ready_fields and ready_fields["projected_s"] == "40.0"

    caplog.clear()
    world2 = World(world.monkeypatch)
    world2.document(chunk_texts(2, 600))
    with caplog.at_level("INFO", logger="semigraph.uploads.jobs"):
        world2.run(steps(world2.clock, 4))
    assert "projected_s" not in fields_of(stage_lines(caplog)[0])


# ---------------------------------------------------------------- 5. the client copy, S1, S4, S5


def test_the_too_slow_text_is_the_specs_and_is_the_only_text_for_the_code():
    assert jobs.JOB_ERROR_MESSAGES["too_slow"] == TOO_SLOW_TEXT


def test_s5_no_event_and_no_persisted_job_carries_a_rate_a_projection_or_a_limit(world, refunds):
    world.document(chunk_texts(10, 400), parse_s=7.0, chunk_s=3.0)
    events = world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)
    dump = json.dumps([*events, *world.persisted()])

    assert last_event(events) == FAILED_TOO_SLOW_EVENT and world.persisted()[-1] == FAILED_TOO_SLOW_EVENT
    for word in ("projected", "rate", "tok", "ceiling", "limit", "39", "40.0", "aborted"):
        assert word not in dump.replace(TOO_SLOW_TEXT, "")
    progress = [e["progress"] for e in events if "progress" in e]
    assert progress and all(set(p) == {"done", "total", "eta_s"} for p in progress)


def test_s4_the_abort_frees_the_slot_once_and_leaves_nothing_behind(world, refunds, fresh_drain):
    world.document(chunk_texts(10, 400))
    world.run(steps(world.clock, 4), ceiling=39, charged_day=DAY)

    assert world.repo.put_version_calls == [] and world.repo.workspace_tokens == 0 and world.repo.embedded == {}
    assert world.slots.released == 1 and world.reg.is_terminal(WS, "job1") is True and fresh_drain.active == 0
    assert world.slots.acquire(blocking=False) is True
    world.slots.release()


def test_s1_one_visitors_abort_changes_nothing_for_the_next_job(world, refunds):
    """The decision reads the job's own clock and tokens: a job that was aborted leaves no state a later job reads."""
    world.document(chunk_texts(10, 400))
    first = world.run(steps(world.clock, 4), ceiling=39, job_id="jobA", charged_day=DAY)
    world2 = World(world.monkeypatch)
    world2.document(chunk_texts(10, 400))
    second = world2.run(steps(world2.clock, 4), ceiling=40, job_id="jobB", charged_day=DAY)

    assert last_event(first)["error"]["code"] == "too_slow" and last_event(second)["state"] == "ready"
    assert refunds == [(DAY, "jobA")]


def test_s2_the_refund_arguments_are_the_servers_day_and_job_id_only(world, refunds):
    """The only inputs of the refund are the charged day (the route's own clock) and the job id (``secrets.token_hex``)."""
    world.document(chunk_texts(10, 400), )
    world.run(steps(world.clock, 4), ceiling=39, job_id="0123456789abcdef", charged_day=DAY, title="DROP ALL ' OR 1=1")

    assert refunds == [(DAY, "0123456789abcdef")]


def test_embed_chunks_records_the_decision_on_a_too_slow_exit(world):
    texts = chunk_texts(10, 400)
    hashes = [content_hash(t) for t in texts]
    chunks = [Chunk(seq=i, char_start=0, char_end=1, tokens=400) for i in range(10)]
    stages: dict = {}

    outcome = jobs._embed_chunks(object(), WS, DOC, texts, hashes, chunks, steps(world.clock, 4), JobSettings(39),
                                 lambda *a, **kw: None, clock=world.clock, stages=stages)

    assert outcome == "too_slow"
    assert stages == {"fresh": 10, "tokens": 4000, "done": 3, "embed_s": 12.0, "projected_s": 40.0, "aborted": "too_slow"}


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
