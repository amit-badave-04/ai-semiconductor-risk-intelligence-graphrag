"""The judgement of window W1b: the pre-registered rule, the evidence readers and the verdicts. Pure functions, no I/O.

Split from ``tools/probe/w1b.py`` (the driver) so that each file stays cohesive; the driver re-exports these names.

PASS for a boot (council 7, decision 1, step 3; pre-registered, not to be changed): max VmHWM of the uvicorn process
<= 2,400,000 kB, no OOM kill and no process restart, machine creation to /healthz 200 <= 180 s (all inclusive). The verdict fails
closed (``boot_verdict``): FAIL when a criterion is violated; INVALID when the measurement was not made under the intended
conditions (the job did not reach ready, no ask finished while it ran, a machine class other than shared-cpu-2x / 4 GB, no swap,
the deployed embedder slots are not the live ones); INCONCLUSIVE when a criterion could not be established (evidence missing or
unreadable, the samples do not cover the workload, a healthy reading taken too late to tell); PASS otherwise. ``aggregate`` passes
only when exactly three boots, each on a different machine, each PASS (it re-derives every verdict from the numbers in the files).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

WINDOW = "W1b"

# The pre-registered rule (verdict.md, Decision 1 step 3). Inclusive limits.
VMHWM_LIMIT_KB = 2_400_000
HEALTHY_LIMIT_S = 180.0
# The live settings the measurement is shaped by: fly.toml sets none of them, so they are the code defaults (tests pin both).
EXPECTED_EMBED_SLOTS = 1
EXPECTED_GUEST = {"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}
COVERAGE_SLACK_S = 10.0
EVIDENCE_PATTERN = re.compile(r"\boom\b|oom[-_ ]kill|out of memory|killed process|sigkill|signal: killed|restarting"
                              r"|exit(?:ed)? with exit code (?!0\b)\d+|exit_code=(?!0\b)\d+", re.IGNORECASE)
EVIDENCE_LINES, EVIDENCE_CHARS = 10, 200


class Scrub:
    """Replaces every secret value it was given, in any text."""

    def __init__(self) -> None:
        self.values: set[str] = set()

    def add(self, *values: str | None) -> None:
        self.values |= {v for v in values if v and len(v) >= 8}

    def __call__(self, text: str) -> str:
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text


# ---- the sampler's lines ----------------------------------------------------------------------------------------------------------

def parse_sampler_lines(text: str) -> tuple[dict, list[dict]]:
    """``(header, samples)`` of the sampler's file as read over ssh (any other line, a banner for instance, is skipped)."""
    header, samples = {}, []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "header" in row:
            header = row["header"] or {}
        elif isinstance(row, dict) and "t" in row:
            samples.append(row)
    return header, samples


def summarize_samples(header: dict, samples: list[dict]) -> dict:
    """The numbers the verdict reads from the sampler's lines. ``vanished_samples`` counts samples without the serving process after
    it was first seen (it died or was replaced)."""
    pids, vanished, seen = [], 0, False
    for row in samples:
        pid = row.get("uvicorn_pid")
        if pid:
            seen = True
            if not pids or pids[-1] != pid:
                pids.append(pid)
        elif seen:
            vanished += 1

    def column(key: str) -> list[int]:
        return [row[key] for row in samples if isinstance(row.get(key), int)]

    swap_used = [row["swap_total_kb"] - row["swap_free_kb"] for row in samples
                 if isinstance(row.get("swap_total_kb"), int) and isinstance(row.get("swap_free_kb"), int)]
    oom = column("oom_kill")
    times = [row["t"] for row in samples if isinstance(row.get("t"), int | float)]
    return {"samples": len(samples), "span_s": round(max(times) - min(times), 1) if times else 0.0, "pids": pids,
            "vanished_samples": vanished, "max_vmhwm_kb": max(column("vmhwm_kb"), default=None),
            "max_vmrss_kb": max(column("vmrss_kb"), default=None),
            "min_mem_available_kb": min(column("mem_available_kb"), default=None),
            "max_swap_used_kb": max(swap_used, default=None), "swap_total_kb": header.get("swap_total_kb"),
            "mem_total_kb": header.get("mem_total_kb"), "cpus": header.get("cpus"),
            "oom_kill_delta": (oom[-1] - oom[0]) if oom else None}


# ---- OOM and restart evidence -------------------------------------------------------------------------------------------------------

def _clip(scrub: Scrub, text: str) -> str:
    return scrub(" ".join(text.split()))[:EVIDENCE_CHARS]


def analyze_events(payload: Any) -> dict | None:
    """Restart and OOM evidence in a machine's event list (``machine status --json``, or a ``machine list`` entry); None when the
    payload holds no event list. An ``exit`` or ``restart`` event, a second ``start`` event or an ``oom_killed`` flag counts."""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return None
    kinds = Counter(str(e.get("type", "")) for e in events if isinstance(e, dict))
    oom = any(isinstance((e.get("request") or {}).get("exit_event"), dict) and e["request"]["exit_event"].get("oom_killed") is True
              for e in events if isinstance(e, dict))
    evidence = [f"machine event '{kind}' x{count}" for kind, count in kinds.items() if kind in ("exit", "restart")]
    if kinds.get("start", 0) >= 2:
        evidence.append(f"machine event 'start' x{kinds['start']} (a restart)")
    if oom:
        evidence.append("machine exit event: oom_killed")
    return {"events": len(events), "evidence": evidence}


def analyze_logs(text: str, machine_id: str | None, scrub: Scrub) -> list[str]:
    """Log lines that show an OOM kill or a non-zero exit, of the measured machine only (a line of another machine is dropped)."""
    lines = [ln for ln in text.splitlines() if EVIDENCE_PATTERN.search(ln) and (not machine_id or machine_id in ln or "[" not in ln)]
    return [_clip(scrub, ln) for ln in lines[:EVIDENCE_LINES]]


def judge_oom_or_restart(summary: dict | None, machine: dict | None, scrub: Scrub) -> dict:
    """``detected`` True on any positive evidence, None when the sources were not enough to say (the sampler AND at least one of the
    machine events and the logs must have been read), False when enough sources were read and none shows anything."""
    evidence, unreadable = [], []
    if summary is None or not summary["samples"]:
        unreadable.append("sampler")
    else:
        if len(summary["pids"]) > 1:
            evidence.append(f"the serving process was replaced: {len(summary['pids'])} different pids")
        if summary["vanished_samples"]:
            evidence.append(f"the serving process was missing in {summary['vanished_samples']} samples after it started")
        if summary["oom_kill_delta"]:
            evidence.append(f"/proc/vmstat oom_kill rose by {summary['oom_kill_delta']}")
    if machine is None or (machine["events"] is None and machine["logs"] is None):
        unreadable.append("machine events and logs")
    else:
        evidence += (machine["events"] or {}).get("evidence", []) + [f"log: {line}" for line in machine["logs"] or []]
    detected = True if evidence else (None if unreadable else False)
    return {"detected": detected, "evidence": evidence[:EVIDENCE_LINES], "unreadable_sources": unreadable}


# ---- the verdict --------------------------------------------------------------------------------------------------------------------

def _vmhwm_ok(mem: dict) -> bool | None:
    peak = mem.get("max_vmhwm_kb")
    return None if peak is None else peak <= VMHWM_LIMIT_KB


def _healthy_ok(health: dict) -> bool | None:
    seconds = health.get("boot_to_healthy_s")
    if seconds is None:
        return False                                      # never answered 200 within the wait: it is slower than the limit
    if seconds <= HEALTHY_LIMIT_S:
        return True
    return None if health.get("healthy_on_first_poll") else False      # first poll already healthy: the true time is unknown


def boot_verdict(report: dict) -> tuple[str, list[str], dict]:
    """``(verdict, reasons, criteria)`` of one boot from the numbers in its report alone (``aggregate`` re-runs it)."""
    mem, health = report.get("memory") or {}, report.get("healthz") or {}
    oom = (report.get("oom_or_restart") or {}).get("detected")
    criteria = {"vmhwm_within_limit": _vmhwm_ok(mem), "no_oom_or_restart": None if oom is None else not oom,
                "healthy_within_limit": _healthy_ok(health)}
    reasons = [f"criterion {name} violated" for name, ok in criteria.items() if ok is False]
    if reasons:
        return "FAIL", reasons, criteria
    guest = (report.get("machine") or {}).get("guest") or {}
    job, asks, settings = report.get("job") or {}, report.get("asks") or {}, report.get("settings_evidence") or {}
    invalid = []
    if all(guest.get(k) is not None for k in EXPECTED_GUEST) and guest != EXPECTED_GUEST:
        invalid.append("the machine is not shared-cpu-2x / 4 GB")
    if job.get("state") != "ready":
        invalid.append(f"the upload job did not reach ready (state {job.get('state')!r})")
    if asks.get("done_overlapping_job", 0) < 1:
        invalid.append("no mock-LLM ask finished while the upload job ran")
    if mem.get("swap_total_kb") == 0:
        invalid.append("the machine has no swap (the live one has 1 GiB)")
    if settings.get("embed_slots") not in (None, EXPECTED_EMBED_SLOTS):
        invalid.append("the deployed embedder slots are not the live ones")
    if invalid:
        return "INVALID", invalid, criteria
    unknown = [f"criterion {name} could not be established" for name, ok in criteria.items() if ok is None]
    if any(guest.get(k) is None for k in EXPECTED_GUEST):
        unknown.append("the machine class could not be read")
    if mem.get("swap_total_kb") is None:
        unknown.append("the swap size could not be read")
    if mem.get("samples", 0) and mem.get("span_s", 0) < report.get("workload_s", 0) - COVERAGE_SLACK_S:
        unknown.append("the samples do not cover the workload")
    return ("INCONCLUSIVE", unknown, criteria) if unknown else ("PASS", [], criteria)


def aggregate(reports: Sequence[dict]) -> dict:
    """The W1b verdict of three boot reports: PASS only if there are exactly three boots on three different machines and every
    one, re-judged from its numbers, passes. FAIL if any boot fails; INCOMPLETE if the three are not there; else the weakest
    of INVALID and INCONCLUSIVE."""
    rows = []
    for report in reports:
        verdict, reasons, _ = boot_verdict(report)
        if verdict != report.get("verdict"):
            reasons = [*reasons, f"the file says {report.get('verdict')!r}, its numbers say {verdict!r}"]
        mem = report.get("memory") or {}
        rows.append({"boot": report.get("boot"), "machine_id": (report.get("machine") or {}).get("id"), "verdict": verdict,
                     "reasons": reasons, "max_vmhwm_kb": mem.get("max_vmhwm_kb"), "min_mem_available_kb": mem.get("min_mem_available_kb"),
                     "max_swap_used_kb": mem.get("max_swap_used_kb"), "boot_to_healthy_s": (report.get("healthz") or {}).get("boot_to_healthy_s"),
                     "job_ready_s": (report.get("job") or {}).get("ready_s")})
    machines, boots = [r["machine_id"] for r in rows], [r["boot"] for r in rows]
    problems = []
    if len(rows) != 3 or len(set(boots)) != 3:
        problems.append(f"need three boots with different numbers, got {boots}")
    if len(set(machines)) != len(machines) or None in machines:
        problems.append("the boots must be on three different machines")
    verdicts = {r["verdict"] for r in rows}
    if "FAIL" in verdicts:
        overall = "FAIL"
    elif problems:
        overall = "INCOMPLETE"
    elif verdicts == {"PASS"}:
        overall = "PASS"
    else:
        overall = "INVALID" if "INVALID" in verdicts else "INCONCLUSIVE"
    peaks = [r["max_vmhwm_kb"] for r in rows if r["max_vmhwm_kb"] is not None]
    avail = [r["min_mem_available_kb"] for r in rows if r["min_mem_available_kb"] is not None]
    swap = [r["max_swap_used_kb"] for r in rows if r["max_swap_used_kb"] is not None]
    return {"window": WINDOW, "verdict": overall, "problems": problems, "boots": rows,
            "rule": {"max_vmhwm_kb": VMHWM_LIMIT_KB, "boot_to_healthy_s": HEALTHY_LIMIT_S, "no_oom_or_restart": True},
            "max_vmhwm_kb": {"values": peaks, "max": max(peaks, default=None), "min": min(peaks, default=None),
                             "spread": (max(peaks) - min(peaks)) if peaks else None,
                             "margin_to_limit_kb": (VMHWM_LIMIT_KB - max(peaks)) if peaks else None},
            "min_mem_available_kb": min(avail, default=None), "max_swap_used_kb": max(swap, default=None),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
