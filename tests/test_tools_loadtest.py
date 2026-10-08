"""tools/loadtest: the S2 load generator's pure modules (M5a I5; docs/v2/M5_PLAN.md section 6, council 5 verdict).

Sections, in order: the traffic model and pacing, the SSE parser, the salt (regex proofs against the server's own compiled
patterns), the question pool, the offline salt checks, the request recorder and ask client (against a local fake server), the
CPU sampler, and the packaging (the generator image's files and its import hygiene). Nothing here imports ``locust``:
importing it monkey-patches gevent into the whole pytest process (the opt-in smoke test runs it in a subprocess).
"""

import ast
import hashlib
import importlib
import json
import random
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.loadtest import model  # noqa: E402

LOADTEST_DIR = ROOT / "tools" / "loadtest"
DATA_DIR = ROOT / "tests" / "data"


# =====================================================================================================================
# the traffic model and pacing
# =====================================================================================================================

def test_derived_rates_match_the_pre_registered_figures():
    rates = model.derived_rates(1000)
    assert rates["iterations_per_s"] == pytest.approx(4.762, abs=0.001)
    assert rates["live_per_s"] == pytest.approx(2.619, abs=0.001)         # "~2.6 live asks/s"
    assert rates["cached_per_s"] == pytest.approx(2.143, abs=0.001)       # "~2.1 cached/s"
    assert model.void_floor_rate() == pytest.approx(2.47)


def test_ask_mix_is_the_plan_s_45_40_10_5():
    assert dict(model.ASK_MIX) == {"cached": 0.45, "live_pool": 0.40, "live_unique": 0.10, "live_agent": 0.05}
    assert sum(share for _, share in model.ASK_MIX) == pytest.approx(1.0)
    assert model.live_share() == pytest.approx(0.55)
    assert (model.P_EVIDENCE, model.P_DOSSIER_OR_CHANGES, model.UPLOAD_VUS, model.UPLOAD_PERIOD_S) == (0.20, 0.05, 5, 600.0)


def test_phase_schedule_is_ramp_steady_spike_soak_fault_103_minutes():
    schedule = model.phase_schedule()
    assert [name for name, _, _ in schedule] == ["ramp", "steady", "spike", "soak", "fault"]
    assert [(t1 - t0) / 60 for _, t0, t1 in schedule] == [10, 20, 3, 60, 10]
    assert model.run_length_s() == 103 * 60
    assert model.phase_at(0) == "ramp" and model.phase_at(599.9) == "ramp" and model.phase_at(600) == "steady"
    assert model.phase_at(1800) == "spike" and model.phase_at(1980) == "soak" and model.phase_at(5580) == "fault"
    assert model.phase_at(model.run_length_s()) is None


def test_target_users_ramp_steady_spike_and_end():
    assert model.target_users(0)[0] == 1
    assert model.target_users(300)[0] == 500
    assert model.target_users(599)[0] == 998
    assert model.target_users(600) == (1000, 50.0)
    assert model.target_users(1850) == (1500, 50.0)                       # the spike adds 500 VUs
    assert model.target_users(2000) == (1000, 50.0)
    assert model.target_users(model.run_length_s() + 1) is None
    assert model.target_users(100, vus=200)[0] == 33


def test_expected_paid_asks_and_per_vu_cap_arithmetic():
    assert model.expected_paid_asks() == pytest.approx(15_600, rel=0.02)


def test_wait_after_never_negative_and_completes_the_cycle():
    assert model.wait_after(15.0, 200.0) == 185.0
    assert model.wait_after(250.0, 200.0) == 0.0


def _simulate_one_vu(rng, *, cycles, elapsed_s, paced):
    """Start times of a VU's iterations; ``paced`` = the cycle is start to start, else Locust's ``between`` (think time
    AFTER the iteration)."""
    t, starts = 0.0, []
    for _ in range(cycles):
        starts.append(t)
        cycle = model.draw_cycle(rng)
        t += model.wait_after(elapsed_s, cycle) + elapsed_s if paced else elapsed_s + cycle
    return starts


def test_pacing_holds_the_model_s_rate_where_locust_between_would_miss_the_void_floor():
    # A 25 s iteration (a live stream plus the reads). START-to-start pacing keeps 210 s; "between(120, 300)" makes it 235 s.
    rng = random.Random(7)
    paced = _simulate_one_vu(rng, cycles=4000, elapsed_s=25.0, paced=True)
    naive = _simulate_one_vu(random.Random(7), cycles=4000, elapsed_s=25.0, paced=False)
    paced_cycle = (paced[-1] - paced[0]) / (len(paced) - 1)
    naive_cycle = (naive[-1] - naive[0]) / (len(naive) - 1)
    assert paced_cycle == pytest.approx(210.0, abs=2.0)
    assert naive_cycle == pytest.approx(235.0, abs=2.0)
    live_rate = lambda cycle: 1000 / cycle * model.live_share()               # noqa: E731
    assert live_rate(paced_cycle) >= model.void_floor_rate()                     # ~2.62 against the 2.47 floor
    assert live_rate(naive_cycle) < live_rate(paced_cycle) * 0.92                # the trap: ~2.34, a VOID by construction


def test_first_delay_is_the_residual_life_of_the_cycle_process():
    rng = random.Random(11)
    delays = [model.first_delay(rng) for _ in range(40_000)]
    assert 0.0 <= min(delays) and max(delays) <= 300.0
    assert statistics.fmean(delays) == pytest.approx((2700 + 210**2) / (2 * 210), abs=1.2)       # 111.4 s, not 105 or 210


def test_first_delay_makes_the_arrival_rate_stationary_from_the_first_minute():
    rng = random.Random(3)
    starts = []
    for _ in range(1000):
        t = model.first_delay(rng)
        while t < 900.0:
            starts.append(t)
            t += model.draw_cycle(rng)
    expected = 1000 / 210 * 300                                                  # asks in a 300 s bucket
    for lo in (0, 300, 600):
        in_bucket = sum(1 for t in starts if lo <= t < lo + 300)
        assert in_bucket == pytest.approx(expected, rel=0.06), lo


def test_choose_ask_class_follows_the_shares():
    rng = random.Random(5)
    counts = {}
    for _ in range(100_000):
        name = model.choose_ask_class(rng)
        counts[name] = counts.get(name, 0) + 1
    for name, share in model.ASK_MIX:
        assert counts[name] / 100_000 == pytest.approx(share, abs=0.01)


def test_vu_ip_is_unique_per_worker_and_vu_and_never_shared_with_uploads():
    seen = set()
    for worker in range(10):
        for seq in list(range(1500)) + [59_999]:
            seen.add(model.vu_ip("run-a", worker, seq))
    assert len(seen) == 10 * 1501
    uploads = {model.vu_ip("run-a", w, s, upload=True) for w in range(10) for s in range(5)}
    assert len(uploads) == 50 and not uploads & seen
    assert all(ip.startswith("10.") for ip in seen | uploads)
    # a different run id (a different hash nibble) moves every address, so same-day runs do not share per-address windows
    other_id = next(f"run-{i}" for i in range(100) if model._run_nibble(f"run-{i}") != model._run_nibble("run-a"))
    assert not {model.vu_ip(other_id, 0, s) for s in range(100)} & {model.vu_ip("run-a", 0, s) for s in range(100)}


@pytest.mark.parametrize("worker,seq", [(10, 0), (-1, 0), (0, 60_000), (0, -1)])
def test_vu_ip_refuses_out_of_range_inputs(worker, seq):
    with pytest.raises(ValueError):
        model.vu_ip("run-a", worker, seq)


@pytest.mark.parametrize("host", [
    "http://localhost:8080", "127.0.0.1:8080", "http://[::1]:8080", "https://semigraph-stg.fly.dev",
    "semigraph-stg.fly.dev", "http://sin.semigraph-stg.internal:8080", "semigraph-stg.internal"])
def test_check_target_host_accepts_loopback_and_staging(host):
    assert model.check_target_host(host)


@pytest.mark.parametrize("host", [
    "https://semigraph.fly.dev", "https://semigraph.fly.dev/", "semigraph.internal", "http://sin.semigraph.internal:8080",
    "semigraph-neo4j.internal", "example.com", "semigraph.fly.dev.evil.com", "stg.example.com", "semigraph-staging.fly.dev",
    "10.0.0.5", "", None, "   "])
def test_check_target_host_refuses_the_live_app_and_everything_else(host):
    with pytest.raises(ValueError):
        model.check_target_host(host)


def test_limit_warnings_flag_settings_that_would_turn_a_correct_429_into_an_error():
    production_like = {"limits": {"max_queries_per_day": 150, "max_spend_usd_per_day": 10.0, "per_ip_per_day": 20,
                                  "per_ip": "3 per 10 min"}, "agent_enabled": False, "uploads_enabled": False, "paused": True}
    text = " | ".join(model.limit_warnings(production_like))
    # production's own "5 per 10 min" is enough for cycles of at least 120 s (5 asks need 600 s): no warning for it
    assert not any("per_ip window" in w for w in model.limit_warnings({"limits": {"per_ip": "5 per 10 min"}}))
    for needle in ("max_queries_per_day=150", "max_spend_usd_per_day", "per_ip_per_day=20", "per_ip window",
                   "agent_enabled=false", "uploads_enabled=false", "kill switch"):
        assert needle in text, needle
    relaxed = {"limits": {"max_queries_per_day": 0, "max_spend_usd_per_day": 0, "per_ip_per_day": 0,
                          "per_ip": "20 per 10 min"}, "agent_enabled": True, "uploads_enabled": True, "paused": False}
    assert model.limit_warnings(relaxed) == []


# --- END OF SECTION: model ---


# =====================================================================================================================
# the SSE parser: fed what the server really writes
# =====================================================================================================================

from tools.loadtest import sse  # noqa: E402


def _recorded_streams() -> dict[str, list[dict]]:
    """name -> the event dicts of every recorded scenario in tests/data (pre-M4 SEC answers, pre-M5 agent and workspace)."""
    streams = {}
    for name, scenario in json.loads((DATA_DIR / "answer_events_pre_m4.json").read_text(encoding="utf-8")).items():
        streams[f"answer/{name}"] = scenario["events"]
    for file in ("agent_events_pre_m5", "workspace_events_pre_m5"):
        for scenario in json.loads((DATA_DIR / f"{file}.json").read_text(encoding="utf-8"))["scenarios"]:
            streams[f"{file}/{scenario['name']}"] = scenario["events"]
    return streams


def _wire(events: list[dict], *, sep: str = "\n") -> bytes:
    """The bytes the site writes for these events: the server's own encoder (``stream_runtime.sse_event``) for ``\\n``, and
    sse-starlette's default ``\\r\\n`` for the other separator."""
    from sse_starlette import ServerSentEvent
    from semigraph.serve.stream_runtime import sse_event

    if sep == "\n":
        return b"".join(sse_event(e).encode() for e in events)
    return b"".join(ServerSentEvent(data=json.dumps(e, default=str), event=e["event"], sep=sep).encode() for e in events)


def _parse_all(wire: bytes, chunks: list[int] | None = None) -> list:
    parser, items, at = sse.SseParser(), [], 0
    for size in chunks or [len(wire) or 1]:
        items += parser.feed(wire[at:at + size])
        at += size
    if at < len(wire):
        items += parser.feed(wire[at:])
    items += parser.finish()
    assert not parser.pending
    return items


def _ask_events(items: list) -> list[sse.AskEvent]:
    return [sse.parse_ask_event(m) for m in items if isinstance(m, sse.SseMessage)]


def test_every_recorded_stream_round_trips_through_the_servers_own_encoder():
    streams = _recorded_streams()
    assert len(streams) >= 17
    names = set()
    for label, events in streams.items():
        parsed = _ask_events(_parse_all(_wire(events)))
        assert [e.name for e in parsed] == [e["event"] for e in events], label
        assert [e.payload for e in parsed] == [json.loads(json.dumps(e, default=str)) for e in events], label
        names |= {e.name for e in parsed}
    assert names == set(sse.ASK_EVENTS)                       # the fixtures exercise the whole grammar


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\r"])
def test_every_line_ending_and_every_chunking_gives_the_same_events(sep):
    events = [e for stream in _recorded_streams().values() for e in stream][:40]
    wire = _wire(events, sep=sep)
    expected = [e["event"] for e in events]
    rng = random.Random(1)
    splits = [[1] * len(wire), [rng.randint(1, 9) for _ in range(len(wire))], [len(wire)]]
    for chunks in splits:
        assert [e.name for e in _ask_events(_parse_all(wire, chunks))] == expected


def test_a_cr_lf_split_across_two_chunks_is_one_line_end():
    parser = sse.SseParser()
    assert parser.feed(b'event: delta\r\ndata: {"event": "delta", "text": "a"}\r') == []
    items = parser.feed(b"\n\r\n")
    assert [sse.parse_ask_event(m).payload["text"] for m in items] == ["a"]


def test_pings_are_comments_in_arrival_order_and_never_events():
    from datetime import datetime, timezone
    from sse_starlette import ServerSentEvent

    ping = ServerSentEvent(comment=f"ping - {datetime.now(timezone.utc)}", sep="\n").encode()   # sse-starlette's default ping
    events = [{"event": "retrieval", "counts": {"chunks": 1}}, {"event": "delta", "text": "x"}]
    wire = _wire(events[:1]) + ping + ping + _wire(events[1:]) + ping
    items = _parse_all(wire)
    assert [type(i).__name__ for i in items] == ["SseMessage", "Ping", "Ping", "SseMessage", "Ping"]
    assert items[1].comment.startswith("ping - ")
    assert [e.name for e in _ask_events(items)] == ["retrieval", "delta"]


def test_the_cached_done_is_a_single_event_with_the_narrow_shape():
    cached = {"event": "done", "cached": True, "question": "q", "strategy": "hybrid", "answer": "A [x].", "citations": ["x"],
              "hallucinated": [], "source": "benchmark", "created_at": "2026-10-01T00:00:00+00:00"}
    (event,) = _ask_events(_parse_all(_wire([cached])))
    assert event.name == "done" and event.cached and event.terminal
    live = next(e for e in _recorded_streams()["answer/draft_clean"] if e["event"] == "done")
    assert not sse.parse_ask_event(sse.SseMessage("done", json.dumps(live))).cached


def test_terminal_events_are_done_and_error_only():
    for label, events in _recorded_streams().items():
        parsed = _ask_events(_parse_all(_wire(events)))
        terminals = [e.name for e in parsed if e.terminal]
        assert terminals in ([], ["done"], ["error"]), label


def test_multiline_data_bom_comments_and_unknown_fields_follow_the_spec():
    wire = b"\xef\xbb\xbf: hello\nid: 7\nretry: 3000\nx-other: 1\nevent: job\ndata: {\"state\":\ndata: \"ready\"}\n\n\n\n"
    items = _parse_all(wire)
    messages = [i for i in items if isinstance(i, sse.SseMessage)]
    assert len(messages) == 1 and messages[0].id == "7" and messages[0].retry == 3000
    assert sse.parse_job_event(messages[0]).state == "ready"


def test_a_multibyte_character_split_across_chunks_survives():
    wire = 'event: delta\ndata: {"event": "delta", "text": "Nvidia’s"}\n\n'.encode()
    cut = wire.index("’".encode()) + 1
    parser = sse.SseParser()
    items = parser.feed(wire[:cut]) + parser.feed(wire[cut:]) + parser.finish()
    assert sse.parse_ask_event(items[0]).payload["text"] == "Nvidia’s"


def test_a_truncated_stream_leaves_the_event_pending_and_undispatched():
    parser = sse.SseParser()
    assert parser.feed(b'event: delta\ndata: {"event": "delta", "te') == []
    assert parser.pending and parser.finish() == []
    assert parser.pending


@pytest.mark.parametrize("wire,why", [
    (b"event: delta\n\n", "an event block without data"),
    (b"event: delta\ndata: \xff\xfe\n\n", "not valid UTF-8"),
])
def test_wire_level_violations_raise(wire, why):
    parser = sse.SseParser()
    with pytest.raises(sse.SseProtocolError, match=why):
        parser.feed(wire)
        parser.finish()


@pytest.mark.parametrize("name,data,why", [
    ("progress", '{"event": "progress"}', "unknown ask event name"),
    (None, '{"event": "delta", "text": "x"}', "unknown ask event name"),
    ("delta", "not json", "not JSON"),
    ("delta", '["x"]', "not a JSON object"),
    ("delta", '{"event": "done", "text": "x"}', "payload names 'done'"),
    ("delta", '{"event": "delta"}', "field 'text'"),
    ("done", '{"event": "done"}', "field 'answer'"),
    ("error", '{"event": "error"}', "field 'detail'"),
    ("retrieval", '{"event": "retrieval", "counts": []}', "field 'counts'"),
    ("step", '{"event": "step", "n": true, "tool": "t", "ok": true}', "field 'n'"),
    ("escalated", '{"event": "escalated", "from": "a", "to": "b"}', "field 'reasons'"),
])
def test_grammar_violations_raise(name, data, why):
    with pytest.raises(sse.SseProtocolError, match=why):
        sse.parse_ask_event(sse.SseMessage(name, data))


def test_the_job_stream_grammar_reads_the_servers_own_job_events():
    from semigraph.serve.workspace_routes import _job_sse

    jobs = [{"state": "queued", "document_id": "d1"}, {"state": "parsing"}, {"state": "embedding", "chunks": 3},
            {"state": "ready", "version": 1, "chunks": 3}]
    wire = b"".join(_job_sse(j).encode() for j in jobs)
    events = [sse.parse_job_event(m) for m in _parse_all(wire) if isinstance(m, sse.SseMessage)]
    assert [e.state for e in events] == ["queued", "parsing", "embedding", "ready"]
    assert [e.terminal for e in events] == [False, False, False, True]
    assert sse.parse_job_event(sse.SseMessage("job", '{"state": "failed", "error": "x"}')).terminal
    for bad in (sse.SseMessage("delta", '{"state": "ready"}'), sse.SseMessage("job", '{"nope": 1}')):
        with pytest.raises(sse.SseProtocolError):
            sse.parse_job_event(bad)


# --- END OF SECTION: sse ---


# =====================================================================================================================
# the salt: proofs against the server's own compiled year detectors
# =====================================================================================================================

import re  # noqa: E402
import threading  # noqa: E402

from semigraph.retrieval import retriever, router  # noqa: E402
from tools.loadtest import salt  # noqa: E402

PERIOD_RICH_QUESTIONS = (
    "What was Nvidia's total revenue for the fiscal year ended January 28, 2024?",
    "Between its FY2024 and FY2025 annual reports, did AMD or Micron remove any whole risk factor?",
    "How did Intel's capital expenditures change from fiscal 2020 to fiscal 2023?",
    "Compare TSMC's 2023 and 2025 risk factors.",
    "Which risks did Qualcomm add since 2022, and what did it say in 2019-2021?",
    "What does Broadcom say about customer concentration in its latest 10-K?",
    "Nvidia revenue 2024",
)
EDGE_COUNTERS = (0, 1, 9, 99, 999, 1000, 1899, 1900, 1901, 1999, 2000, 2001, 2019, 2024, 2025, 2099, 2100, 99_999, 100_000,
                 999_999)


def test_the_salt_format_is_council_5s():
    assert salt.apply_salt("Who makes Nvidia's GPUs?", 3, 42) == "Who makes Nvidia's GPUs? (ref 3000042)"
    assert salt.salt_suffix(0, 0) == " (ref 0000000)" and len(salt.salt_suffix(9, 999_999)) == salt.SALT_SUFFIX_LEN == 14
    assert salt.MAX_BASE_CHARS == 486 and salt.MAX_QUESTION_CHARS == 500


@pytest.mark.parametrize("worker,n", [(10, 0), (-1, 0), (True, 0), (1.0, 0), (0, -1), (0, 1.5), (0, "1")])
def test_salt_refuses_a_malformed_worker_or_counter(worker, n):
    with pytest.raises(ValueError):
        salt.salt_token(worker, n)


def test_the_counter_never_widens_past_six_digits():
    with pytest.raises(salt.SaltExhausted):
        salt.salt_token(0, 1_000_000)
    salter = salt.Salter(4, start=999_998)
    assert [salter.next("q").n for _ in range(2)] == [999_998, 999_999]
    with pytest.raises(salt.SaltExhausted):
        salter.next("q")


def test_split_salt_round_trips_and_ignores_unsalted_text():
    for worker, n in ((0, 0), (7, 123_456), (9, 999_999)):
        assert salt.split_salt(salt.apply_salt("Why? (see 2024)", worker, n)) == ("Why? (see 2024)", worker, n)
    assert salt.split_salt("What is revenue? (ref 12)") is None
    assert salt.split_salt("(ref 0000001) in the middle of a question?") is None


def test_the_salter_is_deterministic_distinct_and_resumable():
    first = [salt.Salter(2).next("q").text for _ in range(1)]
    a, b = salt.Salter(2), salt.Salter(2)
    assert [a.next("What is X?").text for _ in range(5)] == [b.next("What is X?").text for _ in range(5)]
    assert first[0] == "q (ref 2000000)"
    resumed = salt.Salter(2, start=100_000)
    assert resumed.next("q").token == "2100000" and resumed.issued == 1 and resumed.remaining == 1_000_000 - 100_001


def test_the_salter_is_thread_safe():
    salter, out = salt.Salter(1), []
    def work():
        out.extend(salter.next("q").n for _ in range(500))
    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == list(range(4000))


def test_the_normalization_is_the_servers_cache_key_normalization():
    from semigraph.serve import store

    for question in ("  What  IS the revenue?! ", "Revenue.", "Q (ref 1000001)", "Already normalized"):
        for strategy in ("hybrid", "agent"):
            local = hashlib.sha256(f"T|{strategy}|{salt.normalize_question(question)}".encode()).hexdigest()[:32]
            assert store.cache_key(question, strategy, template="T") == local
    # the salt survives the normalization (it ends in ")", which rstrip("?.! ") does not touch)
    assert salt.normalize_question("What is revenue? (ref 1000001)").endswith("(ref 1000001)")
    assert salt.normalize_question("What is revenue? (ref 1000001)") != salt.normalize_question("What is revenue? (ref 1000002)")


def test_the_suffix_alone_names_no_period_no_company_and_no_route():
    for worker in range(10):
        for n in EDGE_COUNTERS:
            suffix = salt.salt_suffix(worker, n)
            assert retriever.mentioned_periods(suffix) == {"years": [], "dates": []}, suffix
            assert retriever._YEAR.search(suffix) is None and retriever._YEAR_RANGE.search(suffix) is None
            assert retriever._FY_SHORT.search(suffix) is None
            assert not retriever.detect_anchors(suffix)
            assert re.search(rf"\b{router._YEAR}\b", suffix) is None and router._TIME_ANCHOR.search(suffix) is None
            assert router.needs_strong_model(suffix) is False


def test_the_token_is_a_single_seven_digit_run():
    for worker in range(10):
        for n in (0, 1900, 2024, 999_999):
            assert re.fullmatch(r"\d{7}", salt.salt_token(worker, n))


def test_the_year_regions_are_swept_for_every_worker_digit_against_both_detectors():
    """Every worker digit 0-9 x every counter 0..2100 x period-rich questions: periods, routing and anchors do not move."""
    for question in PERIOD_RICH_QUESTIONS:
        periods = retriever.mentioned_periods(question)
        strong = router.needs_strong_model(question)
        anchors = retriever.detect_anchors(question)
        for worker in range(10):
            for n in range(2101):
                salted = salt.apply_salt(question, worker, n)
                assert retriever.mentioned_periods(salted) == periods, salted
                assert router.needs_strong_model(salted) is strong, salted
                if n in EDGE_COUNTERS:                                         # the alias regexes cost ~100x a year check
                    assert retriever.detect_anchors(salted) == anchors, salted
            for n in (99_999, 999_999):
                salted = salt.apply_salt(question, worker, n)
                assert retriever.mentioned_periods(salted) == periods and router.needs_strong_model(salted) is strong


def test_the_regex_proof_has_teeth_an_unpadded_counter_would_trip_both_detectors():
    assert retriever.mentioned_periods("What was revenue? (ref 2024)")["years"] == [2024]       # the trap council 5 named
    assert retriever.mentioned_periods("What was revenue? (ref 1 2025)")["years"] == [2025]
    base = "Summarize Nvidia's risk factors, compared with"
    assert router.needs_strong_model(base) is False
    assert router.needs_strong_model(f"{base} (ref 1999)") is True          # an unpadded counter re-routes the question
    assert router.needs_strong_model(f"{base} (ref {salt.salt_token(2, 1999)})") is False   # the padded token does not


def test_a_restarted_worker_resumes_after_the_highest_counter_it_already_sent(tmp_path):
    def line(run, worker, token):
        return json.dumps({"kind": "ask", "run_id": run, "worker": worker, "salt": token})

    first = tmp_path / "events.w3.p1.jsonl"
    first.write_text("\n".join([line("r1", 3, "3000041"), line("r1", 3, "3000042"), line("r1", 4, "4999999"),
                                line("other", 3, "3777777"), line("r1", 3, None), "not json at all"]) + "\n", encoding="utf-8")
    second = tmp_path / "events.w3.p2.jsonl"
    second.write_text(line("r1", 3, "3000100") + "\n" + '{"kind": "ask", "salt": "30', encoding="utf-8")        # a torn last line
    assert salt.resume_start([first, second], 3, "r1") == 101
    assert salt.resume_start([first], 3, "r1") == 43 and salt.resume_start([first], 3, "r1", default=500) == 500
    assert salt.resume_start([tmp_path / "missing.jsonl"], 3, "r1", default=7) == 7 and salt.resume_start([], 3, "r1") == 0


# --- END OF SECTION: salt ---


# =====================================================================================================================
# the 300-question pool
# =====================================================================================================================

from tools.loadtest import pool as poolmod  # noqa: E402


@pytest.fixture(scope="module")
def committed_pool():
    return poolmod.load_pool()


def test_the_committed_pool_is_exactly_300_distinct_saltable_questions(committed_pool):
    assert len(committed_pool.live) == 300 == poolmod.POOL_SIZE
    keys = [salt.normalize_question(q.text) for q in committed_pool.live]
    assert len(set(keys)) == 300                                           # de-duplicated by the cache-key normalization
    assert len({q.id for q in committed_pool.live}) == 300
    assert all(poolmod.MIN_QUESTION_CHARS <= len(" ".join(q.text.split())) <= salt.MAX_BASE_CHARS for q in committed_pool.live)
    assert committed_pool.sha256 == poolmod.pool_digest([{"id": q.id, "q": q.text} for q in committed_pool.live])


def test_the_committed_pool_composition_is_recorded_and_adds_up(committed_pool):
    c = committed_pool.composition
    assert c["total"] == 300 == c["real_questions"] + c["templated_questions"]
    assert sum(c["by_source"].values()) == 300 and c["by_source"]["template"] == c["templated_questions"]
    assert sum(c["routed"].values()) == 300 and set(c["routed"]) == {"cheap", "strong"}
    assert c["by_source"]["benchmark"] > 0 and c["by_source"]["agent_benchmark"] > 0
    assert c["dropped_by_dedupe_or_length"]["by_source"].get("examples", 0) > 0          # the examples are a subset of the benchmark
    assert "minority" in c["note"]
    summary = json.loads(poolmod.DEFAULT_COMPOSITION_PATH.read_text(encoding="utf-8"))
    assert summary["sha256"] == committed_pool.sha256 and summary["total"] == 300


def test_the_agent_set_the_examples_the_evidence_ids_and_the_tickers(committed_pool):
    from semigraph.retrieval.ids import classify_id
    from semigraph.universe import FILERS

    live_ids = {q.id for q in committed_pool.live}
    assert committed_pool.agent and {q.id for q in committed_pool.agent} <= live_ids
    assert all(q.source == "agent_benchmark" for q in committed_pool.agent)
    assert len(committed_pool.examples) == 53 and all(e["question"] for e in committed_pool.examples)
    assert committed_pool.evidence_ids and all(classify_id(i) in ("chunk", "xbrl", "fr") for i in committed_pool.evidence_ids)
    assert committed_pool.tickers and set(committed_pool.tickers) <= set(FILERS)
    rng = random.Random(0)
    assert {committed_pool.pick(rng, "live_agent").id for _ in range(200)} <= {q.id for q in committed_pool.agent}
    assert {committed_pool.pick(rng, k).source for k in ("live_pool", "live_unique") for _ in range(300)} >= {"benchmark", "template"}


def test_the_router_share_of_the_pool_is_reported_not_assumed(committed_pool):
    strong = sum(1 for q in committed_pool.live if q.routed == "strong")
    assert strong == committed_pool.composition["routed"]["strong"]
    assert strong == sum(1 for q in committed_pool.live if router.needs_strong_model(q.text))


def test_a_rebuild_from_unchanged_sources_reproduces_the_committed_pool(tmp_path):
    if poolmod.is_stale():
        pytest.skip("the benchmark / examples changed since pool.json was built: run `python -m tools.loadtest.pool build`")
    rebuilt = poolmod.build_pool()
    assert json.loads(json.dumps(rebuilt, ensure_ascii=False)) == json.loads(poolmod.DEFAULT_POOL_PATH.read_text("utf-8"))
    assert poolmod.build_pool() == rebuilt                                # deterministic


def _fake_sources(tmp_path, *, benchmark, agent=(), examples=()):
    files = {"benchmark": tmp_path / "b.json", "agent_benchmark": tmp_path / "a.json", "examples": tmp_path / "e.json"}
    files["benchmark"].write_text(json.dumps([{"id": f"Q{i}", "type": "numeric", "q": q} for i, q in enumerate(benchmark)]), "utf-8")
    files["agent_benchmark"].write_text(json.dumps({"questions": [{"id": f"A{i}", "type": "numeric", "q": q} for i, q in enumerate(agent)]}), "utf-8")
    files["examples"].write_text(json.dumps({"examples": [{"id": f"E{i}", "type": "numeric", "question": q,
                                                           "citations": [f"xbrl:1:revenue:2024-0{i + 1}-01"]} for i, q in enumerate(examples)]}), "utf-8")
    return files


def test_the_builder_deduplicates_by_cache_key_and_drops_what_cannot_carry_the_salt(tmp_path):
    long_question = "What is Nvidia's revenue? " + "x" * 500
    sources = _fake_sources(
        tmp_path,
        benchmark=["What was Nvidia's total revenue for fiscal 2024?", "tiny", long_question],
        agent=["WHAT was   Nvidia's total revenue for fiscal 2024", "Which suppliers does AMD name in its filings?"],
        examples=["What was Nvidia's total revenue for fiscal 2024?!"])
    doc = poolmod.build_pool(sources, size=40)
    texts = [q["q"] for q in doc["live"]]
    assert len(doc["live"]) == 40 == doc["size"] and len({salt.normalize_question(t) for t in texts}) == 40
    assert sum(salt.normalize_question(t) == "what was nvidia's total revenue for fiscal 2024" for t in texts) == 1
    assert long_question not in texts and "tiny" not in texts
    dropped = doc["composition"]["dropped_by_dedupe_or_length"]
    assert dropped["count"] == 4 and dropped["by_source"] == {"benchmark": 2, "agent_benchmark": 1, "examples": 1}
    assert [q["source"] for q in doc["live"] if q["source"] != "template"] == ["benchmark", "agent_benchmark"]
    assert [q["id"] for q in doc["agent"]] == ["A:A1"]


def test_the_builder_fails_rather_than_return_a_short_pool(tmp_path):
    sources = _fake_sources(tmp_path, benchmark=["What was Nvidia's total revenue for fiscal 2024?"])
    with pytest.raises(poolmod.PoolError, match="can only supply"):
        poolmod.build_pool(sources, size=5000)


def test_is_stale_names_the_changed_source(tmp_path):
    sources = _fake_sources(tmp_path, benchmark=["What was Nvidia's total revenue for fiscal 2024?"])
    doc = poolmod.build_pool(sources, size=30)
    path = tmp_path / "pool.json"
    poolmod.write_pool(doc, path, tmp_path / "composition.json")
    assert poolmod.is_stale(path, sources) == []
    sources["benchmark"].write_text("[]", "utf-8")
    assert poolmod.is_stale(path, sources) == ["benchmark"]


@pytest.mark.parametrize("tamper,message", [
    (lambda d: d["live"].pop(), "299 questions"),
    (lambda d: d["live"][1].update(q=d["live"][0]["q"].upper() + "?"), "share a cache key"),
    (lambda d: d["live"][0].update(q="x" * 490), "cannot carry"),
    (lambda d: d["live"][0].update(q="a different question that nobody pinned in the digest?"), "edited by hand"),
    (lambda d: d.update(version=99), "pool version"),
    (lambda d: d.update(agent=[]), "agent set"),
])
def test_load_pool_refuses_a_malformed_file(tmp_path, tamper, message):
    doc = json.loads(poolmod.DEFAULT_POOL_PATH.read_text("utf-8"))
    tamper(doc)
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(doc), "utf-8")
    with pytest.raises(poolmod.PoolError, match=message):
        poolmod.load_pool(path)


# --- END OF SECTION: pool ---


# =====================================================================================================================
# council 5's offline salt checks
# =====================================================================================================================

from tools.loadtest import salt_check  # noqa: E402


@pytest.fixture(scope="module")
def salt_result(committed_pool):
    return salt_check.run_checks(committed_pool)


def test_the_committed_pool_passes_every_offline_salt_check(salt_result):
    assert salt_result["ok"] is True and salt_result["failure_count"] == 0 and salt_result["failures"] == []
    assert [c["name"] for c in salt_result["checks"]] == [
        "validate_question", "periods", "pair_mode", "anchors", "strong", "cache_keys_distinct", "cache_keys_unsalted_overlap"]
    assert all(c["ok"] for c in salt_result["checks"])
    assert [s["token"] for s in salt_result["salts"]] == ["2001999", "1009999", "9999999"]


def test_every_pool_and_agent_question_is_compared_under_three_salts(salt_result, committed_pool):
    questions = len(committed_pool.live) + len(committed_pool.agent)
    assert salt_result["decisions"]["questions"] == questions == 322
    assert salt_result["decisions"]["comparisons"] == questions * 3
    assert salt_result["decisions"]["longest_salted_chars"] <= 500
    assert salt_result["pool_sha256"] == committed_pool.sha256 and salt_result["salt_format"] == salt.SALT_FORMAT


def test_distinct_cache_keys_equal_sends_and_none_is_an_unsalted_key(salt_result):
    keys = salt_result["cache_keys"]
    assert keys["sends"] == 322 * 10 * len(salt_check.SCHEDULE_COUNTERS) == keys["distinct"]
    assert keys["unsalted_keys"] >= 322                       # 300 hybrid + 22 agent (the 53 cached examples are already in the pool)


def test_the_checks_have_teeth_an_unpadded_unworkered_salt_fails_them(committed_pool, monkeypatch):
    monkeypatch.setattr(salt, "SALT_FORMAT", "{q} (ref {n})")
    result = salt_check.run_checks(committed_pool)
    assert result["ok"] is False
    failed = {f["check"] for f in result["failures"]} | {c["name"] for c in result["checks"] if not c["ok"]}
    assert "cache_keys_distinct" in failed                           # ten workers collapse onto the same counters
    assert "validate_question" in failed                             # and the suffix is no longer the pinned format


def test_a_constant_year_like_salt_trips_the_period_detector(committed_pool, monkeypatch):
    monkeypatch.setattr(salt, "SALT_FORMAT", "{q} (ref 2024)")
    result = salt_check.run_checks(committed_pool)
    assert result["ok"] is False and any(f["check"] == "periods" for f in result["failures"])
    assert result["failure_count"] > len(result["failures"]) == salt_check.MAX_FAILURES_LISTED


def _probe(top8=None, context=1000, fail=False):
    def probe(question):
        if fail:
            raise RuntimeError("neo4j is not running")
        salted = salt.split_salt(question) is not None
        ids = [f"c{i}" for i in range(8)]
        if salted and top8 is not None:
            ids = top8
        return {"top8": ids, "context_chars": context if not salted else int(context * 1.0)}
    return probe


def test_the_local_graph_check_passes_when_retrieval_does_not_move(committed_pool):
    result = salt_check.local_graph_check(committed_pool, _probe(), salt.ADVERSARIAL_SALTS, sample=10)
    assert result["ok"] and result["mean_top8_overlap"] == 1.0 and result["median_context_delta"] == 0.0
    assert result["questions"] == 10 and result["comparisons"] == 30


def test_the_local_graph_check_fails_on_a_low_overlap_or_a_moved_context(committed_pool):
    low = salt_check.local_graph_check(committed_pool, _probe(top8=["c0", "c1", "c2", "c3", "x4", "x5", "x6", "x7"]),
                                       salt.ADVERSARIAL_SALTS, sample=5)
    assert low["mean_top8_overlap"] == 0.5 and low["ok"] is False
    base = _probe(context=1000)

    def moved(question):
        out = base(question)
        return {**out, "context_chars": 1100 if salt.split_salt(question) else 1000}

    delta = salt_check.local_graph_check(committed_pool, moved, salt.ADVERSARIAL_SALTS, sample=5)
    assert delta["median_context_delta"] == pytest.approx(0.10) and delta["ok"] is False


def test_a_requested_local_check_that_cannot_run_is_not_ok_and_an_unrequested_one_is_skipped(committed_pool):
    broken = salt_check.run_checks(committed_pool, local_probe=_probe(fail=True), local_requested=True)
    assert broken["ok"] is False and broken["local_graph"]["ran"] is False and "neo4j is not running" in broken["local_graph"]["error"]
    fine = salt_check.run_checks(committed_pool, local_probe=_probe(), local_requested=True, sample=4)
    assert fine["ok"] is True and fine["local_graph"]["ran"] is True
    skipped = salt_check.run_checks(committed_pool)
    assert skipped["local_graph"] == {"requested": False, "ran": False} and skipped["ok"] is True


def test_the_cli_writes_the_result_file_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(salt_check.LOCAL_GRAPH_ENV, raising=False)
    out = tmp_path / "run" / "salt_check.json"
    assert salt_check.main(["--out", str(out)]) == 0
    written = json.loads(out.read_text("utf-8"))
    assert written["ok"] is True and written["version"] == salt_check.CHECK_VERSION
    assert "OFFLINE SALT CHECKS: PASS" in capsys.readouterr().err


# --- END OF SECTION: salt_check ---


# =====================================================================================================================
# the record writer and the ask / read / upload client, against a local fake of the staging API
# =====================================================================================================================

import time  # noqa: E402

import requests  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from loadtest_fake_server import EVENT_GAP_S, ORIGIN_AUTH, FakeStagingServer  # noqa: E402

from tools.loadtest import client as lclient  # noqa: E402
from tools.loadtest import records  # noqa: E402

LIVE_Q = "Which suppliers does Nvidia depend on? (ref 1000001)"


@pytest.fixture()
def fake():
    with FakeStagingServer() as server:
        yield server


def make_client(server, *, writer=None, read_timeout=5.0, origin_auth=ORIGIN_AUTH, **cfg):
    session = requests.Session()

    def send(method, path, label=None, **kwargs):
        return session.request(method, server.url + path, **kwargs)

    config = lclient.ClientConfig(run_id="run-x", worker=3, vu=7, ip="10.48.0.7", origin_auth=origin_auth,
                                  read_timeout_s=read_timeout, **cfg)
    writer = writer or records.RecordWriter()
    return lclient.LoadClient(send, config, writer, phase=lambda: "steady"), writer


def test_a_live_ask_is_measured_from_the_bytes_ttfe_comes_before_the_first_delta(fake):
    client, writer = make_client(fake)
    record, done = client.ask("live_pool", LIVE_Q, "hybrid", salt="1000001")
    assert record["outcome"] == "done" and record["status"] == 200 and done["answer"].startswith("Nvidia depends")
    assert record["events"] == {"retrieval": 1, "delta": 2, "done": 1} and record["pings"] == 1
    assert record["cached"] is False and record["escalated"] is False and record["answered_by"] == "mock/luna"
    assert record["citations"] == ["x", "y"] and record["salt"] == "1000001" and record["question"] == LIVE_Q
    assert record["ttfe_s"] < record["first_delta_s"] - 0.8 * EVENT_GAP_S          # the retrieval event was parsed when it was sent
    assert record["first_delta_s"] <= record["total_s"] and record["ttfb_s"] is not None and record["bytes"] > 0
    assert {"ts", "run_id", "worker", "vu", "phase", "kind", "ip", "klass", "strategy"} <= set(record)
    assert (record["run_id"], record["worker"], record["vu"], record["phase"], record["ip"]) == ("run-x", 3, 7, "steady", "10.48.0.7")
    assert writer.records == [record]


def test_a_cached_example_is_one_done_event_and_its_ttfe_is_that_event(fake):
    client, _ = make_client(fake)
    record, done = client.ask("cached", fake.examples[0]["question"], "hybrid")
    assert record["outcome"] == "done" and record["cached"] is True and record["salt"] is None
    assert record["events"] == {"done": 1} and record["ttfe_s"] is not None and record["first_delta_s"] is None
    assert done["cached"] is True


@pytest.mark.parametrize("marker,outcome,status", [
    ("[[429]]", "shed_429", 429), ("[[503]]", "shed_503", 503), ("[[500]]", "http_500", 500),
    ("[[drop]]", "dropped", 200), ("[[eof]]", "dropped", 200), ("[[error]]", "error_event", 200),
    ("[[bad]]", "protocol_error", 200), ("[[html]]", "bad_content_type", 200), ("[[escalate]]", "done", 200)])
def test_every_way_an_ask_can_end_is_classified(fake, marker, outcome, status):
    client, _ = make_client(fake)
    record, done = client.ask("live_pool", f"{marker} {LIVE_Q}", "hybrid", salt="1000001")
    assert (record["outcome"], record["status"]) == (outcome, status), record
    if outcome == "error_event":
        assert "failed" in record["detail"] and done is None
    if outcome in ("dropped", "protocol_error", "error_event", "shed_429"):
        assert done is None
    if marker == "[[escalate]]":
        assert record["escalated"] is True and record["events"]["escalated"] == 1
    if outcome in ("shed_429", "shed_503", "http_500"):
        assert record["ttfe_s"] is None and "refused" in record["detail"]


def test_a_stalled_stream_is_a_drop_and_a_silent_server_is_a_timeout(fake):
    client, _ = make_client(fake, read_timeout=0.5)
    dropped, _ = client.ask("live_pool", f"[[stall]] {LIVE_Q}", "hybrid")
    assert dropped["outcome"] == "dropped" and dropped["status"] == 200 and dropped["events"] == {"retrieval": 1}
    timed_out, _ = client.ask("live_pool", f"[[hang]] {LIVE_Q}", "hybrid")
    assert timed_out["outcome"] == "timeout" and timed_out["status"] is None


def test_the_stream_cap_turns_a_slow_ask_into_a_drop(fake):
    client, _ = make_client(fake, stream_cap_s=0.05)
    record, _ = client.ask("live_pool", LIVE_Q, "hybrid")
    assert record["outcome"] == "dropped" and "no terminal event within" in record["detail"]


def test_a_refused_connection_is_an_exception_not_a_crash():
    with FakeStagingServer() as dead:
        url = dead.url
    session = requests.Session()
    client = lclient.LoadClient(lambda m, p, label=None, **kw: session.request(m, url + p, **kw),
                                lclient.ClientConfig("r", 0, 0, "10.0.0.1", connect_timeout_s=1), records.RecordWriter())
    record, done = client.ask("live_pool", LIVE_Q, "hybrid")
    assert record["outcome"] == "exception" and record["status"] is None and done is None
    read, _ = client.read("/api/stats", label="stats", want_json=True)
    assert read["outcome"] == "exception"


def test_every_request_carries_the_origin_secret_the_vu_address_and_the_run_id(fake):
    client, _ = make_client(fake)
    client.read("/", label="shell")
    client.read("/api/stats", label="stats", want_json=True)
    client.ask("cached", fake.examples[0]["question"], "hybrid")
    client.ask("live_pool", LIVE_Q, "hybrid")
    client.upload_cycle(1)
    assert len(fake.requests) == 8                      # 2 reads, 2 asks, and the upload cycle's create / post / watch / delete
    for request in fake.requests:
        headers = request["headers"]
        assert headers["x-origin-auth"] == ORIGIN_AUTH, request["path"]
        assert headers["x-test-client-ip"] == "10.48.0.7" and headers["x-load-run-id"] == "run-x"
    ask = fake.asks[-1]
    assert ask["body"] == {"question": LIVE_Q, "strategy": "hybrid", "turnstile_token": "loadtest-stub"}
    assert ask["headers"]["accept"] == "text/event-stream"


def test_a_wrong_origin_secret_is_http_403_not_a_silent_success(fake):
    client, _ = make_client(fake, origin_auth="wrong")
    record, _ = client.ask("live_pool", LIVE_Q, "hybrid")
    assert (record["outcome"], record["status"]) == ("http_403", 403)
    assert client.read("/api/stats", label="stats")[0]["outcome"] == "http_403"


def test_the_gauge_counts_live_streams_while_they_are_open(fake):
    client, _ = make_client(fake)
    seen = []
    worker = threading.Thread(target=lambda: client.ask("live_pool", f"[[slow]] {LIVE_Q}", "hybrid"))
    worker.start()
    deadline = time.time() + 2
    while time.time() < deadline and not seen:
        if client.gauge.live:
            seen.append(client.gauge.snapshot())
        time.sleep(0.01)
    worker.join()
    assert seen == [(1, 1)] and client.gauge.snapshot() == (0, 0)
    cached_thread = client.ask("cached", fake.examples[0]["question"], "hybrid")
    assert cached_thread[0]["outcome"] == "done" and client.gauge.snapshot() == (0, 0)


def test_reads_record_status_json_and_outcome(fake):
    client, writer = make_client(fake)
    record, data = client.read("/api/examples", label="examples", want_json=True)
    assert record["outcome"] == "ok" and record["status"] == 200 and len(data["examples"]) == 2
    missing, none = client.read("/api/evidence/missing-id", label="evidence", want_json=True)
    assert (missing["outcome"], missing["status"], none) == ("http_404", 404, None)
    assert "no evidence" in missing["detail"]
    shell, body = client.read("/", label="shell")
    assert shell["outcome"] == "ok" and body is None
    assert [r["kind"] for r in writer.records] == ["read", "read", "read"]


def test_the_upload_cycle_creates_uploads_unique_content_watches_to_ready_and_deletes(fake):
    client, writer = make_client(fake)
    first = client.upload_cycle(1)
    second = client.upload_cycle(2)
    assert first["outcome"] == second["outcome"] == "ready" and first["to_ready_s"] > 0
    assert len(fake.uploads) == 2 and fake.uploads[0] != fake.uploads[1]          # an unchanged hash would start no job
    assert b"run=run-x" in fake.uploads[0] and b"seq=1" in fake.uploads[0]
    steps = [(r["label"], r["outcome"]) for r in writer.records if r["kind"] == "upload_step"]
    assert steps == [("upload_create", "ok"), ("upload_post", "ok"), ("upload_delete", "ok")] * 2
    watched = [r for r in fake.requests if "/jobs/" in r["path"]]
    assert all(r["headers"]["x-workspace-token"] == "t" * 43 for r in watched)


def test_an_upload_job_that_fails_or_stalls_is_not_ready(fake):
    fake.job_final = "failed"
    client, _ = make_client(fake)
    failed = client.upload_cycle(1)
    assert failed["outcome"] == "failed" and failed["detail"] == "boom"
    fake.job_final = "stall"
    slow_client, _ = make_client(fake, read_timeout=0.5)
    stalled = slow_client.upload_cycle(2)
    assert stalled["outcome"] == "dropped"
    broken, _ = make_client(fake, origin_auth="wrong")
    assert broken.upload_cycle(3)["outcome"] == "http_403"


def test_listeners_see_every_record_and_the_phase_comes_from_the_phase_function(fake):
    seen = []
    session = requests.Session()
    config = lclient.ClientConfig("r", 1, 2, "10.0.0.2", origin_auth=ORIGIN_AUTH)
    client = lclient.LoadClient(lambda m, p, label=None, **kw: session.request(m, fake.url + p, **kw), config,
                                records.RecordWriter(), phase=lambda: "soak", listeners=(seen.append,))
    client.read("/api/stats", label="stats")
    assert [r["phase"] for r in seen] == ["soak"]


def test_the_record_writer_appends_json_lines_and_survives_a_torn_last_line(tmp_path):
    path = tmp_path / "gen" / "events.0.jsonl"
    writer = records.RecordWriter(path)
    writer.write({"kind": "ask", "n": 1, "text": "café ’"})
    writer.write({"kind": "read", "n": 2})
    writer.close()
    path.write_text(path.read_text("utf-8") + '{"kind": "ask", "n"', "utf-8")        # a process killed mid-write
    assert [r["n"] for r in records.read_records(path)] == [1, 2]
    assert records.read_records(path)[0]["text"] == "café ’"


# --- END OF SECTION: client ---


# =====================================================================================================================
# cpu_watch
# =====================================================================================================================

import os  # noqa: E402

from tools.loadtest import cpu_watch  # noqa: E402

CPU_FIELDS = {"t", "name", "role", "ncores", "cpu_pct_system", "n_procs", "proc_pct_core_max", "proc_pct_core_sum",
              "cpu_pct_procs", "cpu_time_s", "rss_mb", "rss_pct", "mem_total_mb", "cpu_pct_gate"}


def test_the_gate_percentage_counts_a_saturated_single_threaded_process_on_a_bigger_machine():
    assert cpu_watch.gate_pct(50.0, 100.0) == 100.0          # one pinned Locust worker on a 2-core machine
    assert cpu_watch.gate_pct(30.0, 250.0) == 100.0          # a multithreaded process is capped at one core's worth
    assert cpu_watch.gate_pct(80.0, 10.0) == 80.0


def test_cpu_watch_samples_a_busy_process_as_jsonl(tmp_path):
    watch = cpu_watch.CpuWatch("gen-test", "generator", pids=[os.getpid()], interval_s=0.2)
    stop, burn_until = threading.Event(), time.time() + 1.2

    def burn():
        while time.time() < burn_until:
            sum(i * i for i in range(2000))

    burner = threading.Thread(target=burn)
    burner.start()
    out = tmp_path / "cpu" / "gen-test.jsonl"
    lines = watch.run(out, duration_s=0.9, stop=stop)
    burner.join()
    samples = records.read_records(out)
    assert lines == len(samples) >= 3
    assert all(CPU_FIELDS <= set(s) for s in samples)
    assert all(s["name"] == "gen-test" and s["role"] == "generator" and s["n_procs"] == 1 for s in samples)
    times = [s["cpu_time_s"] for s in samples]
    assert times == sorted(times) and times[-1] > times[0]                   # cumulative process CPU-seconds
    assert max(s["proc_pct_core_max"] for s in samples) > 30                  # the burning thread was seen, not the primed 0.0
    assert all(0 <= s["cpu_pct_system"] <= 100 and s["rss_mb"] > 0 and 0 < s["rss_pct"] < 100 for s in samples)
    assert all(s["cpu_pct_gate"] >= s["cpu_pct_system"] for s in samples)


def test_cpu_watch_finds_a_process_by_command_line_text_and_never_writes_the_command_line(tmp_path):
    marker = f"cpuwatch-marker-{os.getpid()}"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", marker])
    try:
        watch = cpu_watch.CpuWatch("api", "server", match=marker, interval_s=0.1)
        watch.prime()
        time.sleep(0.15)
        sample = watch.sample()
        assert sample["n_procs"] == 1 and sample["role"] == "server"
        assert marker not in json.dumps(sample)
        child.terminate()
        child.wait(10)
        watch._refreshed = float("-inf")
        time.sleep(0.15)
        assert watch.sample()["n_procs"] == 0                                # the process went away: no crash, no stale count
    finally:
        child.kill()


def test_cpu_watch_cli_stops_after_the_duration_and_rejects_a_bad_role(tmp_path, capsys):
    out = tmp_path / "w.jsonl"
    assert cpu_watch.main(["--name", "mock", "--role", "mock", "--out", str(out), "--interval", "0.1", "--duration", "0.45"]) == 0
    assert 2 <= len(records.read_records(out)) <= 6 and "samples" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cpu_watch.main(["--name", "x", "--role", "client", "--out", str(out)])
    with pytest.raises(ValueError):
        cpu_watch.CpuWatch("x", "client")


# --- END OF SECTION: cpu_watch ---


# =====================================================================================================================
# the virtual user (the pre-registered iteration) and the phase clock
# =====================================================================================================================

import loadtest_fake_server  # noqa: E402

from tools.loadtest import phases, user  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def quick_fake(monkeypatch):
    monkeypatch.setattr(loadtest_fake_server, "EVENT_GAP_S", 0.0)
    with FakeStagingServer() as server:
        yield server


def make_user(server, *, mix, seed=1, worker=3, clock=None, **params):
    client, writer = make_client(server)
    salter = salt.Salter(worker)
    vu = user.VirtualUser(client, poolmod.load_pool(), salter, random.Random(seed), user.UserParams(mix=mix, **params),
                          clock=clock or time.monotonic)
    return vu, writer, salter


def paths(server):
    return [r["path"].split("?")[0] for r in server.requests]


def test_an_iteration_is_the_shell_then_three_reads_then_exactly_one_ask(quick_fake):
    vu, writer, _ = make_user(quick_fake, mix=(("live_pool", 1.0),), p_evidence=0.0, p_dossier=0.0)
    vu.run_iteration()
    assert paths(quick_fake) == ["/", "/api/stats", "/api/examples", "/api/freshness", "/api/ask"]
    assert [r["kind"] for r in writer.records] == ["read", "read", "read", "read", "ask", "iteration"]
    assert len(quick_fake.asks) == 1


def test_the_static_bundle_is_fetched_on_the_first_iteration_only(quick_fake):
    vu, _, _ = make_user(quick_fake, mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0, static_paths=("/static/app.js",))
    vu.run_iteration()
    vu.run_iteration()
    assert paths(quick_fake).count("/static/app.js") == 1 and paths(quick_fake).count("/api/ask") == 2


def test_a_cached_ask_repeats_a_listed_example_unsalted(quick_fake):
    vu, writer, salter = make_user(quick_fake, mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0)
    for _ in range(4):
        result = vu.run_iteration()
        assert result["ask"]["salt"] is None and result["ask"]["cached"] is True
    sent = [a["body"]["question"] for a in quick_fake.asks]
    assert set(sent) <= {e["question"] for e in quick_fake.examples} and salter.issued == 0
    assert all(a["body"]["strategy"] == "hybrid" for a in quick_fake.asks)


@pytest.mark.parametrize("klass,strategy", [("live_pool", "hybrid"), ("live_unique", "hybrid"), ("live_agent", "agent")])
def test_every_live_ask_is_salted_with_the_workers_next_counter(quick_fake, klass, strategy):
    vu, writer, salter = make_user(quick_fake, mix=((klass, 1.0),), p_evidence=0.0, p_dossier=0.0, worker=3)
    pool = poolmod.load_pool()
    allowed = {q.text for q in (pool.agent if klass == "live_agent" else pool.live)}
    for expected in range(3):
        result = vu.run_iteration()
        ask = result["ask"]
        assert ask["salt"] == f"3{expected:06d}" and ask["question"].endswith(f" (ref 3{expected:06d})")
        assert salt.split_salt(ask["question"])[0] in allowed and ask["klass"] == klass
    assert {a["body"]["strategy"] for a in quick_fake.asks} == {strategy} and salter.issued == 3
    assert len({a["body"]["question"] for a in quick_fake.asks}) == 3


def test_the_cached_examples_come_from_the_live_examples_response_and_fall_back_to_the_pool(quick_fake):
    quick_fake.examples = [{"id": "Z1", "type": "numeric", "question": "What was Micron's net income for fiscal 2025?"}]
    vu, _, _ = make_user(quick_fake, mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0)
    vu.run_iteration()
    assert quick_fake.asks[-1]["body"]["question"] == "What was Micron's net income for fiscal 2025?"
    quick_fake.examples = []
    vu.run_iteration()
    assert quick_fake.asks[-1]["body"]["question"] in {e["question"] for e in poolmod.load_pool().examples}


def test_evidence_follows_a_citation_of_the_answer_and_dossier_alternates(quick_fake):
    vu, _, _ = make_user(quick_fake, mix=(("live_pool", 1.0),), p_evidence=1.0, p_dossier=1.0, seed=5)
    for _ in range(10):
        vu.run_iteration()
    evidence = [p for p in paths(quick_fake) if p.startswith("/api/evidence/")]
    company = [p for p in paths(quick_fake) if p.startswith("/api/company/")]
    assert len(evidence) == len(company) == 10 and {p.rsplit("/", 1)[1] for p in evidence} <= {"x", "y"}   # the fake answers cite x and y
    assert {p.rsplit("/", 1)[1] for p in company} == {"dossier", "risk-changes"}
    assert {p.split("/")[3] for p in company} <= set(poolmod.load_pool().tickers)


def test_with_no_citation_the_evidence_lookup_uses_a_pool_id(quick_fake):
    vu, _, _ = make_user(quick_fake, mix=(("live_pool", 1.0),), p_evidence=1.0, p_dossier=0.0)
    vu._evidence([])
    path = paths(quick_fake)[-1]
    assert path.startswith("/api/evidence/") and path[len("/api/evidence/"):].replace("%3A", ":") in poolmod.load_pool().evidence_ids


def test_stats_are_handed_to_the_limit_check_on_the_first_iteration_only(quick_fake):
    seen = []
    client, _ = make_client(quick_fake)
    vu = user.VirtualUser(client, poolmod.load_pool(), salt.Salter(0), random.Random(1),
                          user.UserParams(mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0), on_stats=seen.append)
    vu.run_iteration()
    vu.run_iteration()
    assert len(seen) == 1 and seen[0]["agent_enabled"] is True and model.limit_warnings(seen[0]) == []


def test_the_cycle_is_paced_from_the_start_of_the_iteration_and_an_overrun_is_recorded(quick_fake):
    clock = FakeClock()
    vu, writer, _ = make_user(quick_fake, mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0, clock=clock)
    vu.run_iteration()
    iteration = writer.records[-1]
    assert iteration["kind"] == "iteration" and 120 <= iteration["target_s"] <= 300 and iteration["overran"] is False
    assert vu.wait_time() == pytest.approx(iteration["target_s"], abs=1e-3)      # the fake clock did not move: the whole cycle remains
    clock.now += 40.0                                                            # 40 s of iteration (reads + a stream)
    assert vu.wait_time() == pytest.approx(iteration["target_s"] - 40.0, abs=1e-3)
    clock.now += 1000.0
    assert vu.wait_time() == 0.0
    # an overrun is flagged when the iteration itself took longer than its cycle
    slow = user.VirtualUser(make_client(quick_fake)[0], poolmod.load_pool(), salt.Salter(0), random.Random(2),
                            user.UserParams(think_min_s=0.0, think_max_s=0.0, mix=(("cached", 1.0),), p_evidence=0.0, p_dossier=0.0))
    slow.run_iteration()
    assert slow.client.writer.records[-1]["overran"] is True


def test_the_first_delay_is_inside_the_think_range(quick_fake):
    vu, _, _ = make_user(quick_fake, mix=(("cached", 1.0),))
    assert all(0 <= vu.start_delay() <= 300 for _ in range(200))


def test_the_upload_users_are_staggered_across_the_period_and_paced_start_to_start(quick_fake):
    clock = FakeClock()
    delays = []
    for slot in range(5):
        client, _ = make_client(quick_fake)
        uploader = user.UploadVirtualUser(client, user.UserParams(), random.Random(slot), stagger_slot=slot, clock=clock)
        delays.append(uploader.start_delay())
    assert [round(d // 120) for d in delays] == [0, 1, 2, 3, 4] and all(slot * 120 <= d < slot * 120 + 12 for slot, d in enumerate(delays))
    client, writer = make_client(quick_fake)
    uploader = user.UploadVirtualUser(client, user.UserParams(), random.Random(0), clock=clock)
    assert uploader.run_cycle()["outcome"] == "ready" and uploader.run_cycle()["seq"] == 2
    clock.now += 45.0
    assert uploader.wait_time() == pytest.approx(600.0 - 45.0)
    assert len(quick_fake.uploads) == 2 and quick_fake.uploads[0] != quick_fake.uploads[1]


def test_the_phase_clock_follows_the_schedule_and_a_fixed_label_when_the_shape_is_off():
    clock = FakeClock()
    pc = phases.PhaseClock(clock=clock)
    assert pc.phase() == "pre" and not pc.started
    pc.start()
    clock.now += 1.0
    pc.start()                                                                   # idempotent
    seen = {}
    for t, expected in ((0, "ramp"), (599, "ramp"), (600, "steady"), (1799, "steady"), (1800, "spike"), (1980, "soak"),
                        (5579, "soak"), (5580, "fault"), (6179, "fault"), (6180, "after")):
        clock.now = 1000.0 + t
        seen[t] = pc.phase()
        assert seen[t] == expected, t
    clock.now = 1000.0 + 700
    assert pc.t_phase() == pytest.approx(100.0)
    fixed = phases.PhaseClock(shape=False, fixed_phase="soak", clock=clock)
    assert fixed.phase() == "pre"
    fixed.start()
    assert fixed.phase() == "soak"


def test_the_stream_sampler_writes_one_phase_marker_per_change_and_a_sample_per_tick():
    clock, writer, gauge = FakeClock(), records.RecordWriter(), lclient.StreamGauge()
    pc = phases.PhaseClock(clock=clock)
    sampler = phases.StreamSampler(gauge, pc, writer, run_id="r", worker=2, wall=clock)
    sampler.tick()
    assert writer.records == []                                                  # nothing before the first user is spawned
    pc.start()
    gauge.enter(True)
    gauge.enter(False)
    for dt in (0, 1, 1):
        clock.now += dt
        sampler.tick()
    clock.now += 600
    sampler.tick()
    kinds = [r["kind"] for r in writer.records]
    assert kinds == ["phase", "streams", "streams", "streams", "phase", "streams"]
    assert [r["name"] for r in writer.records if r["kind"] == "phase"] == ["ramp", "steady"]
    samples = [r for r in writer.records if r["kind"] == "streams"]
    assert all((s["live_in_flight"], s["total_in_flight"], s["worker"]) == (1, 2, 2) for s in samples)
    assert [(s["phase"], s["t_phase"]) for s in samples] == [("ramp", 0), ("ramp", 1), ("ramp", 2), ("steady", 2)]


# --- END OF SECTION: user ---


# =====================================================================================================================
# packaging: the generator image's files, the launcher, and import hygiene
# =====================================================================================================================

from tools.loadtest import launch  # noqa: E402

STAGING_DIR = ROOT / "deploy" / "staging"
PURE_MODULES = ("model", "sse", "salt", "records", "client", "cpu_watch", "phases", "user", "pool", "launch")
LOADGEN_FILES = [STAGING_DIR / "Dockerfile.loadgen", STAGING_DIR / "fly.loadgen.toml", STAGING_DIR / "requirements-loadgen.in",
                 STAGING_DIR / "requirements-loadgen.txt"]


def test_the_pure_modules_import_with_semigraph_locust_and_gevent_blocked():
    code = ("import sys\nfor name in ('semigraph', 'locust', 'gevent'):\n    sys.modules[name] = None\n"
            f"sys.path.insert(0, {str(ROOT)!r})\nimport importlib\n"
            f"for name in {PURE_MODULES!r}:\n    importlib.import_module('tools.loadtest.' + name)\nprint('ok')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_locustfile_imports_locust_and_no_module_imports_semigraph_at_import_time():
    for path in sorted(LOADTEST_DIR.glob("*.py")):
        imported = _top_level_imports(path)
        assert "semigraph" not in imported, f"{path.name} imports semigraph at module level"
        assert ("locust" in imported or "gevent" in imported) == (path.name == "locustfile.py"), path.name


def test_the_locustfile_parses_without_being_imported():
    tree = ast.parse((LOADTEST_DIR / "locustfile.py").read_text("utf-8"))
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert {"Runtime", "MainUser", "UploadUser"} <= classes


def test_the_dockerfile_installs_hashed_requirements_runs_unprivileged_and_carries_no_semigraph():
    text = (STAGING_DIR / "Dockerfile.loadgen").read_text("utf-8")
    assert text.count("FROM ") == 1 and "FROM python:3.13-slim" in text
    assert "--require-hashes" in text and "--only-binary=:all:" in text and "requirements-loadgen.txt" in text
    assert "COPY tools/loadtest/" in text and "COPY tools/__init__.py" in text
    assert "COPY src" not in text and "COPY . " not in text and "pip install ." not in text                # no semigraph in the image
    assert "\nUSER app" in text and 'CMD ["python", "-m", "tools.loadtest.launch"]' in text
    for line in text.splitlines():
        if line.startswith(("ENV", "ARG")):
            assert not re.search(r"SECRET|TOKEN|PASSWORD|API_KEY|ORIGIN_AUTH", line), line


def test_the_requirements_are_hash_locked_pinned_and_free_of_a_pdf_library():
    text = (STAGING_DIR / "requirements-loadgen.txt").read_text("utf-8")
    assert "uv pip compile deploy/staging/requirements-loadgen.in" in text and "--generate-hashes" in text
    pinned = re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", text, re.M)
    assert len(pinned) >= 20 and ("locust", re.search(r"^locust==(\S+)", text, re.M).group(1)) in pinned
    blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", text.split("\n", 3)[3])
    assert all("--hash=sha256:" in block for block in blocks if re.match(r"[A-Za-z0-9_.\-]+==", block)), "an unhashed requirement"
    assert not re.search(r"^[A-Za-z0-9_.\-]+(?:>=|~=|<=|>|<)", text, re.M)                # no loose pin survives the compile
    assert re.search(r"^locust>=", (STAGING_DIR / "requirements-loadgen.in").read_text("utf-8"), re.M)


def test_the_generator_files_never_name_a_pdf_library_a_provider_key_or_the_live_host():
    paths = LOADGEN_FILES + [p for p in LOADTEST_DIR.iterdir() if p.suffix in (".py", ".md", ".json", ".toml")]
    for path in paths:
        text = path.read_text("utf-8").lower()
        assert "pymupdf" not in text and "fitz" not in text, path.name
        assert "sec_user_agent" not in text, path.name
        assert "semigraph.fly.dev" not in text and "semigraph.internal" not in text, path.name
        assert not re.search(r"sk-[a-z0-9]{20}|anthropic_api_key|openai_api_key", text), path.name


def test_the_fly_toml_is_a_secretless_staging_generator_with_no_inbound_service():
    config = tomllib.loads((STAGING_DIR / "fly.loadgen.toml").read_text("utf-8"))
    assert config["app"] == "semigraph-loadgen-stg" and "stg" in config["app"].split("-") and config["primary_region"] == "sin"
    assert config["vm"] == [{"size": "performance-1x", "memory": "2gb"}]
    assert "http_service" not in config and "services" not in config and "mounts" not in config
    for key, value in config["env"].items():
        assert key.startswith("LOADTEST_") and not re.search(r"SECRET|TOKEN|PASSWORD|KEY|AUTH", key), key
        assert isinstance(value, str) and "fly.dev" not in value and "://" not in value
    assert config["env"]["LOADTEST_ROLE"] == "worker" and config["env"]["LOADTEST_OUT_DIR"] == "/out"


def test_the_docker_context_does_not_exclude_what_the_image_copies():
    ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
               if line.strip() and not line.startswith("#") and not line.startswith("!")]
    for needed in ("tools/loadtest/pool.json", "tools/__init__.py", "deploy/staging/requirements-loadgen.txt"):
        for pattern in ignored:
            stripped = pattern.rstrip("/")
            assert not (needed == stripped or needed.startswith(stripped + "/") or stripped == "*.json"), (needed, pattern)


def test_the_launcher_builds_the_three_roles_and_keeps_secrets_out_of_argv():
    env = {"LOADTEST_ORIGIN_AUTH": "s3cret-origin-value", "LOADTEST_HOST": "http://semigraph-stg.internal:8080",
           "LOADTEST_WORKER_NAME": "w1", "LOADTEST_OUT_DIR": "/out", "LOADTEST_EXPECT_WORKERS": "3", "LOADTEST_WORKER": "1",
           "LOADTEST_MASTER_HOST": "master.vm.semigraph-loadgen-stg.internal"}
    cpu, master = launch.build_commands({**env, "LOADTEST_ROLE": "master"}, python="py")
    assert master[:5] == ["py", "-m", "locust", "-f", "tools/loadtest/locustfile.py"]
    assert "--master" in master and master[master.index("--expect-workers") + 1] == "3" and "--headless" in master
    assert master[master.index("--host") + 1] == "http://semigraph-stg.internal:8080"
    assert cpu == ["py", "-m", "tools.loadtest.cpu_watch", "--name", "gen-w1", "--role", "generator", "--match", "locust",
                   "--out", "/out/cpu/gen-w1.jsonl"]
    _, worker = launch.build_commands({**env, "LOADTEST_ROLE": "worker"}, python="py")
    assert worker[-3:] == ["--worker", "--master-host", "master.vm.semigraph-loadgen-stg.internal"]
    assert "--headless" not in worker and "--host" not in worker
    _, standalone = launch.build_commands({**env, "LOADTEST_ROLE": "standalone"}, python="py")
    assert "--master" not in standalone and "--worker" not in standalone and "--headless" in standalone
    for argv in (cpu, master, worker, standalone):
        assert not any("s3cret" in part for part in argv)


@pytest.mark.parametrize("env,message", [
    ({}, "LOADTEST_ROLE"), ({"LOADTEST_ROLE": "boss"}, "LOADTEST_ROLE"), ({"LOADTEST_ROLE": "master"}, "LOADTEST_HOST"),
    ({"LOADTEST_ROLE": "standalone"}, "LOADTEST_HOST"), ({"LOADTEST_ROLE": "worker"}, "LOADTEST_MASTER_HOST"),
    ({"LOADTEST_ROLE": "worker", "LOADTEST_MASTER_HOST": "m.internal"}, "LOADTEST_WORKER"),
    ({"LOADTEST_ROLE": "worker", "LOADTEST_MASTER_HOST": "m.internal", "LOADTEST_WORKER": "12"}, "LOADTEST_WORKER"),
    ({"LOADTEST_ROLE": "worker", "LOADTEST_MASTER_HOST": "m.internal", "LOADTEST_WORKER": "x"}, "LOADTEST_WORKER")])
def test_the_launcher_refuses_an_incomplete_environment(env, message, capsys):
    with pytest.raises(launch.LaunchError, match=message):
        launch.build_commands(env)
    assert launch.main(env) == 2 and message in capsys.readouterr().err


# --- END OF SECTION: packaging ---
