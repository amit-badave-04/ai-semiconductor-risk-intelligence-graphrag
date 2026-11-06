"""The judgement of window W1d, the upload-ceiling confirmation: the pre-registered rules, the stage-line reader and the verdicts.
Pure functions, no I/O.

Window W1d (docs/specs/upload-embed-timeout.md, Plan 2 "Staging") is ONE fresh shared-cpu-2x / 4 GB boot of the fp32 image with the
change, a burn that uses up the burst balance, ONE max-size upload and no asks. It asks one question: does a cap-size job on a
THROTTLED machine finish inside the 2,100 s embed ceiling? A boot only answers it if the machine really was slow, so:

* **share** = tokens / (85.8 x 1.18 x embed_s), taken from the ``upload stages`` line of a job that reached ``ready`` ONLY (on a
  ``timeout`` line ``tokens=`` is the plan, not what was embedded, so a share from it would overstate the machine's speed);
* **INVALID** (not a pass) when the share is above 0.15 or ``embed_s`` is under 0.9 x the harness's ``ready_s``; exactly 0.15 and
  exactly 0.9 x are valid. The arithmetic is exact (fractions), so a boundary never depends on a float;
* **PASS**: valid, the job reached ``ready`` (not ``timeout``), and uvicorn VmHWM <= 2,400,000 kB (inclusive), no OOM kill or restart;
* **FAIL**: the job ended with code ``timeout`` from the embed loop while valid, or a memory or OOM criterion is violated;
* **INCONCLUSIVE**: it cannot be judged (no stage line, the harness gave up first, unreadable evidence); never a PASS;
* the window: an INVALID boot is re-run unchanged ONCE on a fresh machine; two INVALID boots end it INCOMPLETE and nothing is amended.

Reuses the readers and the attempt rule of ``tools/probe/w1b_judge.py`` (W1b, W1c).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

from tools.probe.w1b_judge import COVERAGE_SLACK_S, EXPECTED_GUEST, VMHWM_LIMIT_KB, _attempt_problem

WINDOW = "W1d"
MAX_ATTEMPTS = 2                                   # a boot and at most one unchanged re-run of it when it is INVALID
STOP_RULE = ("an INVALID boot is re-run unchanged ONCE on a fresh machine; two INVALID boots end the window INCOMPLETE "
             "and nothing is amended")

# The spec's inputs of the share (docs/specs/upload-embed-timeout.md): the full-core rate of W1's fp32 passages (85.8 tok/s, on
# performance-2x), the conservative class factor (1.18: a fresh shared core is faster than that one, so a valid boot was throttled
# under either factor), the limits of the validity rule and the default ceiling of the image under test (config.py).
FULL_CORE_TOK_S, CLASS_FACTOR = Fraction("85.8"), Fraction("1.18")
SHARE_LIMIT, EMBED_READY_RATIO = Fraction("0.15"), Fraction("0.9")
EMBED_CEILING_S = 2100

# The fields of the ``upload stages`` line (jobs.py ``_STAGE_FIELDS``): seconds are decimals, counts are integers.
SECONDS_FIELDS = ("parse_s", "chunk_s", "embed_s", "compare_s", "index_s")
COUNT_FIELDS = ("chunks", "fresh", "done", "tokens")
MARKER = "upload stages ws_hash="
SECONDS_RE, COUNT_RE = re.compile(r"\d+(?:\.\d+)?"), re.compile(r"\d+")


# ---- the stage line --------------------------------------------------------------------------------------------------------------

def _line_fields(words: list[str]) -> dict | None:
    """The numeric fields of one stage line, or None when a field the reader knows has a value that is not a number (a line is
    skipped, never guessed)."""
    fields: dict[str, float | int] = {}
    for word in words:
        name, _, value = word.partition("=")
        if name in SECONDS_FIELDS:
            if not SECONDS_RE.fullmatch(value):
                return None
            fields[name] = float(value)
        elif name in COUNT_FIELDS:
            if not COUNT_RE.fullmatch(value):
                return None
            fields[name] = int(value)
    return fields


def parse_stage_lines(text: str) -> list[dict]:
    """Every ``upload stages`` line in ``text`` (flyctl's own prefix before it is ignored), as ``{"ws_hash", "job_id", "fields"}``.
    The failure variant (``upload stages line failed ...``) and the ``upload embed`` line are not stage lines."""
    lines = []
    for raw in text.splitlines():
        start = raw.find(MARKER)
        if start < 0:
            continue
        words = raw[start + len("upload stages "):].split()
        head = dict(word.partition("=")[::2] for word in words[:2])
        if len(words) < 2 or not words[0].startswith("ws_hash=") or not words[1].startswith("job_id=") or not all(head.values()):
            continue
        fields = _line_fields(words[2:])
        if fields is not None:
            lines.append({"ws_hash": head["ws_hash"], "job_id": head["job_id"], "fields": fields})
    return lines


def stage_line_for(lines: Sequence[dict], job_id: str | None) -> dict | None:
    """The line whose ``job_id`` is exactly ``job_id`` (not a longer id that starts with it), or None."""
    if not job_id:
        return None
    return next((line for line in lines if line["job_id"] == job_id), None)


# ---- the share ---------------------------------------------------------------------------------------------------------------------

def _exact(value: Any) -> Fraction:
    """A number as it is written (``1174.3`` is 11743/10, not the float nearest to it)."""
    return Fraction(str(value))


def embed_share(tokens: Any, embed_s: Any) -> Fraction | None:
    """tokens / (85.8 x 1.18 x embed_s) as an exact fraction; None when ``embed_s`` is not positive."""
    seconds = _exact(embed_s)
    return None if seconds <= 0 else _exact(tokens) / (FULL_CORE_TOK_S * CLASS_FACTOR * seconds)


# ---- the verdict of one boot ---------------------------------------------------------------------------------------------------------

def _judge_ready(job: dict, fields: dict | None, criteria: dict, invalid: list[str], unknown: list[str]) -> None:
    """A job that reached ready: the validity rule, from its stage line."""
    criteria["job_ended_as_judged"] = True
    if not fields or "embed_s" not in fields or "tokens" not in fields:
        unknown.append("no usable `upload stages` line for the job (logs missing, or an image without this change)")
        return
    share = embed_share(fields["tokens"], fields["embed_s"])
    if share is None:
        unknown.append("embed_s is not positive: no share can be computed")
        return
    criteria["share_within_limit"] = share <= SHARE_LIMIT
    if not criteria["share_within_limit"]:
        invalid.append(f"the embed-stage share is {float(share):.3f}, above {float(SHARE_LIMIT)}: the job ran faster than the slow end "
                       "this boot tests")
    ready_s = job.get("ready_s")
    if ready_s is None:
        unknown.append("the harness has no ready_s for the job")
        return
    criteria["embed_dominates_ready"] = _exact(fields["embed_s"]) >= EMBED_READY_RATIO * _exact(ready_s)
    if not criteria["embed_dominates_ready"]:
        invalid.append(f"embed_s is under {float(EMBED_READY_RATIO)} x the harness's ready_s: embedding was not most of the job")


def _judge_timeout(job: dict, fields: dict | None, criteria: dict, invalid: list[str], unknown: list[str]) -> bool:
    """A job the app ended with code ``timeout``. True when it is a valid ceiling stop (the caller makes it a FAIL unless the boot is
    invalid for another reason). The share is not computed: on this line ``tokens=`` is the plan. A ceiling stop needs the embed loop
    to have stopped early (``done`` below ``fresh``) at or after the image's ceiling; a timeout of another stage writes no such line."""
    if not fields or any(name not in fields for name in ("embed_s", "fresh", "done")):
        unknown.append("the job ended timeout but there is no usable `upload stages` line: the timeout may not be the embed ceiling's")
        return False
    if fields["done"] >= fields["fresh"] or _exact(fields["embed_s"]) < EMBED_CEILING_S:
        unknown.append(f"the job ended timeout but the embed loop did not stop at the {EMBED_CEILING_S} s ceiling "
                       "(all chunks done, or embed_s under it: another stage, or an image without this change)")
        return False
    end_s = job.get("end_s")
    if end_s is None:
        unknown.append("the harness has no end time for the job")
        return False
    criteria["embed_dominates_ready"] = _exact(fields["embed_s"]) >= EMBED_READY_RATIO * _exact(end_s)
    if not criteria["embed_dominates_ready"]:
        invalid.append(f"embed_s is under {float(EMBED_READY_RATIO)} x the job's time to the failure: embedding was not most of the job")
        return False
    return True


def boot_verdict(report: dict) -> tuple[str, list[str], dict]:
    """``(verdict, reasons, criteria)`` of one boot from the numbers in its report alone (``aggregate`` re-runs it). Fails closed:
    FAIL, then INVALID, then INCONCLUSIVE, else PASS (which needs a valid job that reached ready)."""
    mem, guest = report.get("memory") or {}, (report.get("machine") or {}).get("guest") or {}
    job, fields = report.get("job") or {}, (report.get("stages") or {}).get("fields") or None
    peak, oom = mem.get("max_vmhwm_kb"), (report.get("oom_or_restart") or {}).get("detected")
    criteria: dict[str, bool | None] = {
        "job_ended_as_judged": None, "share_within_limit": None, "embed_dominates_ready": None,
        "vmhwm_within_limit": None if peak is None else peak <= VMHWM_LIMIT_KB, "no_oom_or_restart": None if oom is None else not oom}
    fail = [f"criterion {name} violated" for name in ("vmhwm_within_limit", "no_oom_or_restart") if criteria[name] is False]
    invalid: list[str] = []
    unknown = [f"criterion {name} could not be established" for name in ("vmhwm_within_limit", "no_oom_or_restart") if criteria[name] is None]
    if all(guest.get(key) is not None for key in EXPECTED_GUEST):
        if guest != EXPECTED_GUEST:
            invalid.append("the machine is not shared-cpu-2x / 4 GB")
    else:
        unknown.append("the machine class could not be read")
    state, error = job.get("state"), job.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    timeout_stop = False
    if state == "ready":
        _judge_ready(job, fields, criteria, invalid, unknown)
    elif state == "failed" and code == "timeout":
        timeout_stop = _judge_timeout(job, fields, criteria, invalid, unknown)
    else:
        unknown.append(f"the upload job neither reached ready nor ended timeout (state {state!r}, code {code!r})")
    if timeout_stop and not invalid:
        criteria["job_ended_as_judged"] = False
        fail.append(f"the job ended timeout while valid: the {EMBED_CEILING_S} s embed ceiling stopped a throttled upload")
    if mem.get("samples", 0) and mem.get("span_s", 0) < report.get("workload_s", 0) - COVERAGE_SLACK_S:
        unknown.append("the samples do not cover the workload")
    for reasons, name in ((fail, "FAIL"), (invalid, "INVALID"), (unknown, "INCONCLUSIVE")):
        if reasons:
            return name, reasons, criteria
    return "PASS", [], criteria


# ---- the window ---------------------------------------------------------------------------------------------------------------------------

def _row(report: dict) -> dict:
    """One boot report as a row of the aggregate, its verdict RE-DERIVED from its numbers (a file that claims another is told so)."""
    verdict, reasons, _ = boot_verdict(report)
    if verdict != report.get("verdict"):
        reasons = [*reasons, f"the file says {report.get('verdict')!r}, its numbers say {verdict!r}"]
    job, fields, mem = report.get("job") or {}, (report.get("stages") or {}).get("fields") or {}, report.get("memory") or {}
    share = embed_share(fields["tokens"], fields["embed_s"]) if job.get("state") == "ready" and "tokens" in fields and "embed_s" in fields else None
    burn = report.get("burn") or {}
    return {"boot": report.get("boot"), "attempt": report.get("attempt", 1), "machine_id": (report.get("machine") or {}).get("id"),
            "verdict": verdict, "reasons": reasons, "job_state": job.get("state"), "job_id": job.get("job_id"),
            "ready_s": job.get("ready_s"), "end_s": job.get("end_s"), "embed_s": fields.get("embed_s"), "tokens": fields.get("tokens"),
            "share": None if share is None else round(float(share), 4), "max_vmhwm_kb": mem.get("max_vmhwm_kb"),
            "burn": {key: burn.get(key) for key in ("stopped_by", "elapsed_s", "onset_s", "sustained_fraction")}}


def aggregate(reports: Sequence[dict]) -> dict:
    """The W1d verdict of its boot reports (attempts included). FAIL if ANY attempt fails. Otherwise INCOMPLETE when a problem keeps
    the window from being judged (no boot file, files of another window, an attempt the stop rule does not allow, a machine used
    twice), when the boot is INVALID with no re-run yet (``rerun_due``) or is still INVALID after it (``stopped``: STOP_RULE).
    Otherwise PASS when the deciding (last) attempt passes, else INCONCLUSIVE (the rule re-runs INVALID boots only)."""
    windows = {report.get("window") or WINDOW for report in reports}
    problems: list[str] = []
    if not reports:
        problems.append("no boot file")
    elif len(windows) > 1:
        problems.append(f"the boot files name different windows ({', '.join(sorted(windows))}): a window is judged by its own boots only")
    elif windows != {WINDOW}:
        problems.append(f"these are boot files of window {', '.join(sorted(windows))}, not {WINDOW}")
    rows = [_row(report) for report in reports]
    if problems:
        return _result(rows, rows, problems, "INCOMPLETE", False, False)
    machines = [row["machine_id"] for row in rows]
    if None in machines or len(set(machines)) != len(machines):
        problems.append("every attempt must be on its own freshly created machine (a machine id is missing or used twice)")
    if {row["boot"] for row in rows} != {1}:
        problems.append(f"{WINDOW} is one boot, number 1, got {sorted({str(row['boot']) for row in rows})}")
    attempts = sorted(rows, key=lambda row: str(row["attempt"]))
    if problem := _attempt_problem(1, attempts):
        problems.append(problem)
    deciding = attempts[-1]
    invalid = deciding["verdict"] == "INVALID"
    rerun_due, stopped = invalid and len(attempts) == 1, invalid and len(attempts) > 1
    if any(row["verdict"] == "FAIL" for row in rows):
        overall = "FAIL"
    elif problems or rerun_due or stopped:
        overall = "INCOMPLETE"
    else:
        overall = "PASS" if deciding["verdict"] == "PASS" else "INCONCLUSIVE"
    return _result(rows, [deciding], problems, overall, rerun_due, stopped)


def _result(rows: list[dict], deciding: list[dict], problems: list[str], overall: str, rerun_due: bool, stopped: bool) -> dict:
    return {"window": WINDOW, "verdict": overall, "problems": problems, "rerun_due": rerun_due, "stopped": stopped, "stop_rule": STOP_RULE,
            "boots": deciding, "attempts": sorted(rows, key=lambda row: (str(row["boot"]), str(row["attempt"]))),
            "rule": {"share_limit": float(SHARE_LIMIT), "embed_over_ready": float(EMBED_READY_RATIO), "full_core_tok_s": float(FULL_CORE_TOK_S),
                     "class_factor": float(CLASS_FACTOR), "embed_ceiling_s": EMBED_CEILING_S, "max_vmhwm_kb": VMHWM_LIMIT_KB,
                     "max_attempts": MAX_ATTEMPTS},
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
