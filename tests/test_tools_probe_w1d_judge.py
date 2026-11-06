"""tools/probe/w1d_judge.py: the rules of window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Staging"). Pure functions, no I/O.

W1d is one fresh shared-cpu-2x / 4 GB boot of the fp32 image, a burn to use up the burst balance, ONE max-size upload and no asks. What is
pinned, each at its boundary:

* the ``upload stages`` line (``src/semigraph/uploads/jobs.py`` ``_log_stages``): read out of a flyctl log line whatever the prefix
  flyctl puts before it, matched to the job by its id, the failure variant of the line and the neighbouring ``upload embed`` line ignored;
* the validity rule: share = tokens / (85.8 x 1.18 x embed_s), from a line of a job that reached ``ready`` only; INVALID when the share is
  above 0.15 (exactly 0.15 is valid) or ``embed_s`` is under 0.9 x the harness's ``ready_s`` (exactly 0.9 x is valid); the arithmetic is
  exact (fractions), so a boundary is not at the mercy of a float;
* PASS: valid, ``ready``, uvicorn VmHWM at most 2,400,000 kB (inclusive); FAIL: the job ended ``timeout`` while valid (the share is NEVER
  computed from a timeout line, whose ``tokens=`` is the plan), or a memory / OOM criterion violated; INVALID: the measurement was not
  made as intended; INCONCLUSIVE: it cannot be judged (no line, no state, unreadable evidence), never a PASS;
* the aggregate: one boot, one unchanged re-run of an INVALID boot, INCOMPLETE after two INVALID boots, a FAIL anywhere is a FAIL.
"""

import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import w1b_judge, w1d_judge  # noqa: E402

GUEST = {"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}
JOB_ID = "a1b2c3d4e5f6"
PREFIX = "2026-11-06T10:12:03Z app[e286f0aa00ee08] sin [info]2026-11-06 10:12:03,123 INFO semigraph.uploads.jobs "
READY_LINE = (PREFIX + f"upload stages ws_hash=ab12cd34ef56 job_id={JOB_ID} parse_s=12.250 chunk_s=1.500 embed_s=1300.000 "
              "compare_s=0.000 index_s=5.125 chunks=60 fresh=60 done=60 tokens=16000")


def stage_fields(**changes):
    fields = {"parse_s": 12.25, "chunk_s": 1.5, "embed_s": 1300.0, "compare_s": 0.0, "index_s": 5.125, "chunks": 60, "fresh": 60,
              "done": 60, "tokens": 16000}
    fields.update(changes)
    return {key: value for key, value in fields.items() if value is not None}


def good_report(**changes):
    report = {
        "window": "W1d", "boot": 1, "attempt": 1, "machine": {"id": "m1", "guest": dict(GUEST)},
        "job": {"state": "ready", "job_id": JOB_ID, "ready_s": 1330.0, "end_s": 1330.0, "error": None},
        "stages": {"source": "stream", "fields": stage_fields()},
        "memory": {"samples": 2900, "span_s": 2900.0, "pids": [11], "vanished_samples": 0, "max_vmhwm_kb": 2_000_000,
                   "min_mem_available_kb": 1_500_000, "max_swap_used_kb": 0, "swap_total_kb": 1_048_576, "oom_kill_delta": 0},
        "oom_or_restart": {"detected": False, "evidence": [], "unreadable_sources": []}, "workload_s": 2900.0}
    report.update(changes)
    return report


def verdict(**changes):
    return w1d_judge.boot_verdict(good_report(**changes))[0]


def timeout_report(**field_changes):
    """A job that the embed ceiling stopped: ``tokens=`` is the PLAN, ``done`` is below ``fresh``."""
    fields = stage_fields(**{"embed_s": 2101.5, "chunks": 60, "fresh": 60, "done": 41, "tokens": 16000, **field_changes})
    return good_report(job={"state": "failed", "job_id": JOB_ID, "ready_s": None, "end_s": 2125.0,
                            "error": {"code": "timeout", "message": "processing took too long and was stopped"}},
                       stages={"source": "stream", "fields": fields})


# --- the constants: the spec's numbers --------------------------------------------------------------------------------------------------

def test_the_constants_are_the_specs_numbers():
    assert w1d_judge.FULL_CORE_TOK_S == Fraction("85.8") and w1d_judge.CLASS_FACTOR == Fraction("1.18")
    assert w1d_judge.SHARE_LIMIT == Fraction("0.15") and w1d_judge.EMBED_READY_RATIO == Fraction("0.9")
    assert w1d_judge.VMHWM_LIMIT_KB == w1b_judge.VMHWM_LIMIT_KB == 2_400_000
    assert w1d_judge.EMBED_CEILING_S == 2100 and w1d_judge.WINDOW == "W1d" and w1d_judge.MAX_ATTEMPTS == 2


# --- reading the line ------------------------------------------------------------------------------------------------------------------

def test_the_line_is_read_out_of_a_flyctl_log_line_whatever_its_prefix():
    (line,) = w1d_judge.parse_stage_lines("noise\n" + READY_LINE + "\nmore noise\n")
    assert line["job_id"] == JOB_ID and line["ws_hash"] == "ab12cd34ef56"
    assert line["fields"] == stage_fields() and isinstance(line["fields"]["tokens"], int) and isinstance(line["fields"]["embed_s"], float)
    bare = "upload stages ws_hash=ab12 job_id=j2 chunk_s=0.125 chunks=130"
    assert w1d_judge.parse_stage_lines(bare)[0]["fields"] == {"chunk_s": 0.125, "chunks": 130}    # a stage not reached has no field


# Copied from a recorded W1c app log (`flyctl logs --no-tail`, 2026-11-06, machine 2862626a6e2d48; kept in the session scratchpad, not in the repository): the prefix
# flyctl adds, the logging format of serve/main.py (basicConfig) and real neighbours. They show that INFO lines of the app's own loggers reach flyctl logs.
RECORDED_NEIGHBOURS = [
    "2026-11-06T05:03:30Z app[2862626a6e2d48] sin [info]2026-11-06 05:03:30,049 WARNING semigraph.serve.main example X1 refused: failed checks: ungrounded_number",
    "2026-11-06T05:03:30Z app[2862626a6e2d48] sin [info]2026-11-06 05:03:30,049 INFO semigraph.serve.main seeded 51 of 53 benchmark answers into the cache "
    "(snapshot snap-20260924-97c6597d58)",
    "2026-11-06T05:03:24Z app[2862626a6e2d48] sin [info]2026-11-06 05:03:24,825 INFO semigraph.graph.schema schema applied (idempotent) - 36 statements",
]


def test_a_stage_line_in_the_recorded_log_format_is_read_among_real_neighbours():
    prefix = "2026-11-06T05:41:12Z app[2862626a6e2d48] sin [info]2026-11-06 05:41:12,401 INFO semigraph.uploads.jobs "
    stage = prefix + "upload stages ws_hash=0123456789ab job_id=fake0123456789 parse_s=9.500 chunk_s=1.250 embed_s=1250.250 chunks=60 fresh=60 done=60 tokens=15849"
    lines = w1d_judge.parse_stage_lines("\n".join([*RECORDED_NEIGHBOURS, stage, RECORDED_NEIGHBOURS[0]]))
    assert [line["job_id"] for line in lines] == ["fake0123456789"]
    assert lines[0]["fields"] == {"parse_s": 9.5, "chunk_s": 1.25, "embed_s": 1250.25, "chunks": 60, "fresh": 60, "done": 60, "tokens": 15849}


def test_the_failure_variant_and_the_neighbouring_embed_line_are_not_stage_lines():
    text = "\n".join([PREFIX + f"upload stages line failed job_id={JOB_ID}: TypeError",
                      PREFIX + "upload embed ws_hash=ab12 document_id=d new_tokens=16000 wall_s=1300.0 tok_s=12.3",
                      PREFIX + f"upload job failed ws_hash=ab12 job_id={JOB_ID} code=timeout"])
    assert w1d_judge.parse_stage_lines(text) == []


def test_a_line_cut_short_or_with_a_field_that_is_not_a_number_is_skipped_not_guessed():
    text = f"upload stages ws_hash=ab12 job_id={JOB_ID} embed_s=12x.0 tokens=16000\nupload stages ws_hash=ab12\n"
    assert w1d_judge.parse_stage_lines(text) == []


def test_the_line_of_the_job_is_matched_by_its_whole_id():
    lines = w1d_judge.parse_stage_lines(READY_LINE + "\n" + READY_LINE.replace(JOB_ID, JOB_ID + "ff"))
    assert len(lines) == 2
    assert w1d_judge.stage_line_for(lines, JOB_ID)["job_id"] == JOB_ID
    assert w1d_judge.stage_line_for(lines, JOB_ID + "ff")["job_id"] == JOB_ID + "ff"
    assert w1d_judge.stage_line_for(lines, JOB_ID[:-1]) is None and w1d_judge.stage_line_for(lines, "") is None
    assert w1d_judge.stage_line_for([], JOB_ID) is None


# --- the share ---------------------------------------------------------------------------------------------------------------------------

def test_the_share_is_tokens_over_full_core_rate_times_class_factor_times_embed_seconds():
    assert w1d_judge.embed_share(16000, 1300.0) == Fraction(16000) / (Fraction("85.8") * Fraction("1.18") * 1300)
    assert float(w1d_judge.embed_share(16000, 1317.0)) == pytest.approx(0.12, abs=1e-3)       # the spec's 12 % row: 1,317 s
    assert w1d_judge.embed_share(16000, 0) is None and w1d_judge.embed_share(16000, -1.0) is None


# --- the validity rule at its boundaries -----------------------------------------------------------------------------------------------------

# 75,933 / (85.8 x 1.18 x 5,000) = 0.15 exactly (85.8 x 1.18 = 101.244; 101.244 x 5,000 x 0.15 = 75,933). Not a real job: the arithmetic.
EXACT_TOKENS, EXACT_EMBED_S = 75933, 5000.0


def test_a_share_of_exactly_point_one_five_is_valid_and_one_more_token_is_not():
    assert w1d_judge.embed_share(EXACT_TOKENS, EXACT_EMBED_S) == Fraction("0.15")
    exact = good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": 5200.0, "end_s": 5200.0, "error": None},
                        stages={"source": "stream", "fields": stage_fields(embed_s=EXACT_EMBED_S, tokens=EXACT_TOKENS)})
    assert w1d_judge.boot_verdict(exact)[0] == "PASS"
    over = {**exact, "stages": {"source": "stream", "fields": stage_fields(embed_s=EXACT_EMBED_S, tokens=EXACT_TOKENS + 1)}}
    verdict_, reasons, criteria = w1d_judge.boot_verdict(over)
    assert verdict_ == "INVALID" and criteria["share_within_limit"] is False
    assert any("above 0.15" in reason for reason in reasons)


def test_the_cap_job_is_valid_from_about_1054_seconds_of_embedding_and_invalid_below():
    # the spec: "for the cap job, embed_s under about 1,054 s" is a job that ran faster than the slow end this boot tests
    def with_embed(seconds):
        return good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": seconds + 30, "end_s": seconds + 30, "error": None},
                           stages={"source": "stream", "fields": stage_fields(embed_s=seconds)})
    assert w1d_judge.boot_verdict(with_embed(1054.0))[0] == "PASS"
    assert w1d_judge.boot_verdict(with_embed(1053.0))[0] == "INVALID"


def test_an_embed_time_of_exactly_point_nine_of_ready_s_is_valid_and_a_thousandth_less_is_not():
    def with_times(embed_s, ready_s):
        return good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": ready_s, "end_s": ready_s, "error": None},
                           stages={"source": "stream", "fields": stage_fields(embed_s=embed_s)})
    assert w1d_judge.boot_verdict(with_times(1170.0, 1300.0))[0] == "PASS"                     # 0.9 x 1300.0 = 1170.0 exactly
    verdict_, reasons, criteria = w1d_judge.boot_verdict(with_times(1169.999, 1300.0))
    assert verdict_ == "INVALID" and criteria["embed_dominates_ready"] is False
    assert any("0.9" in reason and "ready_s" in reason for reason in reasons)
    assert w1d_judge.boot_verdict(with_times(1300.0, 1300.0))[0] == "PASS"
    # a ready_s with one decimal is read as written (1174.3 x 0.9 = 1056.87), not as the float nearest to it
    assert w1d_judge.boot_verdict(with_times(1056.87, 1174.3))[0] == "PASS"
    assert w1d_judge.boot_verdict(with_times(1056.869, 1174.3))[0] == "INVALID"


def test_a_job_that_was_not_throttled_is_invalid_not_a_pass_w1c_boot_3_was_such_a_job():
    fast = good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": 387.6, "end_s": 387.6, "error": None},
                       stages={"source": "stream", "fields": stage_fields(embed_s=370.0)})
    verdict_, reasons, _ = w1d_judge.boot_verdict(fast)
    assert verdict_ == "INVALID" and any("above 0.15" in r for r in reasons)


def test_both_validity_reasons_are_listed_when_both_fail():
    both = good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": 900.0, "end_s": 900.0, "error": None},
                       stages={"source": "stream", "fields": stage_fields(embed_s=200.0)})
    _, reasons, criteria = w1d_judge.boot_verdict(both)
    assert criteria["share_within_limit"] is False and len(reasons) >= 1
    assert [c for c, ok in criteria.items() if ok is False] == ["share_within_limit", "embed_dominates_ready"]


# --- PASS, the memory limit, OOM ----------------------------------------------------------------------------------------------------------

def test_a_valid_ready_job_with_the_memory_inside_the_limit_passes():
    verdict_, reasons, criteria = w1d_judge.boot_verdict(good_report())
    assert (verdict_, reasons) == ("PASS", [])
    assert criteria == {"job_ended_as_judged": True, "share_within_limit": True, "embed_dominates_ready": True,
                        "vmhwm_within_limit": True, "no_oom_or_restart": True}


@pytest.mark.parametrize("peak, expected", [(2_399_999, "PASS"), (2_400_000, "PASS"), (2_400_001, "FAIL")])
def test_the_memory_limit_is_inclusive_at_2_400_000_kb(peak, expected):
    assert verdict(memory={**good_report()["memory"], "max_vmhwm_kb": peak}) == expected


def test_an_oom_or_a_restart_fails_and_unreadable_evidence_is_never_a_pass():
    assert verdict(oom_or_restart={"detected": True, "evidence": ["oom"], "unreadable_sources": []}) == "FAIL"
    assert verdict(oom_or_restart={"detected": None, "evidence": [], "unreadable_sources": ["sampler"]}) == "INCONCLUSIVE"


def test_a_memory_fail_wins_over_invalid_as_in_w1b_and_w1c():
    over = {**good_report()["memory"], "max_vmhwm_kb": 2_500_000}
    fast = {"state": "ready", "job_id": JOB_ID, "ready_s": 387.6, "end_s": 387.6, "error": None}
    assert verdict(job=fast, stages={"source": "stream", "fields": stage_fields(embed_s=370.0)}, memory=over) == "FAIL"


def test_no_memory_reading_is_inconclusive_and_so_are_samples_that_do_not_cover_the_workload():
    assert verdict(memory={**good_report()["memory"], "max_vmhwm_kb": None}) == "INCONCLUSIVE"
    assert verdict(memory={**good_report()["memory"], "span_s": 2000.0}) == "INCONCLUSIVE"
    assert verdict(memory={**good_report()["memory"], "span_s": 2890.0}) == "PASS"                # the 10 s slack, inclusive
    assert verdict(memory={}) == "INCONCLUSIVE"


def test_a_machine_that_is_not_shared_cpu_2x_4gb_is_invalid_and_an_unreadable_class_is_inconclusive():
    assert verdict(machine={"id": "m1", "guest": {"cpu_kind": "performance", "cpus": 2, "memory_mb": 4096}}) == "INVALID"
    assert verdict(machine={"id": "m1", "guest": {"cpu_kind": None, "cpus": None, "memory_mb": None}}) == "INCONCLUSIVE"


# --- no line, no verdict ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stages", [None, {}, {"source": "stream", "fields": None}, {"source": "stream", "fields": stage_fields(tokens=None)},
                                    {"source": "stream", "fields": stage_fields(embed_s=None)}])
def test_a_ready_job_without_a_usable_stage_line_is_inconclusive_never_a_pass(stages):
    assert verdict(stages=stages) == "INCONCLUSIVE"


def test_a_ready_job_without_the_harness_ready_s_is_inconclusive():
    assert verdict(job={"state": "ready", "job_id": JOB_ID, "ready_s": None, "end_s": None, "error": None}) == "INCONCLUSIVE"


def test_a_zero_embed_time_cannot_give_a_share_and_is_inconclusive():
    assert verdict(stages={"source": "stream", "fields": stage_fields(embed_s=0.0)}) == "INCONCLUSIVE"


@pytest.mark.parametrize("job", [
    {"state": "timeout", "job_id": JOB_ID, "last_state": "embedding"},                                   # the HARNESS gave up: not the app's timeout
    {"state": "failed", "job_id": JOB_ID, "end_s": 80.0, "error": {"code": "internal_error", "message": "x"}},
    {"state": "failed", "job_id": JOB_ID, "end_s": 80.0, "error": {"code": "too_many_chunks", "message": "x"}},
    {"state": "gone", "job_id": JOB_ID},
    {"state": "not_started", "error": "upload answered 429"},
    {},
])
def test_a_job_that_neither_reached_ready_nor_was_stopped_by_the_ceiling_is_inconclusive(job):
    assert verdict(job=job) == "INCONCLUSIVE"


def test_the_harness_deadline_is_not_the_apps_timeout_even_with_a_timeout_looking_error():
    harness = {"state": "timeout", "job_id": JOB_ID, "last_state": "embedding", "error": {"code": "timeout"}}
    report = timeout_report()
    assert w1d_judge.boot_verdict(report)[0] == "FAIL"
    assert w1d_judge.boot_verdict({**report, "job": harness})[0] == "INCONCLUSIVE"


# --- the job ended timeout: FAIL while valid -------------------------------------------------------------------------------------------------

def test_a_job_stopped_by_the_ceiling_while_valid_is_a_fail():
    verdict_, reasons, criteria = w1d_judge.boot_verdict(timeout_report())
    assert verdict_ == "FAIL" and criteria["job_ended_as_judged"] is False
    assert any("2100" in reason or "2,100" in reason for reason in reasons)


def test_the_share_is_never_computed_from_a_timeout_line_whose_tokens_is_the_plan():
    # tokens=75934 and embed_s=2101.5 would read as a share of 0.357 if it were taken from the line; the line is a timeout line
    report = timeout_report()
    report["stages"]["fields"].update(tokens=75934)
    assert w1d_judge.embed_share(75934, 2101.5) > Fraction("0.15")
    verdict_, _, criteria = w1d_judge.boot_verdict(report)
    assert verdict_ == "FAIL" and criteria["share_within_limit"] is None


def test_a_timeout_where_the_embed_was_not_most_of_the_time_is_invalid_by_the_point_nine_rule():
    report = timeout_report()
    report["job"]["end_s"] = 2335.1                                 # 0.9 x 2335.1 = 2101.59 > 2101.5: embedding was under 0.9 of the job's time
    assert w1d_judge.boot_verdict(report)[0] == "INVALID"
    report["job"]["end_s"] = 2335.0                                 # 0.9 x 2335.0 = 2101.5 exactly: valid
    assert w1d_judge.boot_verdict(report)[0] == "FAIL"


@pytest.mark.parametrize("changes", [{"done": 60}, {"embed_s": 1199.0}, {"embed_s": 2099.999}, {"fresh": None}, {"done": None}, {"embed_s": None}])
def test_a_timeout_that_the_embed_ceiling_did_not_cause_is_inconclusive(changes):
    # done == fresh (the loop finished), embed_s under the 2,100 s ceiling (an image without this change, or another stage), fields missing
    assert w1d_judge.boot_verdict(timeout_report(**changes))[0] == "INCONCLUSIVE"


def test_a_timeout_without_a_stage_line_is_inconclusive_because_a_parse_timeout_writes_none():
    assert w1d_judge.boot_verdict({**timeout_report(), "stages": None})[0] == "INCONCLUSIVE"


def test_a_timeout_with_a_memory_overrun_is_a_fail_for_both_reasons():
    report = timeout_report()
    report["memory"] = {**report["memory"], "max_vmhwm_kb": 2_500_000}
    verdict_, reasons, _ = w1d_judge.boot_verdict(report)
    assert verdict_ == "FAIL" and len(reasons) == 2


# --- the aggregate ---------------------------------------------------------------------------------------------------------------------------

def attempt(n, kind="pass", machine=None, **extra):
    base = {"pass": good_report(), "invalid": good_report(job={"state": "ready", "job_id": JOB_ID, "ready_s": 387.6, "end_s": 387.6, "error": None},
                                                          stages={"source": "stream", "fields": stage_fields(embed_s=370.0)}),
            "fail": timeout_report(), "inconclusive": good_report(stages=None)}[kind]
    report = {**base, "attempt": n, "machine": {"id": machine or f"machine-{n}", "guest": dict(GUEST)}, **extra}
    report["verdict"] = w1d_judge.boot_verdict(report)[0]
    return report


def test_one_valid_passing_boot_passes_the_window():
    result = w1d_judge.aggregate([attempt(1, "pass")])
    assert result["verdict"] == "PASS" and result["problems"] == [] and result["rerun_due"] is False and result["stopped"] is False
    assert result["window"] == "W1d" and result["boots"][0]["share"] == pytest.approx(0.1216, abs=1e-3)
    assert result["boots"][0]["embed_s"] == 1300.0 and result["boots"][0]["tokens"] == 16000


def test_an_invalid_boot_alone_is_incomplete_with_its_one_rerun_due():
    result = w1d_judge.aggregate([attempt(1, "invalid")])
    assert result["verdict"] == "INCOMPLETE" and result["rerun_due"] is True and result["stopped"] is False


def test_two_invalid_boots_end_the_window_incomplete_and_nothing_is_amended():
    result = w1d_judge.aggregate([attempt(1, "invalid"), attempt(2, "invalid")])
    assert result["verdict"] == "INCOMPLETE" and result["stopped"] is True and result["rerun_due"] is False
    assert "nothing" in result["stop_rule"] and "INCOMPLETE" in result["stop_rule"]


def test_an_invalid_boot_followed_by_a_passing_rerun_passes_and_both_attempts_are_reported():
    result = w1d_judge.aggregate([attempt(1, "invalid"), attempt(2, "pass")])
    assert result["verdict"] == "PASS" and [a["attempt"] for a in result["attempts"]] == [1, 2]
    assert result["boots"][0]["attempt"] == 2                      # the last attempt decides


def test_a_fail_in_any_attempt_is_a_fail_the_ceiling_timeout_included():
    assert w1d_judge.aggregate([attempt(1, "fail")])["verdict"] == "FAIL"
    assert w1d_judge.aggregate([attempt(1, "invalid"), attempt(2, "fail")])["verdict"] == "FAIL"
    memory_fail = attempt(1, "pass", memory={**good_report()["memory"], "max_vmhwm_kb": 2_400_001})
    assert w1d_judge.aggregate([memory_fail])["verdict"] == "FAIL"


def test_only_an_invalid_boot_is_rerun_and_only_once_and_on_its_own_machine():
    after_pass = w1d_judge.aggregate([attempt(1, "pass"), attempt(2, "pass")])
    assert after_pass["verdict"] == "INCOMPLETE" and any("only an INVALID boot is re-run" in p for p in after_pass["problems"])
    third = w1d_judge.aggregate([attempt(1, "invalid"), attempt(2, "invalid"), attempt(3, "pass")])
    assert third["verdict"] == "INCOMPLETE" and any("re-run once" in p or "attempts" in p for p in third["problems"])
    same = w1d_judge.aggregate([attempt(1, "invalid", machine="m"), attempt(2, "pass", machine="m")])
    assert same["verdict"] == "INCOMPLETE" and any("own freshly created machine" in p for p in same["problems"])
    assert w1d_judge.aggregate([attempt(2, "pass")])["verdict"] == "INCOMPLETE"                # an attempt 2 with no attempt 1


def test_an_inconclusive_boot_is_not_rerun_by_the_rule_and_the_window_is_inconclusive():
    result = w1d_judge.aggregate([attempt(1, "inconclusive")])
    assert result["verdict"] == "INCONCLUSIVE" and result["rerun_due"] is False


def test_the_aggregate_re_derives_each_verdict_from_the_numbers_in_the_file():
    lying = attempt(1, "invalid")
    lying["verdict"] = "PASS"
    result = w1d_judge.aggregate([lying])
    assert result["verdict"] == "INCOMPLETE" and any("the file says 'PASS'" in reason for reason in result["boots"][0]["reasons"])


def test_files_of_another_window_or_an_empty_list_are_never_judged_as_this_one():
    assert w1d_judge.aggregate([])["verdict"] == "INCOMPLETE"
    result = w1d_judge.aggregate([attempt(1, "pass"), {**attempt(2, "pass"), "window": "W1c"}])
    assert result["verdict"] == "INCOMPLETE" and any("different windows" in p for p in result["problems"])
    assert w1d_judge.aggregate([{**attempt(1, "pass"), "window": "W1c"}])["verdict"] == "INCOMPLETE"


def test_the_stop_rule_text_is_the_specs():
    assert w1d_judge.STOP_RULE == ("an INVALID boot is re-run unchanged ONCE on a fresh machine; two INVALID boots end the window INCOMPLETE "
                                   "and nothing is amended")


def test_the_parser_reads_the_line_the_app_actually_writes(caplog):
    """The app's own ``_log_stages`` output, behind a flyctl-style prefix, parses to the same numbers (the Opus verifier's check that
    the judge and src/semigraph/uploads/jobs.py agree on the format; it cannot show the line reaches flyctl logs, the canary does)."""
    import logging

    from semigraph.uploads import jobs

    stages = {"parse_s": 4.25, "chunk_s": 0.5, "embed_s": 1174.3, "compare_s": 0.0, "index_s": 2.125,
              "chunks": 60, "fresh": 60, "done": 60, "tokens": 14151}
    with caplog.at_level(logging.INFO, logger=jobs.logger.name):
        jobs._log_stages("0123456789abcdef0123456789abcdef", "a1b2c3d4e5f60718", stages)
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("upload stages ws_hash="))
    prefix = "2026-11-06T13:00:00Z app[0123456789abcd] sin [info]2026-11-06 13:00:00,000 INFO semigraph.uploads.jobs "
    [line] = w1d_judge.parse_stage_lines(prefix + message)

    assert line["job_id"] == "a1b2c3d4e5f60718" and line["ws_hash"] and "0123456789abcdef0123" not in line["ws_hash"]
    assert line["fields"] == {**stages}
