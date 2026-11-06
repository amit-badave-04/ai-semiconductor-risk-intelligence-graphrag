"""tools/probe/w1b.py: the driver of window W1b (council 7, decision 1, step 3). Nothing here touches Fly, a provider or a database.

The service is an ``httpx.MockTransport`` (``loadtest_fake_server`` has no /healthz and is not this file's), flyctl is a stub
runner, the clock and the sleeps are injected, ``subprocess`` is not used. What is pinned:

* the verdict, fail closed (``boot_verdict``): every pre-registered limit at its boundary (2,400,000 kB and 180 s are
  inclusive), FAIL / INVALID / INCONCLUSIVE / PASS and their order, the aggregator's every branch (it re-derives each verdict,
  needs three different machines, and lets a FAIL win);
* the workload as the real client sends it: the origin header on every request, one client address per ask, every ask salted
  (a verbatim benchmark question is a cached example), never more asks in flight than the live cap, the upload as multipart with
  the workspace token and the stub token in headers, the job stream until a terminal state, a 503 retried;
* the evidence: samples summarised (pids, vanished process, OOM counter), machine events and logs, and that unreadable sources
  give INCONCLUSIVE and never PASS;
* the guards: every flyctl call is an argument list naming exactly the staging API app, the target is that app or loopback (never
  the live host), exactly one machine, and no secret on any printed line or in any argument;
* window W1c (council 8; the last section): W1b with one change, the ask workload. The count of asks is derived (recomputed here from
  the live day cap and hybrid estimate, and shown to follow its inputs), the pacing decision is driven on a fake clock (one ask every
  30 s from accept, anchored; at most 2 in flight; a wait recorded, never a skip; sending stops at job ready plus 30 s), the sender runs
  on threads, a paced boot runs end to end, the aggregator's stop rule (one unchanged re-run of an INVALID boot, then INCOMPLETE; a memory
  FAIL is a FAIL) and its registered-workload check are pinned at every branch, and W1b's own boot and aggregate are shown unchanged.
"""

import io
import json
import re
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import embed_rss, paced_asks, w1b, w1b_judge  # noqa: E402

ORIGIN = "SENTINEL-origin-" + "o" * 40       # gitleaks:allow
ADMIN = "SENTINEL-admin-" + "a" * 40         # gitleaks:allow
WS_ID, WS_TOKEN = "w" * 32, "SENTINEL-wstoken-" + "t" * 30     # gitleaks:allow
SERVICE_URL = "https://semigraph-stg.fly.dev"
APP = "semigraph-stg"
MACHINE = "e286f0aa00ee08"
GUEST = {"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}


# --- a report to judge ---------------------------------------------------------------------------------------------------------

def good_report(**changes):
    report = {
        "window": "W1b", "boot": 1, "machine": {"id": MACHINE, "guest": dict(GUEST)},
        "healthz": {"boot_to_healthy_s": 95.0, "healthy_on_first_poll": False},
        "job": {"state": "ready", "ready_s": 190.0}, "asks": {"sent": 30, "done": 28, "done_overlapping_job": 20, "errors": {}},
        "memory": {"samples": 300, "span_s": 300.0, "pids": [11], "vanished_samples": 0, "max_vmhwm_kb": 2_000_000,
                   "min_mem_available_kb": 1_700_000, "max_swap_used_kb": 0, "swap_total_kb": 1_048_576, "oom_kill_delta": 0},
        "oom_or_restart": {"detected": False, "evidence": [], "unreadable_sources": []},
        "settings_evidence": {"embed_slots": 1}, "workload_s": 290.0}
    report.update(changes)
    report["verdict"] = w1b.boot_verdict(report)[0]
    return report


def with_memory(**changes):
    return good_report(memory={**good_report()["memory"], **changes})


def test_a_good_boot_passes_with_all_three_criteria_true():
    verdict, reasons, criteria = w1b.boot_verdict(good_report())
    assert (verdict, reasons) == ("PASS", [])
    assert criteria == {"vmhwm_within_limit": True, "no_oom_or_restart": True, "healthy_within_limit": True}


def test_the_pre_registered_limits_are_the_ones_of_the_verdict_and_of_the_embedder_probe():
    assert w1b.VMHWM_LIMIT_KB == 2_400_000 == embed_rss.UVICORN_LIMIT_KB and w1b.HEALTHY_LIMIT_S == 180


@pytest.mark.parametrize("peak, verdict", [(2_400_000, "PASS"), (2_399_999, "PASS"), (2_400_001, "FAIL"), (3_000_000, "FAIL")])
def test_the_memory_limit_is_inclusive_at_2_400_000_kb(peak, verdict):
    assert w1b.boot_verdict(with_memory(max_vmhwm_kb=peak))[0] == verdict


@pytest.mark.parametrize("seconds, first_poll, verdict", [(180.0, False, "PASS"), (179.9, False, "PASS"), (180.1, False, "FAIL"),
                                                           (400.0, False, "FAIL"), (None, False, "FAIL"),
                                                           (200.0, True, "INCONCLUSIVE"), (180.0, True, "PASS")])
def test_the_healthy_limit_is_inclusive_at_180_s_and_a_late_first_look_is_inconclusive_not_a_pass(seconds, first_poll, verdict):
    report = good_report(healthz={"boot_to_healthy_s": seconds, "healthy_on_first_poll": first_poll})
    assert w1b.boot_verdict(report)[0] == verdict


def test_an_oom_or_a_restart_fails_and_unreadable_evidence_is_never_a_pass():
    assert w1b.boot_verdict(good_report(oom_or_restart={"detected": True, "evidence": ["oom_kill rose by 1"]}))[0] == "FAIL"
    assert w1b.boot_verdict(good_report(oom_or_restart={"detected": None, "unreadable_sources": ["sampler"]}))[0] == "INCONCLUSIVE"


def test_a_fail_wins_over_invalid_and_inconclusive_and_invalid_wins_over_inconclusive():
    both = good_report(job={"state": "failed"}, memory={**good_report()["memory"], "max_vmhwm_kb": 2_500_000})
    assert w1b.boot_verdict(both)[0] == "FAIL"
    invalid_and_unknown = good_report(job={"state": "timeout"}, oom_or_restart={"detected": None})
    assert w1b.boot_verdict(invalid_and_unknown)[0] == "INVALID"


@pytest.mark.parametrize("changes, text", [
    ({"job": {"state": "failed"}}, "did not reach ready"), ({"job": {"state": "not_started"}}, "did not reach ready"),
    ({"asks": {"done": 5, "done_overlapping_job": 0}}, "no mock-LLM ask finished while"),
    ({"asks": {}}, "no mock-LLM ask finished while"),
    ({"machine": {"id": "m", "guest": {"cpu_kind": "performance", "cpus": 2, "memory_mb": 4096}}}, "not shared-cpu-2x"),
    ({"machine": {"id": "m", "guest": {"cpu_kind": "shared", "cpus": 2, "memory_mb": 2048}}}, "not shared-cpu-2x"),
    ({"settings_evidence": {"embed_slots": 2}}, "embedder slots")])
def test_a_measurement_not_made_under_the_intended_conditions_is_invalid(changes, text):
    verdict, reasons, _ = w1b.boot_verdict(good_report(**changes))
    assert verdict == "INVALID" and any(text in reason for reason in reasons)


def test_no_swap_is_invalid_and_each_unreadable_fact_is_inconclusive():
    assert w1b.boot_verdict(with_memory(swap_total_kb=0))[0] == "INVALID"
    for report, text in ((with_memory(swap_total_kb=None), "swap"), (good_report(machine={"id": "m", "guest": {}}), "class"),
                         (with_memory(max_vmhwm_kb=None), "vmhwm_within_limit"), (with_memory(span_s=100.0), "cover"),
                         (good_report(settings_evidence={}), None)):
        verdict, reasons, _ = w1b.boot_verdict(report)
        if text:
            assert verdict == "INCONCLUSIVE" and any(text in r for r in reasons), (verdict, reasons)
        else:
            assert verdict == "PASS"                              # settings evidence is advisory unless it disagrees


# --- the aggregate -----------------------------------------------------------------------------------------------------------------

def boot(n, machine=None, peak=2_000_000, **changes):
    report = good_report(boot=n, machine={"id": machine or f"m{n}", "guest": dict(GUEST)}, **changes)
    report["memory"] = {**report["memory"], "max_vmhwm_kb": peak}
    report["verdict"] = w1b.boot_verdict(report)[0]
    return report


def test_three_passing_boots_on_three_machines_pass_and_the_spread_is_reported():
    result = w1b.aggregate([boot(1, peak=2_000_000), boot(2, peak=2_100_000), boot(3, peak=2_050_000)])
    assert result["verdict"] == "PASS" and result["problems"] == []
    assert result["max_vmhwm_kb"] == {"values": [2_000_000, 2_100_000, 2_050_000], "max": 2_100_000, "min": 2_000_000, "spread": 100_000,
                                      "margin_to_limit_kb": 300_000}
    assert result["min_mem_available_kb"] == 1_700_000 and result["max_swap_used_kb"] == 0
    assert result["rule"] == {"max_vmhwm_kb": 2_400_000, "boot_to_healthy_s": 180.0, "no_oom_or_restart": True}


def test_one_failing_boot_fails_the_window_even_beside_an_invalid_one_and_even_with_too_few_boots():
    assert w1b.aggregate([boot(1), boot(2, peak=2_400_001), boot(3)])["verdict"] == "FAIL"
    assert w1b.aggregate([boot(1), boot(2, peak=2_400_001), boot(3, job={"state": "failed"})])["verdict"] == "FAIL"
    assert w1b.aggregate([boot(1), boot(2, peak=2_400_001)])["verdict"] == "FAIL"


@pytest.mark.parametrize("reports", [[boot(1), boot(2)], [boot(1), boot(2), boot(3), boot(1, machine="m9")],
                                     [boot(1), boot(2), boot(3, machine="m1")], [boot(1), boot(1, machine="m2"), boot(3)]])
def test_anything_but_three_numbered_boots_on_three_machines_is_incomplete(reports):
    result = w1b.aggregate(reports)
    assert result["verdict"] == "INCOMPLETE" and result["problems"]


def test_invalid_and_inconclusive_boots_never_make_a_pass_and_invalid_is_named_first():
    assert w1b.aggregate([boot(1), boot(2), boot(3, job={"state": "timeout"})])["verdict"] == "INVALID"
    assert w1b.aggregate([boot(1), boot(2), boot(3, oom_or_restart={"detected": None})])["verdict"] == "INCONCLUSIVE"
    assert w1b.aggregate([boot(1), boot(2, job={"state": "failed"}), boot(3, oom_or_restart={"detected": None})])["verdict"] == "INVALID"


def test_the_aggregate_judges_from_the_numbers_not_from_the_verdict_a_file_claims():
    forged = boot(2, peak=2_500_000)
    forged["verdict"] = "PASS"
    result = w1b.aggregate([boot(1), forged, boot(3)])
    assert result["verdict"] == "FAIL" and any("says 'PASS'" in reason for reason in result["boots"][1]["reasons"])


# --- the evidence -------------------------------------------------------------------------------------------------------------------

def sample(t, pid=11, hwm=1_000_000, avail=2_000_000, swap_free=1_048_576, oom=0):
    return {"t": t, "mem_available_kb": avail, "swap_free_kb": swap_free, "swap_total_kb": 1_048_576, "uvicorn_pid": pid,
            "vmrss_kb": None if hwm is None else hwm - 10, "vmhwm_kb": hwm, "oom_kill": oom}


HEADER = {"mem_total_kb": 4_000_000, "swap_total_kb": 1_048_576, "cpus": 2}


def test_the_summary_takes_the_peak_the_floor_the_swap_and_the_span():
    rows = [sample(0, hwm=1_000_000, avail=3_000_000), sample(1, hwm=1_800_000, avail=1_200_000, swap_free=900_000),
            sample(2, hwm=1_700_000, avail=1_500_000, swap_free=1_048_576)]
    summary = w1b.summarize_samples(HEADER, rows)
    assert (summary["max_vmhwm_kb"], summary["min_mem_available_kb"], summary["max_swap_used_kb"]) == (1_800_000, 1_200_000, 148_576)
    assert (summary["samples"], summary["span_s"], summary["pids"], summary["vanished_samples"]) == (3, 2.0, [11], 0)
    assert (summary["swap_total_kb"], summary["mem_total_kb"], summary["cpus"], summary["oom_kill_delta"]) == (1_048_576, 4_000_000, 2, 0)


def test_a_replaced_process_a_vanished_one_and_a_risen_oom_counter_are_all_restart_or_oom_evidence():
    replaced = w1b.summarize_samples(HEADER, [sample(0, pid=11), sample(1, pid=12)])
    assert replaced["pids"] == [11, 12]
    vanished = w1b.summarize_samples(HEADER, [sample(0), sample(1, pid=None, hwm=None), sample(2, pid=None, hwm=None)])
    assert vanished["vanished_samples"] == 2 and vanished["max_vmhwm_kb"] == 1_000_000
    assert w1b.summarize_samples(HEADER, [sample(0, oom=3), sample(1, oom=4)])["oom_kill_delta"] == 1
    assert w1b.summarize_samples({}, [])["max_vmhwm_kb"] is None
    scrub = w1b.Scrub()
    for summary, text in ((replaced, "replaced"), (vanished, "missing"), (w1b.summarize_samples(HEADER, [sample(0, oom=0), sample(1, oom=2)]), "oom_kill")):
        judged = w1b.judge_oom_or_restart(summary, {"events": {"evidence": []}, "logs": []}, scrub)
        assert judged["detected"] is True and any(text in line for line in judged["evidence"])


def test_machine_events_name_exits_restarts_a_second_start_and_an_oom_kill():
    assert w1b.analyze_events({"events": [{"type": "start"}, {"type": "launch"}]}) == {"events": 2, "evidence": []}
    assert w1b.analyze_events({"events": [{"type": "start"}, {"type": "exit"}, {"type": "start"}]})["evidence"] == [
        "machine event 'exit' x1", "machine event 'start' x2 (a restart)"]
    oom = {"events": [{"type": "exit", "request": {"exit_event": {"oom_killed": True}}}]}
    assert "machine exit event: oom_killed" in w1b.analyze_events(oom)["evidence"]
    assert w1b.analyze_events({"no": "events"}) is None and w1b.analyze_events(None) is None


def test_logs_are_searched_for_oom_kills_and_non_zero_exits_only_on_the_measured_machine():
    scrub = w1b.Scrub()
    text = "\n".join([
        f"2026-11-06T03:00:00Z app[{MACHINE}] sin [info]INFO: Application startup complete.",
        f"2026-11-06T03:05:00Z app[{MACHINE}] sin [info][  300.1] Out of memory: Killed process 11 (python)",
        f"2026-11-06T03:05:01Z app[{MACHINE}] sin [info]machine exited with exit code 137, restarting",
        "2026-11-06T03:05:02Z app[OTHER] sin [info]Out of memory: Killed process 5",
        f"2026-11-06T03:05:03Z app[{MACHINE}] sin [info]machine exited with exit code 0",
        f"2026-11-06T03:05:04Z app[{MACHINE}] sin [info]a room with a view"])
    lines = w1b.analyze_logs(text, MACHINE, scrub)
    assert len(lines) == 2 and "Out of memory" in lines[0] and "137" in lines[1]
    assert w1b.analyze_logs("all quiet\n", MACHINE, scrub) == []


def test_the_oom_judgement_needs_the_sampler_and_one_more_source_and_names_what_it_could_not_read():
    scrub, summary = w1b.Scrub(), w1b.summarize_samples(HEADER, [sample(0), sample(1)])
    clean = {"events": {"evidence": []}, "logs": []}
    assert w1b.judge_oom_or_restart(summary, clean, scrub) == {"detected": False, "evidence": [], "unreadable_sources": []}
    assert w1b.judge_oom_or_restart(summary, {"events": {"evidence": []}, "logs": None}, scrub)["detected"] is False
    assert w1b.judge_oom_or_restart(summary, {"events": None, "logs": []}, scrub)["detected"] is False
    none_read = w1b.judge_oom_or_restart(summary, {"events": None, "logs": None}, scrub)
    assert none_read["detected"] is None and none_read["unreadable_sources"] == ["machine events and logs"]
    assert w1b.judge_oom_or_restart(None, clean, scrub)["unreadable_sources"] == ["sampler"]
    assert w1b.judge_oom_or_restart(w1b.summarize_samples({}, []), clean, scrub)["detected"] is None


# --- guards ---------------------------------------------------------------------------------------------------------------------------

def test_every_flyctl_call_must_name_exactly_the_registered_staging_app():
    seen = []
    fly = w1b.Fly(APP, lambda args, timeout: seen.append(args) or w1b.Result(0, b"", b""))
    fly(["machine", "list", "-a", APP, "--json"])
    for bad in (["machine", "list", "-a", "semigraph", "--json"], ["machine", "list", "--json"], ["apps", "list"],
                ["logs", "-a", APP, "-a", "semigraph"], ["ssh", "console", "-a", "semigraph-neo4j-stg"]):
        with pytest.raises(w1b.W1bError):
            fly(bad)
    assert seen == [["flyctl", "machine", "list", "-a", APP, "--json"]]


def test_a_flyctl_timeout_is_a_failed_result_and_a_missing_flyctl_is_a_clean_error():
    import subprocess

    def timing_out(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    assert w1b.Fly(APP, timing_out)(["logs", "-a", APP]).returncode == 124
    with pytest.raises(w1b.W1bError, match="flyctl could not be started"):
        w1b.Fly(APP, lambda args, timeout: (_ for _ in ()).throw(FileNotFoundError()))(["logs", "-a", APP])


@pytest.mark.parametrize("url", ["https://semigraph.fly.dev", "https://semigraph-neo4j.fly.dev", "https://evil.example.com",
                                 "http://semigraph-stg.fly.dev", "https://semigraph-stg.fly.dev.evil.com", "ftp://127.0.0.1",
                                 "https://user:pw@semigraph-stg.fly.dev", "https://semigraph-stg-2.fly.dev", ""])
def test_the_target_is_the_staging_api_or_loopback_never_the_live_site(url):
    with pytest.raises(w1b.W1bError):
        w1b.check_target(url, APP)


def test_the_staging_api_and_loopback_are_accepted():
    assert w1b.check_target("https://semigraph-stg.fly.dev/", APP) == SERVICE_URL
    assert w1b.check_target("http://127.0.0.1:8080", APP) == "http://127.0.0.1:8080"
    assert w1b.check_target("http://localhost:9", APP) == "http://localhost:9"


def test_the_registry_comes_from_windows_json_and_is_never_the_live_app():
    assert w1b.load_registry() == {"api_app": APP}


def test_the_secrets_are_read_from_the_state_file_and_a_missing_one_names_the_command_not_a_value(tmp_path):
    scrub = w1b.Scrub()
    with pytest.raises(w1b.W1bError, match="staging.py create W1b"):
        w1b.load_secrets(tmp_path, scrub)
    (tmp_path / "W1b.json").write_text(json.dumps({"secrets": {"api": {"ORIGIN_AUTH_SECRET": ORIGIN, "ADMIN_TOKEN": ADMIN}}}), encoding="utf-8")
    assert w1b.load_secrets(tmp_path, scrub) == (ORIGIN, ADMIN)
    assert scrub(f"a {ORIGIN} b {ADMIN}") == "a [redacted] b [redacted]"
    (tmp_path / "W1b.json").write_text(json.dumps({"secrets": {"api": {}}}), encoding="utf-8")
    with pytest.raises(w1b.W1bError) as error:
        w1b.load_secrets(tmp_path, w1b.Scrub())
    assert ORIGIN not in str(error.value)


def test_the_time_parser_takes_fly_timestamps_with_nine_fractional_digits_and_zulu():
    assert w1b.parse_time("2026-11-06T03:00:00Z") == datetime(2026, 11, 6, 3, 0, tzinfo=UTC).timestamp()
    assert w1b.parse_time("2026-11-06T03:00:00.123456789Z") == pytest.approx(datetime(2026, 11, 6, 3, 0, tzinfo=UTC).timestamp() + 0.123456)
    assert w1b.parse_time("2026-11-06T03:00:00+00:00") == w1b.parse_time("2026-11-06T03:00:00Z")


def test_the_live_caps_the_load_is_shaped_by_are_the_code_defaults_and_fly_toml_overrides_neither():
    import tomllib
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    live_env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]
    assert w1b.LIVE_MAX_CONCURRENT_ANSWERS == Settings.model_fields["max_concurrent_answers"].default == 2
    assert w1b.EXPECTED_EMBED_SLOTS == Settings.model_fields["embed_slots"].default == 1
    assert "MAX_CONCURRENT_ANSWERS" not in live_env and "EMBED_SLOTS" not in live_env
    live_toml = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["vm"][0]
    assert w1b.EXPECTED_GUEST == {"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096} and live_toml == {"size": "shared-cpu-2x", "memory": "4gb"}


# --- the two pieces of tools/loadtest the driver carries itself (that package is not in the tools image) ----------------------------------

def test_the_salt_is_the_loadtest_salt_and_the_questions_it_takes_are_the_ones_the_loadtest_takes():
    from tools.loadtest import salt as loadtest_salt
    for worker, n in ((1, 0), (1, 29), (0, 1999), (9, 999_999), (2, 1999)):
        assert w1b.apply_salt("What is X?", worker, n) == loadtest_salt.apply_salt("What is X?", worker, n)
    assert w1b.MAX_BASE_CHARS == loadtest_salt.MAX_BASE_CHARS and w1b.MAX_SALT_COUNTER == loadtest_salt.MAX_COUNTER
    for text in ("What was Nvidia's revenue?", "  Mixed   CASE question...  ", "ends with bang!", "plain"):
        assert w1b.normalize_question(text) == loadtest_salt.normalize_question(text)
    for worker, n in ((10, 0), (-1, 0), (1, -1), (1, 1_000_000), ("1", 0), (1, 1.5)):
        with pytest.raises(ValueError):
            w1b.apply_salt("q", worker, n)


STREAMS = [
    b"event: retrieval\ndata: {\"event\": \"retrieval\", \"counts\": {}}\n\nevent: done\ndata: {\"event\": \"done\", \"answer\": \"x\"}\n\n",
    b"event: done\r\ndata: {\"a\": 1}\r\n\r\n", b"event: job\rdata: {\"state\": \"ready\"}\r\r",
    b": ping - 2026-11-06\n\nevent: delta\ndata: {\"text\": \"a\"}\n\n", b"event: delta\ndata: one\ndata: two\n\n",
    b"data: no name\n\n", b"event: job\ndata: {\"s\": \"caf\xc3\xa9 \xe2\x80\x9cq\xe2\x80\x9d\"}\n\n", b"\n\n\n", b"event: done\ndata: unfinished\n"]


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("cut", [None, 1, 2, 7])
def test_the_event_reader_frames_exactly_as_the_loadtest_parser_does_whatever_the_chunk_boundaries(stream, cut):
    from tools.loadtest import sse
    chunks = [stream] if cut is None else [stream[i:i + cut] for i in range(0, len(stream), cut)]
    reference, parser = [], sse.SseParser()
    for chunk in chunks:
        reference += [(m.event, m.data) for m in parser.feed(chunk) if isinstance(m, sse.SseMessage)]
    reader, mine = w1b.SseReader(), []
    for chunk in chunks:
        mine += reader.feed(chunk)
    assert mine == reference


def test_the_event_reader_rejects_bytes_that_are_not_utf8():
    with pytest.raises(UnicodeDecodeError):
        w1b.SseReader().feed(b"data: \xff\xfe\n\n")


def test_an_ask_that_streams_no_terminal_event_is_dropped_a_bad_event_is_a_protocol_error_and_an_error_event_is_named(state_dir):
    def ask_with(content: bytes, status=200):
        transport = httpx.MockTransport(lambda request: httpx.Response(status, headers={"content-type": "text/event-stream"}, content=content))
        ctx = w1b.Context(httpx.Client(transport=transport), SERVICE_URL, ORIGIN, ADMIN, w1b.Scrub(), boot=1)
        return w1b.ask_once(ctx, "A question long enough?", "10.1.0.1")

    done = b"event: done\ndata: {\"event\": \"done\", \"answer\": \"x\"}\n\n"
    assert ask_with(done) == "done"
    assert ask_with(b"event: done\ndata: {\"event\": \"done\", \"cached\": true, \"answer\": \"x\"}\n\n") == "done_cached"
    assert ask_with(b"event: error\ndata: {\"event\": \"error\", \"detail\": \"d\"}\n\n") == "error_event"
    assert ask_with(b"event: delta\ndata: {\"event\": \"delta\", \"text\": \"a\"}\n\n") == "dropped"
    assert ask_with(b"event: done\ndata: not json\n\n") == "protocol_error"
    assert ask_with(b"event: done\ndata: {\"event\": \"error\"}\n\n") == "protocol_error"
    assert ask_with(done, status=429) == "http_429" and ask_with(done, status=503) == "http_503"


# --- the sampler command ------------------------------------------------------------------------------------------------------------

def test_the_sampler_outlives_the_longest_workload_the_driver_allows():
    assert w1b.sampler_duration_s(w1b.JOB_TIMEOUT_S, w1b.TAIL_S) > w1b.JOB_TIMEOUT_S + w1b.TAIL_S + w1b.ASK_CAP_S + w1b.GATE_WAIT_S
    assert w1b.sampler_duration_s(0.3, 0.05) == int(0.3 + 0.05 + w1b.ASK_CAP_S + w1b.GATE_WAIT_S + 300)


def test_a_boot_starts_the_sampler_with_the_duration_derived_from_its_own_timeouts(state_dir, tmp_path):
    _, _, _, _, _, fly = drive(state_dir, tmp_path, extra=("--job-timeout-s", "1000"))
    started = next(c for c in fly.calls if c[1:3] == ["ssh", "console"] and c[-1].startswith("python -c"))
    assert started[-1].endswith(f"--duration-s {w1b.sampler_duration_s(1000, 0.05)}")


def test_the_sampler_command_ships_the_file_as_one_quoted_program_and_one_payload_word():
    import base64
    import shlex
    import zlib
    command = w1b.sampler_command(w1b.memsample_source(), duration_s=900)
    words = shlex.split(command)
    assert words[:2] == ["python", "-c"] and words[2] == w1b.BOOTSTRAP and "'" not in w1b.BOOTSTRAP and '"' not in w1b.BOOTSTRAP
    assert zlib.decompress(base64.b64decode(words[3])).decode() == w1b.memsample_source()
    assert not re.search(r"\s", words[3]) and words[4:] == ["--daemon", "--out", w1b.SAMPLER_OUT, "--stop-file", w1b.SAMPLER_STOP,
                                                           "--duration-s", "900"]
    assert len(command) < 12_000 and ORIGIN not in command


# --- the fake service and the fake flyctl -----------------------------------------------------------------------------------------------

class FakeService:
    """The staging API's wire as the driver uses it. ``MockTransport`` calls the handler on the calling thread."""

    def __init__(self, *, healthy_after=1, job_final="ready", job_asks_needed=2, refuse_create_503=1, ask_refusal_every=0,
                 hang_job=False, embed_slots=1, kill_levels=("on", "off"), ask_seconds=0.01):
        self.__dict__.update(locals())
        self.lock, self.cond = threading.Lock(), threading.Condition()
        self.requests, self.asks, self.uploads, self.health_calls, self.create_calls = [], [], [], 0, 0
        self.inflight = self.max_inflight = self.asks_done = 0
        self.policy_calls = 0
        self.deleted = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        with self.lock:
            self.requests.append({"method": method, "path": path, "headers": dict(request.headers)})
        if path != "/healthz" and request.headers.get("x-origin-auth") != ORIGIN:
            return httpx.Response(403, json={"detail": "Forbidden"})
        if path == "/healthz":
            with self.lock:
                self.health_calls += 1
                ok = self.health_calls > self.healthy_after
            return httpx.Response(200, json={"status": "ok", "db": True, "embedder": "onnx"}) if ok else httpx.Response(503)
        if path == "/api/admin/policy":
            assert request.headers.get("x-admin-token") == ADMIN
            with self.lock:
                level = self.kill_levels[min(self.policy_calls, len(self.kill_levels) - 1)]
                self.policy_calls += 1
            return httpx.Response(200, json={"kill_switch": "off", "effective": level})
        if path == "/api/admin/state":
            return httpx.Response(200, json={"limiters": {"embed": {"borrowed": 0, "total": self.embed_slots}}, "drain": {"draining": False}})
        if path == "/api/workspace" and method == "POST":
            with self.lock:
                self.create_calls += 1
                refuse = self.create_calls <= self.refuse_create_503
            if refuse:
                return httpx.Response(503, json={"detail": "restarting"})
            assert json.loads(request.content)["turnstile_token"]
            return httpx.Response(201, json={"workspace_id": WS_ID, "token": WS_TOKEN, "expires_at": "later", "limits": {}})
        if path == f"/api/workspace/{WS_ID}/documents":
            return self._upload(request)
        if path == f"/api/workspace/{WS_ID}/jobs/jobid0123456789":
            return self._job(request)
        if path == f"/api/workspace/{WS_ID}" and method == "DELETE":
            self.deleted.append(request.headers.get("x-workspace-token"))
            return httpx.Response(204)
        if path == "/api/ask":
            return self._ask(request)
        return httpx.Response(404, json={"detail": "not found"})

    def _upload(self, request):
        if request.headers.get("x-workspace-token") != WS_TOKEN or not request.headers.get("x-turnstile-token"):
            return httpx.Response(404, json={"detail": "workspace not found"})
        assert request.headers["content-type"].startswith("multipart/form-data")
        self.uploads.append(request.content)
        return httpx.Response(202, json={"job_id": "jobid0123456789", "document_id": "d", "version": 1})

    def _job(self, request):
        assert request.headers.get("x-workspace-token") == WS_TOKEN
        if not self.hang_job:
            with self.cond:
                self.cond.wait_for(lambda: self.asks_done >= self.job_asks_needed, timeout=5)
        events = [{"state": "queued"}, {"state": "embedding", "progress": {"done": 3, "total": 14, "eta_s": 10}}]
        if not self.hang_job:
            events.append({"state": self.job_final, "error": "boom" if self.job_final == "failed" else None})
        body = "".join(f"event: job\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    def _ask(self, request):
        body = json.loads(request.content)
        with self.lock:
            self.asks.append({"body": body, "ip": request.headers.get("x-test-client-ip")})
            number = len(self.asks)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(self.ask_seconds)
            if self.ask_refusal_every and number % self.ask_refusal_every == 0:
                return httpx.Response(429, json={"detail": "busy"})
            events = [("retrieval", {"event": "retrieval", "counts": {"chunks": 3}}), ("delta", {"event": "delta", "text": "x"}),
                      ("done", {"event": "done", "answer": "x", "citations": []})]
            content = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events).encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
        finally:
            with self.lock:
                self.inflight -= 1
            with self.cond:
                self.asks_done += 1
                self.cond.notify_all()


class FakeFly:
    """A flyctl stub: answers by what the arguments ask for, records every call."""

    def __init__(self, *, created_ago_s=5.0, machines=1, guest=None, hwm=1_500_000, pid=11, oom_kill=0, events=None,
                 logs="all quiet\n", logs_flag_rejected=False, sampler_ok=True, samples=30, now=time.time, list_script=None):
        self.__dict__.update(locals())
        self.calls = []

    def machine(self, mid=MACHINE, ago=None, state="started"):
        created = datetime.fromtimestamp(self.now() - (self.created_ago_s if ago is None else ago), UTC).isoformat().replace("+00:00", "Z")
        return {"id": mid, "state": state, "region": "sin", "created_at": created, "config": {"guest": self.guest or dict(GUEST)}}

    def jsonl(self, count=None):
        t0, total = self.now(), count or self.samples
        rows = [{"header": {"t": t0, **HEADER}}]
        for i in range(total):
            late = i >= total // 2                          # the peak and an OOM counter that rose come late in the run
            rows.append(sample(t0 + i * 10, pid=self.pid, hwm=self.hwm if late else self.hwm - 300_000, oom=self.oom_kill if late else 0))
        return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        words = args[1:]
        if words[:2] == ["machine", "list"]:
            if self.list_script:                            # successive answers (the last one repeats): a machine that appears later
                rows = self.list_script.pop(0) if len(self.list_script) > 1 else self.list_script[0]
                return w1b.Result(0, json.dumps(rows).encode(), b"")
            return w1b.Result(0, json.dumps([self.machine(f"m{i}") if i else self.machine() for i in range(self.machines)]).encode(), b"")
        if words[:2] == ["machine", "status"]:
            events = self.events if self.events is not None else [{"type": "start"}, {"type": "launch"}]
            return w1b.Result(0, json.dumps({"id": MACHINE, "events": events}).encode(), b"")
        if words[:1] == ["logs"]:
            if self.logs_flag_rejected and "--machine" in words:
                return w1b.Result(1, b"", b"unknown flag: --machine")
            return w1b.Result(0, self.logs.encode(), b"")
        command = words[words.index("-C") + 1] if "-C" in words else ""
        if command.startswith("python -c"):
            return w1b.Result(0 if self.sampler_ok else 1, b"", b"")
        if command.startswith("tail"):
            return w1b.Result(0, self.jsonl(2) if self.sampler_ok else b"", b"")
        if command.startswith("cat"):
            return w1b.Result(0, self.jsonl(), b"")
        return w1b.Result(0, b"", b"")


@pytest.fixture
def state_dir(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "W1b.json").write_text(json.dumps({"secrets": {"api": {"ORIGIN_AUTH_SECRET": ORIGIN, "ADMIN_TOKEN": ADMIN}}}),
                                                 encoding="utf-8")
    return tmp_path / "state"


def drive(state_dir, tmp_path, service=None, fly=None, extra=(), **kwargs):
    """Run ``w1b.main`` for one boot against the fakes; returns (exit code, report or None, stdout, stderr, service, fly)."""
    service, fly = service or FakeService(), fly or FakeFly()
    out_file, out, err = tmp_path / "boot.json", io.StringIO(), io.StringIO()
    client = httpx.Client(transport=httpx.MockTransport(service.handler), timeout=httpx.Timeout(10.0))
    argv = ["boot", "--boot", "1", "--state-dir", str(state_dir), "--tail-s", "0.05", "--salt-start", "100", "--out", str(out_file), *extra]
    with redirect_stdout(out), redirect_stderr(err):
        code = w1b.main(argv, runner=fly, client=client, sleep=lambda s: time.sleep(min(s, 0.01)), **kwargs)
    report = json.loads(out.getvalue()) if out.getvalue().strip() else None
    return code, report, out.getvalue(), err.getvalue(), service, fly


# --- one boot, end to end ----------------------------------------------------------------------------------------------------------------

def test_a_clean_boot_passes_and_carries_the_numbers_the_owner_asked_for(state_dir, tmp_path):
    code, report, out, err, service, fly = drive(state_dir, tmp_path)
    assert code == 0 and report["verdict"] == "PASS" and report["reasons"] == []
    assert report["max_vmhwm_kb"] == 1_500_000 and report["min_mem_available_kb"] == 2_000_000 and report["max_swap_used_kb"] == 0
    assert report["boot_to_healthy_s"] == report["healthz"]["boot_to_healthy_s"] and 5 <= report["boot_to_healthy_s"] < 60
    assert report["job"]["state"] == "ready" and report["job_ready_s"] == report["job"]["ready_s"] and report["job"]["chunks_total"] == 14
    assert report["oom_or_restart"]["detected"] is False and report["oom_or_restart"]["evidence"] == []
    assert report["asks"]["sent"] >= 2 and report["asks"]["done"] >= 2 and report["asks"]["errors"] == {}
    assert report["asks"]["done_overlapping_job"] >= 1 and report["machine"]["id"] == MACHINE
    assert report["pdf"]["pages"] == 30 and len(report["pdf"]["sha256"]) == 64
    assert json.loads((tmp_path / "boot.json").read_text(encoding="utf-8"))["verdict"] == "PASS"


def test_no_secret_reaches_stdout_stderr_the_file_or_any_flyctl_argument(state_dir, tmp_path):
    _, _, out, err, service, fly = drive(state_dir, tmp_path)
    files = (tmp_path / "boot.json").read_text(encoding="utf-8")
    for secret in (ORIGIN, ADMIN, WS_TOKEN, WS_ID):
        assert secret not in out and secret not in err and secret not in files, secret[:18]
        assert not any(secret in word for call in fly.calls for word in call)
    assert service.deleted == [WS_TOKEN]                                 # the workspace was deleted, with its own token


def test_the_secrets_are_scrubbed_even_from_an_error_path(state_dir, tmp_path):
    fly = FakeFly(logs=f"x {ORIGIN} Out of memory: Killed process {ADMIN} app[{MACHINE}]")
    code, report, out, err, *_ = drive(state_dir, tmp_path, fly=fly)
    assert code == 1 and report["verdict"] == "FAIL"
    assert ORIGIN not in out and ADMIN not in out and "[redacted]" in out
    _, _, _, err2, *_ = drive(state_dir, tmp_path, fly=lambda args, timeout: (_ for _ in ()).throw(FileNotFoundError()))
    assert ORIGIN not in err2 and "flyctl could not be started" in err2


def test_every_request_carries_the_origin_header_and_every_ask_its_own_address_and_a_salt(state_dir, tmp_path):
    _, report, _, _, service, _ = drive(state_dir, tmp_path)
    assert service.requests and all(r["headers"].get("x-origin-auth") == ORIGIN for r in service.requests)
    ips = [a["ip"] for a in service.asks]
    assert len(ips) == len(set(ips)) >= 2 and all(re.fullmatch(r"10\.1\.\d+\.\d+", ip) for ip in ips)
    base = set(w1b.load_questions())
    for ask in service.asks:
        body = ask["body"]
        match = re.fullmatch(r"(.+) \(ref 1(\d{6})\)", body["question"])
        assert match and match.group(1) in base and int(match.group(2)) >= 100        # the boot digit, then the counter from --salt-start
        assert body["strategy"] == "hybrid" and body["turnstile_token"] and "workspace_id" not in body
    assert len({a["body"]["question"] for a in service.asks}) == len(service.asks)


def test_never_more_asks_in_flight_than_the_live_cap(state_dir, tmp_path):
    service = FakeService(job_asks_needed=12)
    drive(state_dir, tmp_path, service=service)
    assert 1 <= service.max_inflight <= w1b.LIVE_MAX_CONCURRENT_ANSWERS == 2
    with pytest.raises(SystemExit):
        w1b.main(["boot", "--boot", "1", "--concurrency", "3"])
    with pytest.raises(SystemExit):
        w1b.main(["boot", "--boot", "0"])


def test_the_upload_is_the_max_size_pdf_as_multipart_with_the_workspace_and_stub_tokens_in_headers(state_dir, tmp_path):
    _, report, _, _, service, _ = drive(state_dir, tmp_path)
    assert len(service.uploads) == 1
    pdf = w1b.load_pdf()
    assert pdf in service.uploads[0] and pdf.startswith(b"%PDF") and len(pdf) < 15 * 1024 * 1024
    assert pdf == w1b.load_pdf() and b"filename=\"w1b-max-size.pdf\"" in service.uploads[0]
    upload = next(r for r in service.requests if r["path"].endswith("/documents"))
    assert upload["headers"]["x-workspace-token"] == WS_TOKEN and upload["headers"]["x-turnstile-token"]
    assert report["pdf"]["bytes"] == len(pdf)


def test_the_fixture_pdf_has_the_thirty_pages_of_the_m4_gate():
    pdf = w1b.load_pdf()
    assert pdf.count(b"/Type /Page ") == 30 and w1b.PDF_PAGES == 30 and w1b.PDF_CHARS_PER_PAGE == 2700


def test_a_503_on_the_first_workspace_create_is_retried_and_counted(state_dir, tmp_path):
    service = FakeService(refuse_create_503=2)
    _, report, _, _, service, _ = drive(state_dir, tmp_path, service=service)
    assert service.create_calls == 3 and report["retries"] == 2 and report["verdict"] == "PASS"


def test_the_kill_level_is_awaited_through_the_admin_route_before_anything_is_asked(state_dir, tmp_path):
    service = FakeService(kill_levels=("on", "on", "on", "off"))
    _, report, _, _, service, _ = drive(state_dir, tmp_path, service=service)
    assert service.policy_calls == 4 and report["kill_level"] == "off"
    first_ask = next(i for i, r in enumerate(service.requests) if r["path"] == "/api/ask")
    last_policy = max(i for i, r in enumerate(service.requests) if r["path"] == "/api/admin/policy")
    assert last_policy < first_ask


def test_refused_asks_are_counted_by_status_and_do_not_void_the_boot(state_dir, tmp_path):
    service = FakeService(ask_refusal_every=3, job_asks_needed=6)
    _, report, *_ = drive(state_dir, tmp_path, service=service)
    assert report["asks"]["errors"].get("http_429", 0) >= 1 and report["asks"]["done"] >= 2 and report["verdict"] == "PASS"


@pytest.mark.parametrize("job_final", ["failed"])
def test_a_job_that_fails_or_never_finishes_makes_the_boot_invalid(state_dir, tmp_path, job_final):
    _, report, *_ = drive(state_dir, tmp_path, service=FakeService(job_final=job_final))
    assert report["verdict"] == "INVALID" and report["job"]["state"] == "failed" and report["job"]["error"] == "boom"
    _, hung, *_ = drive(state_dir, tmp_path, service=FakeService(hang_job=True), extra=("--job-timeout-s", "0.3"))
    assert hung["verdict"] == "INVALID" and hung["job"]["state"] == "timeout" and hung["job_ready_s"] is None


def test_a_memory_peak_over_the_limit_fails_the_boot_and_the_exit_code_says_so(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, fly=FakeFly(hwm=2_400_001))
    assert code == 1 and report["verdict"] == "FAIL" and report["max_vmhwm_kb"] == 2_400_001
    code, report, *_ = drive(state_dir, tmp_path, fly=FakeFly(hwm=2_400_000))
    assert code == 0 and report["verdict"] == "PASS"


@pytest.mark.parametrize("fly_kwargs, text", [
    ({"events": [{"type": "start"}, {"type": "exit", "request": {"exit_event": {"oom_killed": True}}}, {"type": "start"}]}, "oom_killed"),
    ({"oom_kill": 2}, "oom_kill"), ({"logs": f"app[{MACHINE}] sin [info]Out of memory: Killed process 11\n"}, "Out of memory")])
def test_an_oom_kill_or_a_restart_fails_the_boot_whichever_source_shows_it(state_dir, tmp_path, fly_kwargs, text):
    code, report, *_ = drive(state_dir, tmp_path, fly=FakeFly(**fly_kwargs))
    assert code == 1 and report["verdict"] == "FAIL" and report["oom_or_restart"]["detected"] is True
    assert any(text in line for line in report["oom_or_restart"]["evidence"])


def test_a_replaced_serving_process_in_the_samples_fails_the_boot(state_dir, tmp_path):
    fly = FakeFly()
    rows = [json.loads(line) for line in fly.jsonl().decode().splitlines()]
    for row in rows[16:]:
        row["uvicorn_pid"] = 12
    fly.jsonl = lambda count=None: ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    _, report, *_ = drive(state_dir, tmp_path, fly=fly)
    assert report["verdict"] == "FAIL" and any("replaced" in line for line in report["oom_or_restart"]["evidence"])


def test_a_log_call_that_rejects_the_machine_flag_falls_back_to_the_apps_logs_filtered_by_machine(state_dir, tmp_path):
    fly = FakeFly(logs_flag_rejected=True, logs=f"app[{MACHINE}] sin [info]fine\napp[OTHER] sin [info]Out of memory: Killed process 5\n")
    _, report, *_ = drive(state_dir, tmp_path, fly=fly)
    assert report["verdict"] == "PASS" and [c for c in fly.calls if c[1] == "logs"][-1].count("--machine") == 0


def test_unreadable_machine_evidence_gives_inconclusive_not_a_pass(state_dir, tmp_path):
    class Blind(FakeFly):
        def __call__(self, args, timeout):
            if args[1] in ("machine",) and args[2] == "status" or args[1] == "logs":
                return w1b.Result(1, b"", b"nope")
            return super().__call__(args, timeout)

    code, report, *_ = drive(state_dir, tmp_path, fly=Blind())
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["oom_or_restart"]["detected"] is None


def test_a_wrong_machine_class_is_invalid_and_a_second_machine_stops_the_run_before_any_request(state_dir, tmp_path):
    _, report, *_ = drive(state_dir, tmp_path, fly=FakeFly(guest={"cpu_kind": "performance", "cpus": 1, "memory_mb": 2048}))
    assert report["verdict"] == "INVALID" and any("not shared-cpu-2x" in r for r in report["reasons"])
    service = FakeService()
    code, report, out, err, *_ = drive(state_dir, tmp_path, service=service, fly=FakeFly(machines=2))
    assert code == 2 and report is None and "exactly one machine" in err and service.requests == []


def old_and_new(fly):
    old, new = fly.machine("old-machine", ago=3600.0, state="destroyed"), fly.machine(MACHINE, ago=4.0, state="created")
    return old, new


def test_started_before_the_deploy_the_driver_waits_for_the_new_machine_and_never_takes_the_old_one(state_dir, tmp_path):
    fly = FakeFly()
    old, new = old_and_new(fly)
    still_there = {**old, "state": "started"}
    fly.list_script = [[], [still_there], [still_there, new], [new]]          # nothing, the old one alone, both, the new one alone
    service = FakeService()
    code, report, _, _, _, _ = drive(state_dir, tmp_path, service=service, fly=fly, extra=("--wait-for-machine-s", "60"))
    assert code == 0 and report["machine"]["id"] == MACHINE and report["verdict"] == "PASS"
    lists = [c for c in fly.calls if c[1:3] == ["machine", "list"]]
    assert len(lists) >= 4 and report["boot_to_healthy_s"] < 60                  # timed from the NEW machine's creation (4 s before)
    assert service.health_calls >= 2 and service.uploads and "old-machine" not in json.dumps(report)


def test_waiting_for_a_machine_gives_up_with_the_reason_when_only_the_old_one_is_there_or_none_ever_comes(state_dir, tmp_path):
    clock = {"t": time.time()}
    fly = FakeFly(now=lambda: clock["t"])
    old = {**fly.machine("old-machine", ago=3600.0), "state": "started"}
    ticking = dict(now=lambda: clock["t"], sleep=lambda s: clock.update(t=clock["t"] + s))
    fly.list_script = [[old]]
    with pytest.raises(w1b.MachineCountError, match="before this run started"):
        w1b.wait_for_machine(w1b.Fly(APP, fly), None, wait_s=10, not_before=clock["t"] - 120, **ticking)
    fly.list_script = [[]]
    with pytest.raises(w1b.MachineCountError, match="found 0"):
        w1b.wait_for_machine(w1b.Fly(APP, fly), None, wait_s=10, not_before=clock["t"] - 120, **ticking)
    fly.list_script = [[old]]
    assert w1b.wait_for_machine(w1b.Fly(APP, fly), None, wait_s=0, not_before=0, **ticking)["id"] == "old-machine"   # no wait: any one machine
    with pytest.raises(w1b.MachineCountError, match="--machine-id"):
        w1b.wait_for_machine(w1b.Fly(APP, fly), "another", wait_s=0, not_before=0, **ticking)


def test_a_sampler_that_cannot_start_voids_the_boot_before_any_load(state_dir, tmp_path):
    _, report, _, _, service, _ = drive(state_dir, tmp_path, fly=FakeFly(sampler_ok=False))
    assert report["verdict"] == "INVALID" and service.uploads == [] and service.asks == []
    assert "sampler" in report["job"]["error"]


def test_a_boot_that_is_never_healthy_fails_without_a_workload(state_dir, tmp_path):
    clock = {"t": time.time()}

    def now():
        clock["t"] += 50.0
        return clock["t"]

    service = FakeService(healthy_after=10**9)
    code, report, *_ = drive(state_dir, tmp_path, service=service, fly=FakeFly(now=lambda: clock["t"]), now=now)
    assert code == 1 and report["verdict"] == "FAIL" and report["boot_to_healthy_s"] is None
    assert service.uploads == [] and service.asks == [] and report["memory"] == {}


def test_a_machine_already_healthy_at_the_first_look_and_older_than_the_limit_is_inconclusive(state_dir, tmp_path):
    code, report, *_ = drive(state_dir, tmp_path, service=FakeService(healthy_after=0), fly=FakeFly(created_ago_s=400.0))
    assert code == 2 and report["verdict"] == "INCONCLUSIVE" and report["healthz"]["healthy_on_first_poll"] is True
    assert report["boot_to_healthy_s"] > 180 and any("healthy_within_limit" in r for r in report["reasons"])


def test_the_live_host_is_refused_before_a_single_request_or_flyctl_call(state_dir, tmp_path):
    service, fly = FakeService(), FakeFly()
    code, report, out, err, *_ = drive(state_dir, tmp_path, service=service, fly=fly, extra=("--base-url", "https://semigraph.fly.dev"))
    assert code == 2 and report is None and "staging API" in err and service.requests == [] and fly.calls == []


def test_a_missing_state_file_says_which_command_creates_it_and_runs_nothing(tmp_path):
    service, fly = FakeService(), FakeFly()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = w1b.main(["boot", "--boot", "1", "--state-dir", str(tmp_path / "none")], runner=fly,
                        client=httpx.Client(transport=httpx.MockTransport(service.handler)))
    assert code == 2 and "staging.py create W1b" in err.getvalue() and service.requests == [] and fly.calls == []


def test_every_flyctl_call_of_a_boot_is_an_argument_list_naming_the_staging_app(state_dir, tmp_path):
    _, _, _, _, _, fly = drive(state_dir, tmp_path)
    assert fly.calls and all(call[0] == "flyctl" and all(isinstance(word, str) for word in call) for call in fly.calls)
    for call in fly.calls:
        assert [call[i + 1] for i, word in enumerate(call) if word in ("-a", "--app")] == [APP], call
    kinds = [tuple(call[1:3]) for call in fly.calls]
    assert kinds[0] == ("machine", "list") and ("ssh", "console") in kinds and ("machine", "status") in kinds and ("logs", "-a") in kinds


def test_aggregate_runs_from_the_command_line_and_the_exit_code_follows_the_verdict(tmp_path, capsys):
    paths = []
    for n in (1, 2, 3):
        path = tmp_path / f"boot-{n}.json"
        path.write_text(json.dumps(boot(n)), encoding="utf-8")
        paths.append(str(path))
    assert w1b.main(["aggregate", *paths, "--out", str(tmp_path / "w1b.json")]) == 0
    assert json.loads((tmp_path / "w1b.json").read_text(encoding="utf-8"))["verdict"] == "PASS"
    bad = boot(3, peak=2_400_001)
    Path(paths[2]).write_text(json.dumps(bad), encoding="utf-8")
    assert w1b.main(["aggregate", *paths]) == 1
    assert w1b.main(["aggregate", *paths[:2]]) == 2
    capsys.readouterr()


# --- the docs name the commands --------------------------------------------------------------------------------------------------------------

def test_the_readme_and_windows_json_name_the_whole_w1b_sequence():
    readme = (ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8")
    for needle in ("staging.py quote W1b", "create W1b", "seed W1b", "deploy W1b mockllm", "--build-arg EMBEDDER_VARIANT=fp32", "preflight W1b",
                   "machine destroy", "tools.probe.w1b boot", "tools.probe.w1b aggregate", "snapshot W1b", "destroy W1b --yes", "UNVERIFIED",
                   "--wait-for-machine-s"):
        assert needle in readme, needle
    steps = " ".join(json.loads((ROOT / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))["windows"]["W1b"]["steps"])
    for needle in ("tools.probe.w1b boot", "tools.probe.w1b aggregate", "machine destroy", "EMBEDDER_VARIANT=fp32", "--wait-for-machine-s"):
        assert needle in steps, needle
    assert "--wait-for-machine-s" in readme and steps.index("START the driver BEFORE the deploy") < steps.rindex("scripts/staging.py deploy W1b api")
    assert re.findall(r"--wait-for-machine-s (\d+)", readme) and set(re.findall(r"--wait-for-machine-s (\d+)", readme + steps)) == {"2700"}
    assert "KEEP_UNPATCHED=1" in readme                                     # said, as the thing NOT to pass


# =====================================================================================================================================
# Window W1c: W1b with ONE change, the ask workload (council 8, "The Recommendation" 2). Same apps, toml, build-argument policy,
# machines, thresholds and INVALID rule; 14 paced asks instead of a saturating load. Nothing here touches Fly or a provider.
# =====================================================================================================================================

WINDOWS_JSON = ROOT / "deploy" / "staging" / "windows.json"
REASON = ("W1b's saturating ask load (~150 asks) starved the upload past its 1,200 s embed timeout on the throttled machine "
          "(boot 2 INVALID twice, memory within limits each time); council 8 chose a paced load anchored to the live day cap; "
          "the memory thresholds are unchanged.")
STOP_RULE = ("an INVALID boot is re-run unchanged ONCE; if still INVALID, W1c is INCOMPLETE and nothing else is amended; "
             "a memory FAIL is a FAIL")
ESTIMATE_FIELDS = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens", "llm_input_price_per_mtok",
                   "llm_output_price_per_mtok", "agent_max_model_calls", "agent_max_tool_calls", "max_question_chars")


def registered(window="W1c"):
    return json.loads(WINDOWS_JSON.read_text(encoding="utf-8"))["windows"][window]


def squash(text):
    return " ".join(text.split())


def live_env():
    import tomllib
    return tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]


def recomputed_ask_count():
    """The count worked out HERE, apart from the module under test: the production day cap over the hybrid estimate of the live
    models (fly.toml's models and every estimate field explicit, as tests/test_serve_estimate.py builds them), in micro-dollars."""
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph import config
    from semigraph.config import Settings
    from semigraph.serve import estimate
    env = live_env()
    settings = Settings(_env_file=None, **{name: env.get(name.upper(), Settings.model_fields[name].default) for name in ESTIMATE_FIELDS})
    cap, hybrid = round(config.PRODUCTION_MAX_SPEND_USD_PER_DAY * 1_000_000), estimate.estimate_micro("hybrid", settings)
    return cap // hybrid, cap, hybrid


# --- the 14: derived from the live day cap and the live hybrid estimate, never written into the code -----------------------------------

def test_the_ask_count_is_worked_out_from_the_live_day_cap_and_the_live_hybrid_estimate():
    count, cap, hybrid = recomputed_ask_count()
    derived = paced_asks.derive_ask_count()
    assert (cap, hybrid, count) == (10_000_000, 709_573, 14)
    assert (derived["day_cap_micro"], derived["hybrid_estimate_micro"], derived["asks"]) == (cap, hybrid, count)
    assert derived["day_cap_usd"] == 10.0 and derived["hybrid_estimate_usd"] == 0.709573
    assert derived["formula"] == "floor(day_cap_micro / hybrid_estimate_micro)" and derived["unrounded"] == pytest.approx(14.093, abs=1e-3)
    env = live_env()
    assert (env["ANSWER_MODEL"], env["ESCALATION_MODEL"]) == ("openai/gpt-6-luna", "anthropic/claude-sonnet-5")
    assert derived["models"] == {"answer": env["ANSWER_MODEL"], "escalation": env["ESCALATION_MODEL"]}


def test_the_production_day_cap_the_derivation_reads_is_the_code_default_and_fly_toml_does_not_override_it():
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph import config
    from semigraph.config import Settings
    assert config.PRODUCTION_MAX_SPEND_USD_PER_DAY == Settings.model_fields["max_spend_usd_per_day"].default == 10.0
    assert "MAX_SPEND_USD_PER_DAY" not in live_env()


def test_the_ask_count_follows_its_inputs_so_it_is_not_a_number_written_into_the_code(monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph import config
    from semigraph.serve import estimate
    monkeypatch.setattr(config, "PRODUCTION_MAX_SPEND_USD_PER_DAY", 20.0)
    assert paced_asks.derive_ask_count()["asks"] == 20_000_000 // 709_573 == 28
    monkeypatch.setattr(estimate, "estimate_micro", lambda ask_type, settings: 2_500_000)
    derived = paced_asks.derive_ask_count()
    assert derived["asks"] == 8 and derived["hybrid_estimate_micro"] == 2_500_000 and derived["day_cap_micro"] == 20_000_000


def test_a_day_cap_that_admits_no_hybrid_ask_is_refused_not_rounded_to_zero(monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph import config
    monkeypatch.setattr(config, "PRODUCTION_MAX_SPEND_USD_PER_DAY", 0.5)
    with pytest.raises(ValueError, match="admits no"):
        paced_asks.derive_ask_count()


def test_w1c_registers_the_derived_count_and_the_paced_schedule_and_w1b_registers_no_workload():
    workload = registered()["workload"]
    assert workload["asks"] == paced_asks.derive_ask_count()["asks"] == 14
    assert (workload["mode"], workload["interval_s"], workload["max_in_flight"], workload["tail_s"]) == ("paced", 30, 2, 30)
    assert workload["max_in_flight"] == w1b.LIVE_MAX_CONCURRENT_ANSWERS and workload["tail_s"] == w1b.TAIL_S
    assert (w1b_judge.W1C_INTERVAL_S, w1b_judge.W1C_MAX_IN_FLIGHT, w1b_judge.W1C_TAIL_S) == (30.0, 2, 30.0)
    assert w1b_judge.W1C_MODE == paced_asks.PACED_MODE == workload["mode"]
    assert "workload" not in registered("W1b")
    assert w1b.load_registry() == {"api_app": APP}
    assert w1b.load_registry(window="W1c") == {"api_app": APP, "workload": workload}


def test_w1c_is_w1b_with_one_change_the_ask_workload_the_same_apps_toml_image_machines_and_quote():
    w1b_window, w1c = registered("W1b"), registered("W1c")
    for key in ("apps", "toml_overrides", "build_args", "machines", "volume_gb_hours", "quote_usd", "cap_usd"):
        assert w1c[key] == w1b_window[key], key
    assert w1c["build_args"]["api"] == {"require": ["EMBEDDER_VARIANT=fp32"], "refuse": ["KEEP_UNPATCHED=1"]}
    assert w1c["toml_overrides"] == {"api": "deploy/staging/fly.w1b.toml"}
    assert (w1c["quote_usd"], w1c["cap_usd"]) == (0.10, 0.25) and "W1c" in w1c["note"]


def test_the_readme_and_windows_json_state_the_reason_the_derivation_the_pacing_and_the_stop_rule_of_w1c():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    w1c = registered()
    for text in (readme, squash(w1c["note"])):
        assert REASON in text and STOP_RULE in text
        assert "floor(10 / 0.709573) = floor(14.09) = 14" in text and "one ask every 30 s" in text and "from upload accept" in text
        assert "at most 2 in flight" in text and "job ready plus 30 s" in text and "different X-Test-Client-IP" in text
        assert "the memory thresholds are unchanged" in text


def test_the_readme_and_windows_json_name_the_whole_w1c_sequence():
    readme = squash((ROOT / "tools" / "probe" / "README.md").read_text(encoding="utf-8"))
    steps = squash(" ".join(registered()["steps"]))
    for text in (readme, steps):
        for needle in ("staging.py quote W1c", "create W1c --org", "seed W1c", "deploy W1c mockllm", "deploy W1c api --build-arg EMBEDDER_VARIANT=fp32",
                       "preflight W1c", "machine destroy", "tools.probe.w1b boot --window W1c", "--attempt 2", "tools.probe.w1b aggregate",
                       "snapshot W1c", "destroy W1c --yes", "--wait-for-machine-s 2700"):
            assert needle in text, needle
    assert steps.index("START the driver BEFORE the deploy") < steps.rindex("scripts/staging.py deploy W1c api")
    # the stop rule is in the commands, not left to the operator: only an INVALID boot is re-run (decided from its JSON), once, and the
    # aggregate is given every boot file of the directory so no attempt can be left out
    assert '$first.verdict -ne "INVALID"' in readme and 'Test-Path "$dir\\boot-$n-a2.json"' in readme
    assert 'Get-ChildItem "$dir\\boot-*.json"' in readme and "aggregate $files" in readme and "$n = 2" not in readme
    assert "only a boot whose JSON verdict is INVALID" in steps.replace("Only", "only") and "boot-*.json" in steps and "no attempt can be left out" in steps
    assert "approve-quote 0.10" in readme and "approve-quote 0.10" in steps
    assert "KEEP_UNPATCHED=1" in readme and set(re.findall(r"--wait-for-machine-s (\d+)", readme + steps)) == {"2700"}


# --- the schedule: a pure decision on a fake clock ----------------------------------------------------------------------------------------

def simulate(durations, *, count=14, interval=30.0, max_in_flight=2, stop_at=None, t0=0.0):
    """Drive ``paced_asks.next_step`` on a fake clock: an ask lasts ``durations[n % len]`` seconds; a wait advances the clock by what the
    step asks for. Returns the asks that went out (planned, sent, end times) and the reason the sender stopped."""
    now, sent, ends, log = t0, 0, [], []
    for _ in range(100_000):
        ends = [end for end in ends if end > now]
        step = paced_asks.next_step(now=now, t0=t0, interval_s=interval, count=count, sent=sent, in_flight=len(ends),
                                    max_in_flight=max_in_flight, stop_at=stop_at)
        if step.action == "stop":
            return log, step.reason
        if step.action == "send":
            log.append({"n": sent, "planned": t0 + interval * sent, "at": now, "ends": now + durations[sent % len(durations)]})
            ends.append(log[-1]["ends"])
            sent += 1
        else:
            assert step.action == "wait" and step.wait_s > 0
            now += step.wait_s
    raise AssertionError("the scheduler never stopped")


def test_the_planned_times_are_one_ask_every_30_s_from_the_upload_accept_and_the_14_asks_span_390_s():
    offsets = paced_asks.planned_offsets(14, 30.0)
    assert offsets == [30.0 * n for n in range(14)] and offsets[0] == 0.0 and offsets[-1] == 390.0 and len(offsets) == 14


def test_asks_that_finish_in_time_go_out_exactly_on_schedule_and_the_sender_stops_at_the_cap():
    log, reason = simulate([5.0])
    assert [e["at"] for e in log] == [30.0 * n for n in range(14)] and reason == "cap"
    assert [e["at"] for e in log] == [e["planned"] for e in log]


def test_with_two_asks_in_flight_the_next_waits_and_is_neither_skipped_nor_is_the_schedule_shifted():
    log, reason = simulate([100.0])                 # every ask lasts 100 s: the third is due at 60 s with both slots taken
    assert reason == "cap" and [e["n"] for e in log] == list(range(14))                      # none skipped
    assert [e["planned"] for e in log] == [30.0 * n for n in range(14)]                      # anchored to the accept, never re-anchored
    assert log[2]["planned"] == 60.0 and log[2]["at"] == pytest.approx(100.0, abs=0.1)       # it waited for the first to end
    assert all(e["at"] >= e["planned"] for e in log) and any(e["at"] - e["planned"] > 1 for e in log)
    for e in log:
        assert sum(1 for other in log if other["at"] <= e["at"] < other["ends"]) <= w1b.LIVE_MAX_CONCURRENT_ANSWERS


def test_sending_stops_at_job_ready_plus_30_s_and_an_ask_due_at_that_very_moment_is_not_sent():
    log, reason = simulate([5.0], stop_at=210.0)    # ask 7 is planned at 210 s, the moment sending stops
    assert [e["n"] for e in log] == list(range(7)) and reason == "job_ready_plus_tail"
    log, reason = simulate([5.0], stop_at=210.5)
    assert [e["n"] for e in log] == list(range(8)) and reason == "job_ready_plus_tail"


def test_a_job_that_outlasts_the_schedule_gets_no_more_asks_the_14_is_a_cap_not_a_target():
    log, reason = simulate([5.0], stop_at=100_000.0)
    assert len(log) == 14 and reason == "cap" and max(e["at"] for e in log) == 390.0


def test_an_ask_still_waiting_for_a_slot_when_sending_stops_is_never_sent():
    log, reason = simulate([100.0], stop_at=95.0)   # asks 0 and 1 hold both slots until 100 s and 130 s
    assert [e["n"] for e in log] == [0, 1] and reason == "job_ready_plus_tail"


@pytest.mark.parametrize("bad", [{"interval_s": 0}, {"interval_s": -1.0}, {"max_in_flight": 0}, {"count": 0}])
def test_a_schedule_that_is_not_a_positive_number_of_asks_at_a_positive_interval_is_refused(bad):
    kwargs = {"ask": lambda n: "done", "now": time.time, "sleep": time.sleep, "t0": 0.0, "count": 14, "interval_s": 30.0, "max_in_flight": 2, **bad}
    with pytest.raises(ValueError):
        paced_asks.PacedSender(**kwargs)


# --- the sender, on threads and a real clock with a short interval --------------------------------------------------------------------------

def make_sender(ask, *, count=6, interval_s=0.04, max_in_flight=2):
    return paced_asks.PacedSender(ask=ask, now=time.time, sleep=lambda s: time.sleep(min(s, 0.01)), t0=time.time(), count=count,
                                  interval_s=interval_s, max_in_flight=max_in_flight)


def test_the_sender_records_planned_and_actual_send_times_and_never_exceeds_two_asks_in_flight():
    running, lock = {"now": 0, "max": 0}, threading.Lock()

    def ask(n):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.15)                            # longer than the interval: the third ask has to wait for a slot
        with lock:
            running["now"] -= 1
        return "done"

    sender = make_sender(ask)
    sender.start()
    assert sender.wait(10)
    sender.finish()
    summary = sender.summary(sender.t0, sender.t0 + 5)
    schedule = summary["schedule"]
    assert [e["n"] for e in schedule] == list(range(6)) and summary["sent"] == 6 and summary["unsent"] == 0 and summary["stopped_by"] == "cap"
    assert summary["planned_offsets_s"] == pytest.approx([0.04 * n for n in range(6)])
    assert [e["planned_s"] for e in schedule] == pytest.approx([0.04 * n for n in range(6)])
    assert all(e["sent_s"] >= e["planned_s"] - 0.005 and e["end_s"] >= e["sent_s"] for e in schedule)
    assert schedule[0]["sent_s"] < 0.1 and any(e["delay_s"] > 0.02 for e in schedule)       # the first at once; a wait is recorded
    assert running["max"] == 2 == summary["max_in_flight_seen"] and summary["done"] == 6
    assert all(e["outcome"] == "done" for e in schedule) and summary["done_overlapping_job"] == 6


def test_the_sender_stops_when_told_and_records_what_it_never_sent():
    sender = make_sender(lambda n: "done", count=14, interval_s=0.1)
    sender.start()
    time.sleep(0.25)
    sender.stop_sending_at(time.time())             # the job is ready and the tail has gone by
    sender.finish()
    summary = sender.summary(sender.t0, time.time())
    assert 2 <= summary["sent"] <= 4 and summary["unsent"] == 14 - summary["sent"] and summary["stopped_by"] == "job_ready_plus_tail"
    assert len(summary["planned_offsets_s"]) == 14


def test_the_ask_summary_counts_done_cached_errors_and_the_asks_that_overlapped_the_job():
    records = [{"start": 0, "end": 5, "outcome": "done"}, {"start": 6, "end": 7, "outcome": "done_cached"},
               {"start": 8, "end": 9, "outcome": "http_429"}, {"start": 20, "end": 25, "outcome": "done"}]
    expected = {"sent": 4, "done": 2, "cached": 1, "done_overlapping_job": 1, "errors": {"http_429": 1}}
    assert paced_asks.summarize_records(records, 1, 10) == expected
    ctx = w1b.Context(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))), SERVICE_URL, ORIGIN, ADMIN,
                      w1b.Scrub(), boot=1)
    loop = w1b.AskLoop(ctx, ["q"], 1, 0)
    loop.records = records
    assert loop.summary(1, 10) == expected          # W1b's loop reports through the same function, unchanged


# --- one W1c boot, end to end -----------------------------------------------------------------------------------------------------------------

def write_w1c_state(state_dir):
    (state_dir / "W1c.json").write_text(json.dumps({"secrets": {"api": {"ORIGIN_AUTH_SECRET": ORIGIN, "ADMIN_TOKEN": ADMIN}}}), encoding="utf-8")


def w1c_windows(tmp_path, **workload):
    """A copy of windows.json whose W1c workload is changed (a test-sized interval and tail: the real file is never edited)."""
    doc = json.loads(WINDOWS_JSON.read_text(encoding="utf-8"))
    doc["windows"]["W1c"]["workload"].update(workload)
    path = tmp_path / "windows-w1c.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def drive_w1c(state_dir, tmp_path, *, service=None, fly=None, interval_s=0.05, tail_s=0.05, extra=(), **workload):
    write_w1c_state(state_dir)
    windows = w1c_windows(tmp_path, interval_s=interval_s, tail_s=tail_s, **workload)
    return drive(state_dir, tmp_path, service=service, fly=fly,
                 extra=("--window", "W1c", "--windows", str(windows), "--tail-s", str(tail_s), *extra))


def test_a_w1c_boot_records_its_window_mode_derivation_and_a_schedule_of_planned_against_actual_sends(state_dir, tmp_path):
    code, report, out, err, service, fly = drive_w1c(state_dir, tmp_path, service=FakeService(job_asks_needed=14))
    assert code == 0 and report["verdict"] == "PASS" and report["window"] == "W1c" and report["attempt"] == 1
    workload = report["workload"]
    assert workload["mode"] == "paced" and workload["asks_cap"] == 14 == workload["derivation"]["asks"]
    assert (workload["interval_s"], workload["max_in_flight"], workload["tail_s"]) == (0.05, 2, 0.05)
    assert workload["derivation"]["day_cap_micro"] == 10_000_000 and workload["derivation"]["hybrid_estimate_micro"] == 709_573
    asks = report["asks"]
    assert asks["sent"] == 14 == len(asks["schedule"]) and asks["unsent"] == 0 and asks["stopped_by"] == "cap"
    assert asks["planned_offsets_s"] == pytest.approx([0.05 * n for n in range(14)])
    assert [e["n"] for e in asks["schedule"]] == list(range(14))
    for entry in asks["schedule"]:
        assert entry["sent_s"] >= entry["planned_s"] - 0.01 and entry["delay_s"] == pytest.approx(entry["sent_s"] - entry["planned_s"], abs=2e-3)
    assert asks["schedule"][0]["sent_s"] < 0.5 and asks["max_in_flight_seen"] <= 2 and asks["distinct_addresses"] == 14
    ips = [a["ip"] for a in service.asks]
    assert len(set(ips)) == 14 and all(re.fullmatch(r"10\.1\.\d+\.\d+", ip) for ip in ips) and service.max_inflight <= 2
    assert len({a["body"]["question"] for a in service.asks}) == 14 and all("(ref 1" in a["body"]["question"] for a in service.asks)


def test_a_w1c_boot_stops_sending_at_job_ready_plus_the_tail_and_says_how_many_went_out(state_dir, tmp_path):
    _, report, *_ = drive_w1c(state_dir, tmp_path, service=FakeService(job_asks_needed=2), interval_s=0.3)
    asks = report["asks"]
    assert asks["sent"] == 2 and asks["unsent"] == 12 and asks["stopped_by"] == "job_ready_plus_tail" and report["verdict"] == "PASS"
    assert len(asks["planned_offsets_s"]) == 14 and report["job"]["ready_s"] >= 0.3


def test_a_w1c_boot_with_asks_that_outlast_the_interval_waits_records_the_delay_and_keeps_two_in_flight(state_dir, tmp_path):
    service = FakeService(job_asks_needed=14, ask_seconds=0.12)
    _, report, *_ = drive_w1c(state_dir, tmp_path, service=service, interval_s=0.03)
    asks = report["asks"]
    assert asks["sent"] == 14 and service.max_inflight <= 2 == asks["max_in_flight_seen"] and any(e["delay_s"] > 0.05 for e in asks["schedule"])
    assert [e["planned_s"] for e in asks["schedule"]] == pytest.approx([0.03 * n for n in range(14)], abs=2e-3)


def test_a_w1c_boot_reads_the_secrets_of_its_own_state_file(state_dir, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = w1b.main(["boot", "--boot", "1", "--window", "W1c", "--state-dir", str(state_dir)], runner=FakeFly(),
                        client=httpx.Client(transport=httpx.MockTransport(FakeService().handler)))
    assert code == 2 and "staging.py create W1c" in err.getvalue() and "W1b.json" not in err.getvalue()   # the W1b file is there and is not read


def test_w1c_refuses_to_run_when_the_registered_count_is_not_the_one_the_code_derives(state_dir, tmp_path):
    service, fly = FakeService(), FakeFly()
    code, report, out, err, *_ = drive_w1c(state_dir, tmp_path, service=service, fly=fly, asks=13)
    assert code == 2 and report is None and "derived" in err and "13" in err and service.requests == [] and fly.calls == []


@pytest.mark.parametrize("flags", [(), ("--tail-s", "30", "--concurrency", "1")])
def test_w1c_refuses_a_command_line_that_changes_the_registered_tail_or_the_asks_in_flight(state_dir, tmp_path, flags):
    write_w1c_state(state_dir)
    service, fly = FakeService(), FakeFly()
    code, report, out, err, *_ = drive(state_dir, tmp_path, service=service, fly=fly, extra=("--window", "W1c", *flags))
    assert code == 2 and report is None and "registers" in err and service.requests == [] and fly.calls == []     # drive's own tail is 0.05


def test_a_rerun_is_attempt_2_of_w1c_and_only_w1c_has_one(state_dir, tmp_path):
    _, report, *_ = drive_w1c(state_dir, tmp_path, service=FakeService(job_asks_needed=3), extra=("--attempt", "2"))
    assert report["attempt"] == 2 and report["window"] == "W1c" and report["verdict"] == "PASS"
    service, fly = FakeService(), FakeFly()
    code, report, out, err, *_ = drive(state_dir, tmp_path, service=service, fly=fly, extra=("--attempt", "2"))
    assert code == 2 and report is None and "W1c" in err and service.requests == [] and fly.calls == []
    with pytest.raises(SystemExit):
        w1b.main(["boot", "--boot", "1", "--window", "W1c", "--attempt", "3"])
    with pytest.raises(SystemExit):
        w1b.main(["boot", "--boot", "1", "--window", "W9"])


def test_a_w1b_boot_is_unchanged_it_runs_the_saturating_loop_and_records_no_schedule(state_dir, tmp_path):
    _, report, *_ = drive(state_dir, tmp_path, service=FakeService(job_asks_needed=12))
    assert report["window"] == "W1b" and report["attempt"] == 1 and report["workload"]["mode"] == "saturating"
    assert report["workload"]["concurrency"] == 2 and "schedule" not in report["asks"] and "unsent" not in report["asks"]
    assert report["verdict"] == "PASS" and report["asks"]["sent"] >= 12


# --- the W1c aggregate: the stop rule -------------------------------------------------------------------------------------------------------------

W1C_WORKLOAD = {"mode": "paced", "asks_cap": 14, "interval_s": 30.0, "max_in_flight": 2, "tail_s": 30.0,
                "derivation": {"asks": 14, "day_cap_micro": 10_000_000, "hybrid_estimate_micro": 709_573}}
INVALID_JOB = {"state": "timeout"}


def w1c_boot(n, attempt=1, machine=None, peak=2_000_000, workload=None, **changes):
    report = good_report(boot=n, window="W1c", attempt=attempt, workload=json.loads(json.dumps(W1C_WORKLOAD)) if workload is None else workload,
                         machine={"id": machine or f"m{n}-{attempt}", "guest": dict(GUEST)}, **changes)
    report["memory"] = {**report["memory"], "max_vmhwm_kb": peak}
    report["verdict"] = w1b.boot_verdict(report)[0]
    return report


def test_three_passing_w1c_boots_pass_and_the_result_names_the_window_and_the_registered_workload():
    result = w1b.aggregate([w1c_boot(1), w1c_boot(2), w1c_boot(3, peak=2_100_000)])
    assert result["window"] == "W1c" and result["verdict"] == "PASS" and result["problems"] == []
    assert result["rerun_due"] == [] and result["stopped"] == [] and [r["boot"] for r in result["boots"]] == [1, 2, 3]
    assert result["rule"] == {"max_vmhwm_kb": 2_400_000, "boot_to_healthy_s": 180.0, "no_oom_or_restart": True,
                              "workload": {"interval_s": 30.0, "max_in_flight": 2, "tail_s": 30.0}}
    assert result["max_vmhwm_kb"]["max"] == 2_100_000 and result["max_vmhwm_kb"]["margin_to_limit_kb"] == 300_000
    assert "nothing else is amended" in result["stop_rule"]


def test_an_invalid_boot_rerun_once_and_passing_lets_the_window_pass_with_the_invalid_attempt_kept_in_the_record():
    result = w1b.aggregate([w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(2, attempt=2), w1c_boot(3)])
    assert result["verdict"] == "PASS" and result["problems"] == []
    assert [(r["boot"], r["attempt"], r["verdict"]) for r in result["attempts"]] == [(1, 1, "PASS"), (2, 1, "INVALID"), (2, 2, "PASS"), (3, 1, "PASS")]
    assert [(r["boot"], r["attempt"]) for r in result["boots"]] == [(1, 1), (2, 2), (3, 1)]


def test_an_invalid_boot_that_has_not_been_rerun_yet_is_incomplete_and_names_the_boot_to_rerun():
    result = w1b.aggregate([w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(3)])
    assert result["verdict"] == "INCOMPLETE" and result["rerun_due"] == [2] and result["stopped"] == [] and result["problems"] == []


def test_a_boot_that_is_invalid_twice_stops_the_window_as_incomplete_never_as_invalid():
    result = w1b.aggregate([w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(2, attempt=2, job=INVALID_JOB), w1c_boot(3)])
    assert result["verdict"] == "INCOMPLETE" and result["stopped"] == [2] and result["rerun_due"] == []
    assert [r["verdict"] for r in result["attempts"]] == ["PASS", "INVALID", "INVALID", "PASS"]
    assert w1b.aggregate([w1c_boot(n, job=INVALID_JOB) for n in (1, 2, 3)])["verdict"] == "INCOMPLETE"       # not "INVALID", as W1b would say


@pytest.mark.parametrize("reports, text", [
    ([w1c_boot(1), w1c_boot(1, attempt=2), w1c_boot(2), w1c_boot(3)], "only an INVALID boot is re-run"),              # a re-run of a PASS
    ([w1c_boot(1, oom_or_restart={"detected": None}), w1c_boot(1, attempt=2), w1c_boot(2), w1c_boot(3)],
     "only an INVALID boot is re-run"),                                                                              # of an INCONCLUSIVE
    ([w1c_boot(1, job=INVALID_JOB), w1c_boot(1, attempt=2, job=INVALID_JOB), w1c_boot(1, attempt=3), w1c_boot(2), w1c_boot(3)],
     "re-run once, not more"),                                                                                       # a second re-run
    ([w1c_boot(1, attempt=2), w1c_boot(2), w1c_boot(3)], "without gaps or repeats"),                                  # no attempt 1 file
    ([w1c_boot(1, job=INVALID_JOB), w1c_boot(1, job=INVALID_JOB, machine="other"), w1c_boot(2), w1c_boot(3)], "without gaps or repeats")])
def test_a_rerun_the_stop_rule_does_not_allow_is_a_problem_and_never_a_pass(reports, text):
    result = w1b.aggregate(reports)
    assert result["verdict"] == "INCOMPLETE" and any(text in problem for problem in result["problems"]), result["problems"]


def test_every_attempt_must_be_on_its_own_freshly_created_machine():
    for reports in ([w1c_boot(1), w1c_boot(2, machine="m1-1"), w1c_boot(3)],
                    [w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(2, attempt=2, machine="m2-1"), w1c_boot(3)]):
        result = w1b.aggregate(reports)
        assert result["verdict"] == "INCOMPLETE" and any("own freshly created machine" in problem for problem in result["problems"])
    assert w1b.aggregate([w1c_boot(1), w1c_boot(2)])["verdict"] == "INCOMPLETE"                            # boot 3 is missing


def test_a_memory_fail_in_any_attempt_is_a_fail_beside_an_invalid_boot_a_rerun_or_too_few_boots():
    assert w1b.aggregate([w1c_boot(1), w1c_boot(2, peak=2_400_001), w1c_boot(3)])["verdict"] == "FAIL"
    assert w1b.aggregate([w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(2, attempt=2, peak=2_400_001), w1c_boot(3)])["verdict"] == "FAIL"
    assert w1b.aggregate([w1c_boot(1, peak=2_400_001)])["verdict"] == "FAIL"
    assert w1b.aggregate([w1c_boot(1), w1c_boot(2, oom_or_restart={"detected": True, "evidence": ["x"]}), w1c_boot(3)])["verdict"] == "FAIL"


def test_an_inconclusive_boot_is_not_rerun_by_this_rule_and_the_window_is_inconclusive():
    result = w1b.aggregate([w1c_boot(1), w1c_boot(2, oom_or_restart={"detected": None}), w1c_boot(3)])
    assert result["verdict"] == "INCONCLUSIVE" and result["rerun_due"] == [] and result["stopped"] == [] and result["problems"] == []


def test_w1b_and_w1c_boot_files_are_never_aggregated_together_and_a_file_without_a_window_is_w1b():
    mixed = w1b.aggregate([boot(1), boot(2), w1c_boot(3)])
    assert mixed["verdict"] == "INCOMPLETE" and any("different windows" in problem for problem in mixed["problems"])
    legacy = boot(1)
    del legacy["window"]                                                      # the recorded W1b boot files predate the window key
    result = w1b.aggregate([legacy, boot(2), boot(3)])
    assert result["window"] == "W1b" and result["verdict"] == "PASS" and "rerun_due" not in result


@pytest.mark.parametrize("workload", [
    {**W1C_WORKLOAD, "interval_s": 0.05}, {**W1C_WORKLOAD, "max_in_flight": 1}, {**W1C_WORKLOAD, "tail_s": 0.05},
    {**W1C_WORKLOAD, "mode": "saturating"}, {**W1C_WORKLOAD, "asks_cap": 13}, {**W1C_WORKLOAD, "asks_cap": 15},
    {**W1C_WORKLOAD, "derivation": {**W1C_WORKLOAD["derivation"], "hybrid_estimate_micro": 1_000_000}},
    {**W1C_WORKLOAD, "derivation": {}}, {}])
def test_a_w1c_boot_that_did_not_run_the_registered_workload_can_never_aggregate_to_a_pass(workload):
    result = w1b.aggregate([w1c_boot(1, workload=workload), w1c_boot(2), w1c_boot(3)])
    assert result["verdict"] == "INCOMPLETE" and any("registered workload" in problem for problem in result["problems"]), result["problems"]


def test_the_w1c_aggregate_runs_from_the_command_line_and_the_exit_code_follows_the_verdict(tmp_path, capsys):
    def write(reports):
        paths = []
        for i, report in enumerate(reports):
            path = tmp_path / f"boot-{i}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            paths.append(str(path))
        return paths

    assert w1b.main(["aggregate", *write([w1c_boot(1), w1c_boot(2), w1c_boot(3)]), "--out", str(tmp_path / "w1c.json")]) == 0
    assert json.loads((tmp_path / "w1c.json").read_text(encoding="utf-8"))["window"] == "W1c"
    assert w1b.main(["aggregate", *write([w1c_boot(1), w1c_boot(2, job=INVALID_JOB), w1c_boot(3)])]) == 2
    assert w1b.main(["aggregate", *write([w1c_boot(1), w1c_boot(2, peak=2_400_001), w1c_boot(3)])]) == 1
    capsys.readouterr()
