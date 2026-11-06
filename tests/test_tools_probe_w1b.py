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
  the live host), exactly one machine, and no secret on any printed line or in any argument.
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

from tools.probe import embed_rss, w1b  # noqa: E402

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
                 hang_job=False, embed_slots=1, kill_levels=("on", "off")):
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
            time.sleep(0.01)
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
