"""The S2 verdict, computed from the raw files of one run: PASS, FAIL or VOID (M5_PLAN.md section 6, council 5 / 6 verdicts).

    python scripts/loadtest_report.py RUN_DIR [--profile gate|subrun|smoke] [--phases steady,soak] [--fly-embed-s S] [--out run.json]

The verdict is never typed: it is derived from files. ``RUN_DIR`` holds

    generator/events.*.jsonl   what each Locust process saw (tools/loadtest/records.py); REQUIRED
    generator/meta.*.json      the parameters each process ran the traffic model with
    cpu/*.jsonl                tools/loadtest/cpu_watch.py output; role generator, mock and server (one file per machine)
    ledger.json                from the staging snapshot, reset at the start of THIS run (or scoped by run id). It counts ONLY
                               granted-lease rows (a denied reserve writes no SvcQuery row), by settled outcome; cached rows
                               are counted apart:
                               {"scope": "run", "paid_rows_by_outcome": {"done": n, "error": n, "abandoned": n},
                                "unsettled_rows": n, "cached_rows": n, "cap": n | null}   (cap: the daily paid-count cap)
    mock.json                  {"requests": n}                     (the mock LLM's request counter)
    salt_check.json            tools/loadtest/salt_check.py output  (council 5's offline checks)
    server.json                {"loop_lag_warnings": n, "limiter_at_capacity_s": s}   (optional extras)
    meta.json                  {"run_id", "window": {"quote", "machines", "hours", "derived_usd"}, "fleet", "settings",
                                "fly_embed_s": the Fly-measured per-embed CPU-seconds (S6)}

THE RULE (harness plan section 6; the VOID / FAIL split is council 5's clarification and NEEDS OWNER SIGN-OFF before W4):

* **VOID** (the run says nothing about the server) iff one of the four pre-registered clauses or the one harness rule applies:
    - the generator's offered live rate is below 0.95 x 2.6/s,
    - any generator's CPU exceeded 70 %,
    - an offline salt check failed,
    - the server spent less than 0.8 x the Fly per-embed cost in CPU-seconds per live ask (the asks did not really embed),
    - the mock's CPU exceeded 50 %: a harness rule (not pre-registered). The 50 % comes from the harness plan, not from
      M5_PLAN / M5_DECISIONS / council 5; the rule is kept, and labelled as what it is.
  Every VOID reason is TAGGED with where its clause comes from: ``[pre-registered]`` for the first four, ``[input missing]`` for a
  validity input that is absent (it never falls through to PASS), ``[harness-added]`` for the clauses the harness adds on top
  (the mock's CPU rule above, a generator process restarted, the traffic model run with other parameters than the registered ones,
  the offline checks run on another pool or salt format, a live ask without a salt or answered from the cache, colliding salted
  questions). VOID outranks FAIL, so the harness-added clauses can relabel a FAIL: they NEED THE OWNER'S SIGN-OFF beside the
  VOID / FAIL split, and ``verdict.void_without_a_pre_registered_clause`` says when a VOID rests on none of the four.
* otherwise **FAIL** on any gate: TTFE p95 > 1.5 s, any dropped ask, errors >= 0.5 % (admission 429 / shed included, no
  carve-out), server CPU p95 > 70 % of the machine, ledger rows != generator terminals (or any row left unsettled), or any
  overshoot of a cap. A shortfall the server causes (shedding, queueing, timeouts) is a FAIL, reported with the maximum
  sustained live asks/s. The error rate has no named denominator in the plan: the gate fails if the rate over every request, over
  asks, OR over live asks reaches 0.5 % (a shed ask is one failure among ~10 requests of its iteration, so the all-request rate alone
  would let a server shed ~5 % of live asks): an interpretation, stated for the owner.
* otherwise **PASS**.

Measured over the ``--phases`` (default steady + soak). TTFE counts an admitted live ask with no first event as infinitely slow.
The rest of the M5_PLAN section 6 criteria (40 concurrent streams held, RSS, uploads ``ready``, fault phase, Neo4j p95) are
REPORTED in ``reported_not_gated`` and NOT gated by this script.

``--profile subrun`` judges the 10-minute zero-overshoot sub-run (a cap set below demand, so refusals are expected): the offered-rate
floor and every validity clause still apply, the staged shape is not required, and only the ledger rows and the overshoot gate; the
TTFE / drop / error / server-CPU gates are shown as ``reported``. It needs ``ledger.cap``; without one it is a VOID. In the other profiles an absent cap
means the overshoot gate is not applicable. ``--profile smoke`` (the local smoke run) scales the offered-rate floor by VUs/1000 and
does not require the staging-only inputs.
Exit code: 0 PASS, 1 FAIL, 2 VOID, 3 the run directory is unusable (no generator log): no run.json is written then.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.loadtest import model, salt  # noqa: E402
from tools.loadtest.records import read_records  # noqa: E402

REPORT_VERSION = 1
OK_OUTCOMES = frozenset({"done", "ok"})
REQUEST_KINDS = frozenset({"ask", "read", "upload_step"})
WINDOW_BUCKET_S = 60
CPU_GUARD_S = 2.0                      # seconds ignored on each side of a measured interval when taking a CPU maximum / percentile
SIGN_OFF_NOTE = ("NOTE: the VOID / FAIL split (council 5, confirmed by council 6: a shortfall the SERVER causes is a FAIL "
                 "reported with the maximum sustained live asks/s; only a GENERATOR shortfall is a VOID) is a clarification "
                 "of the pre-registered wording and is PENDING OWNER SIGN-OFF. Until it is signed, the original text also "
                 "voids a run whose paid rate falls short for any reason.")
NOT_GATED_NOTE = ("Reported here, NOT gated by this script: concurrent live streams (>= 40 asked), RSS (<= 70 %), uploads "
                  "reaching ready, the fault phase (no app 5xx, one reconciled event per injected 429), Neo4j p95 (S7).")
EXIT_CODES = {"PASS": 0, "FAIL": 1, "VOID": 2}
EXIT_BAD_INPUT = 3


class InputError(Exception):
    """The run directory cannot be judged at all (no generator log, unreadable file)."""


# =====================================================================================================================
# loading
# =====================================================================================================================

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise InputError(f"{path}: {type(e).__name__}: {e}") from None


def _digest(paths: list[Path]) -> dict[str, str]:
    return {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(paths)}


class Run:
    """The raw files of one run directory, parsed."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        gen = directory / "generator"
        self.event_paths = sorted(gen.glob("events.*.jsonl")) if gen.is_dir() else []
        if not self.event_paths:
            raise InputError(f"{directory}: no generator/events.*.jsonl: nothing to judge")
        self.events = {p.name: read_records(p) for p in self.event_paths}
        if not any(self.events.values()):
            raise InputError(f"{directory}: the generator logs are empty")
        self.proc_meta = [_json(p) for p in sorted(gen.glob("meta.*.json"))]
        self.cpu = {p.stem: read_records(p) for p in sorted((directory / "cpu").glob("*.jsonl"))} if (directory / "cpu").is_dir() else {}
        self.cpu_paths = sorted((directory / "cpu").glob("*.jsonl")) if (directory / "cpu").is_dir() else []
        self.meta = self._optional("meta.json") or {}
        self.ledger, self.mock = self._optional("ledger.json"), self._optional("mock.json")
        self.salt_check, self.server = self._optional("salt_check.json"), self._optional("server.json") or {}
        run_ids = {r["run_id"] for recs in self.events.values() for r in recs if "run_id" in r}
        if len(run_ids) > 1:
            raise InputError(f"{directory}: the generator logs belong to several runs: {sorted(run_ids)}")
        self.run_id = self.meta.get("run_id") or (run_ids.pop() if run_ids else directory.name)

    def _optional(self, name: str):
        path = self.dir / name
        return _json(path) if path.is_file() else None

    def input_files(self) -> dict[str, str]:
        singles = [self.dir / n for n in ("meta.json", "ledger.json", "mock.json", "salt_check.json", "server.json")
                   if (self.dir / n).is_file()]
        return _digest(self.event_paths + self.cpu_paths + singles)


# =====================================================================================================================
# small statistics
# =====================================================================================================================

def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (never interpolates below an observed value, so a tail is not flattered). inf sorts last."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100.0 * len(ordered))) - 1]


def _json_number(x: float | None):
    return None if x is None or math.isinf(x) else round(x, 4)


# =====================================================================================================================
# windows
# =====================================================================================================================

def file_spans(records: list[dict]) -> list[tuple[str, float, float]]:
    """``(phase, t0, t1)`` of each phase span in one generator file, from its ``phase`` markers (own clock)."""
    stamps = [r["ts"] for r in records if "ts" in r]
    last = max(stamps) if stamps else 0.0
    markers = sorted((r["ts"], r["name"]) for r in records if r.get("kind") == "phase")
    if not markers:                                  # no markers (a killed process): the extent of each phase's own records
        by_phase: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if "phase" in r and "ts" in r:
                by_phase[r["phase"]].append(r["ts"])
        return [(name, min(ts), max(ts)) for name, ts in sorted(by_phase.items(), key=lambda kv: min(kv[1]))]
    spans = []
    for i, (ts, name) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else last
        spans.append((name, ts, end))
    return spans


def measured_window(run: Run, measured: tuple[str, ...]) -> dict:
    """Per-file measured durations (their own clocks), the mean window, and the measured epoch intervals (merged across
    files) that select the CPU samples: a spike between steady and soak is NOT in them."""
    durations: dict[str, float] = {}
    intervals: list[tuple[float, float]] = []
    for name, records in run.events.items():
        spans = [s for s in file_spans(records) if s[0] in measured]
        durations[name] = sum(e - b for _, b, e in spans)
        intervals += [(b, e) for _, b, e in spans]
    live = {n: d for n, d in durations.items() if d > 0}
    return {"per_file_s": durations, "seconds": (sum(live.values()) / len(live)) if live else 0.0,
            "intervals": _merge(intervals)}


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for b, e in sorted(intervals):
        if merged and b <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([b, e])
    return [(b, e) for b, e in merged]


def phase_table(run: Run) -> list[dict]:
    spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
    counts: dict[str, Counter] = defaultdict(Counter)
    for records in run.events.values():
        for name, b, e in file_spans(records):
            spans[name].append((b, e))
        for r in records:
            if r.get("kind") == "ask":
                counts[r["phase"]]["asks"] += 1
                counts[r["phase"]]["live_asks"] += int(r["klass"] in model.LIVE_CLASSES)
    return [{"name": n, "t0": min(b for b, _ in s), "t1": max(e for _, e in s), "asks": counts[n]["asks"],
             "live_asks": counts[n]["live_asks"]} for n, s in sorted(spans.items(), key=lambda kv: min(b for b, _ in kv[1]))]


def _in(records, kinds, measured):
    return [r for r in records if r.get("kind") in kinds and r.get("phase") in measured]


# =====================================================================================================================
# metrics
# =====================================================================================================================

def _concurrency(run: Run, measured) -> dict:
    per_file = []
    for records in run.events.values():
        buckets = {(r["phase"], r["t_phase"]): r["live_in_flight"] for r in records
                   if r.get("kind") == "streams" and r.get("phase") in measured}
        if buckets:
            per_file.append(buckets)
    if not per_file:
        return {"concurrent_live_max": None, "concurrent_live_p50": None, "concurrent_live_min": None}
    shared = set.intersection(*(set(b) for b in per_file)) or set().union(*per_file)
    series = [sum(b.get(k, 0) for b in per_file) for k in sorted(shared)]
    return {"concurrent_live_max": max(series), "concurrent_live_p50": percentile(series, 50), "concurrent_live_min": min(series)}


def _sustained_rate(run: Run, measured) -> float | None:
    """The best 60 s completed-live-asks/s of the run, fleet-wide (buckets are seconds since each file's own first
    measured record, so no clock is compared across machines)."""
    buckets: Counter = Counter()
    span = 0
    for records in run.events.values():
        mine = [r for r in records if r.get("kind") == "ask" and r.get("phase") in measured]
        if not mine:
            continue
        origin = min(r["ts"] for r in mine)
        for r in mine:
            if r["klass"] in model.LIVE_CLASSES and r["outcome"] == "done":
                buckets[int(r["ts"] - origin)] += 1
        span = max(span, int(max(r["ts"] for r in mine) - origin) + 1)
    if not span:
        return None
    window = min(WINDOW_BUCKET_S, span)
    counts = [buckets.get(i, 0) for i in range(span)]
    running = sum(counts[:window])
    best = running
    for i in range(window, span):
        running += counts[i] - counts[i - window]
        best = max(best, running)
    return best / window


def _ttfe(asks: list[dict], klasses) -> list[float]:
    return [r["ttfe_s"] if r.get("ttfe_s") is not None else math.inf
            for r in asks if r["klass"] in klasses and r.get("status") == 200]


def _error_breakdown(requests: list[dict]) -> tuple[int, dict, dict]:
    failed = [r for r in requests if r.get("outcome") not in OK_OUTCOMES]
    by_status: Counter = Counter()
    by_outcome: Counter = Counter()
    for r in failed:
        status = r.get("status")
        by_status[f"{status}:{r['outcome']}" if status == 200 else str(status if status is not None else r["outcome"])] += 1
        by_outcome[r["outcome"]] += 1
    return len(failed), dict(sorted(by_status.items())), dict(sorted(by_outcome.items()))


def _first_minutes(run: Run, measured, minutes: int = 10) -> dict:
    """The cold-start look: the first ``minutes`` of the measured window of each file (its own clock)."""
    horizon, live, ttfe, requests, failed = minutes * 60, 0, [], 0, 0
    seconds = 0.0
    for name, records in run.events.items():
        mine = _in(records, REQUEST_KINDS, measured)
        if not mine:
            continue
        origin = min(r["ts"] for r in mine)
        early = [r for r in mine if r["ts"] - origin < horizon]
        seconds = max(seconds, min(horizon, max(r["ts"] for r in mine) - origin))
        asks = [r for r in early if r["kind"] == "ask"]
        live += sum(r["klass"] in model.LIVE_CLASSES for r in asks)
        ttfe += _ttfe(asks, model.LIVE_CLASSES)
        requests += len(early)
        failed += sum(r.get("outcome") not in OK_OUTCOMES for r in early)
    return {"minutes": minutes, "live_ttfe_p95": _json_number(percentile(ttfe, 95)),
            "error_rate": round(failed / requests, 5) if requests else None,
            "offered_live_rate": round(live / seconds, 4) if seconds else None}


def _rate(requests: list[dict]) -> float | None:
    """The share of these requests whose outcome is not a success."""
    return sum(r.get("outcome") not in OK_OUTCOMES for r in requests) / len(requests) if requests else None


def _iteration_stats(events: list[dict], measured) -> dict:
    """How often a VU's iteration took longer than its drawn cycle (the offered rate falls short by that much)."""
    iterations = [r for r in events if r.get("kind") == "iteration" and r.get("phase") in measured]
    return {"iterations": len(iterations), "iterations_overran": sum(bool(r.get("overran")) for r in iterations)}


def compute_metrics(run: Run, measured: tuple[str, ...], window: dict) -> dict:
    events = [r for recs in run.events.values() for r in recs]
    requests = _in(events, REQUEST_KINDS, measured)
    asks = [r for r in requests if r["kind"] == "ask"]
    live = [r for r in asks if r["klass"] in model.LIVE_CLASSES]
    seconds = window["seconds"]
    failed, by_status, by_outcome = _error_breakdown(requests)
    live_ttfe = _ttfe(asks, model.LIVE_CLASSES)
    cached_ttfe = [r["ttfe_s"] for r in asks if r["klass"] == "cached" and r.get("ttfe_s") is not None]
    return {
        "requests": len(requests), "asks": len(asks), "live_asks_sent": len(live), "window_s": round(seconds, 2),
        "live_ttfe_p50": _json_number(percentile(live_ttfe, 50)), "live_ttfe_p95": _json_number(percentile(live_ttfe, 95)),
        "live_ttfe_p99": _json_number(percentile(live_ttfe, 99)),
        "live_ttfe_unreached": sum(math.isinf(v) for v in live_ttfe), "live_ttfe_p95_raw": percentile(live_ttfe, 95),
        "cached_ttfe_p50": _json_number(percentile(cached_ttfe, 50)), "cached_ttfe_p95": _json_number(percentile(cached_ttfe, 95)),
        "cached_ttfe_p99": _json_number(percentile(cached_ttfe, 99)),
        "dropped": sum(r["outcome"] == "dropped" for r in asks), "errors": failed,
        "error_rate": (failed / len(requests)) if requests else None,
        "error_rate_asks": _rate(asks), "error_rate_live_asks": _rate(live), "errors_by_status": by_status,
        "errors_by_outcome": by_outcome, "offered_live_rate": (len(live) / seconds) if seconds else None,
        "max_sustained_live_rate": _sustained_rate(run, measured), **_concurrency(run, measured),
        "cached_class_misses": sum(r["klass"] == "cached" and r.get("cached") is False for r in asks),
        "first10min": _first_minutes(run, measured), **_iteration_stats(events, measured),
    }


# =====================================================================================================================
# validity inputs
# =====================================================================================================================

def _cpu_runs(samples: list[dict], intervals: list[tuple[float, float]], guard: float = 0.0) -> list[list[dict]]:
    """The samples inside each measured interval ``[b + guard, e - guard)``, one list per interval (no increment is taken across
    the gap between two). The guard keeps the seconds around a phase change, where two generators' clocks disagree, out of a
    maximum or a percentile; the CPU-seconds sum uses guard 0 so the window is not shortened."""
    if not intervals:
        return [samples]
    return [run for run in ([s for s in samples if b + guard <= s["t"] < e - guard] for b, e in intervals) if run]


def cpu_summary(run: Run, window: dict) -> dict:
    out: dict[str, dict] = {"generator": {}, "mock": {}, "server": {}}
    for name, samples in run.cpu.items():
        if not samples:
            continue
        role = samples[0].get("role")
        if role not in out:
            continue
        inside = [s for r in _cpu_runs(samples, window["intervals"], CPU_GUARD_S) for s in r]
        if not inside:
            out[role][name] = {"samples": 0}
            continue
        whole = _cpu_runs(samples, window["intervals"])
        increments = [max(0.0, b["cpu_time_s"] - a["cpu_time_s"]) for r in whole for a, b in zip(r, r[1:])]
        out[role][name] = {
            "samples": len(inside), "cpu_pct_gate_max": max(s["cpu_pct_gate"] for s in inside),
            "cpu_pct_system_p95": percentile([s["cpu_pct_system"] for s in inside], 95),
            "cpu_pct_system_max": max(s["cpu_pct_system"] for s in inside), "rss_pct_max": max(s["rss_pct"] for s in inside),
            "cpu_time_s": round(sum(increments), 3)}
    return out


def salt_evidence(run: Run) -> dict:
    """Distinct salted questions == live sends, over the WHOLE log (the answer cache persists across phases)."""
    live = [r for recs in run.events.values() for r in recs if r.get("kind") == "ask" and r["klass"] in model.LIVE_CLASSES]
    unsalted = sum(salt.split_salt(r["question"]) is None for r in live)
    distinct = len({(r["strategy"], salt.normalize_question(r["question"])) for r in live})
    cached = [r for recs in run.events.values() for r in recs if r.get("kind") == "ask" and r["klass"] == "cached"]
    return {"live_sends": len(live), "distinct_salted": distinct, "live_without_salt": unsalted,
            "live_cache_hits": sum(r.get("cached") is True for r in live),
            "cached_sends": len(cached), "cached_with_salt": sum(r.get("salt") is not None for r in cached)}


def terminals(run: Run) -> dict:
    """Generator-side terminals of the admitted live asks (whole log) and the cached hits, to compare with the ledger."""
    live = [r for recs in run.events.values() for r in recs if r.get("kind") == "ask" and r["klass"] in model.LIVE_CLASSES]
    admitted = [r for r in live if r.get("status") == 200 and r.get("cached") is not True]       # a cache hit has no ledger row
    by_outcome = Counter(r["outcome"] for r in admitted)
    return {"live_admitted": len(admitted), "by_outcome": dict(by_outcome),
            "cached_hits": sum(1 for recs in run.events.values() for r in recs
                               if r.get("kind") == "ask" and r["klass"] == "cached" and r.get("cached") is True)}


def preregistered_deviations(run: Run, *, require_shape: bool = True) -> list[str]:
    """What a process was run with that is not the pre-registered model (empty = the model as registered). The 10-minute
    zero-overshoot sub-run is the one registered run that is not the 103-minute staged shape (``require_shape=False``)."""
    expected = {"think_min_s": model.THINK_MIN_S, "think_max_s": model.THINK_MAX_S, "upload_period_s": model.UPLOAD_PERIOD_S,
                "p_evidence": model.P_EVIDENCE, "p_dossier": model.P_DOSSIER_OR_CHANGES}
    problems: list[str] = []
    if not run.proc_meta:
        return ["no generator meta file: the parameters the traffic model ran with are unknown"]
    for meta in run.proc_meta:
        params, tag = meta.get("params", {}), f"worker {meta.get('worker')}"
        problems += [f"{tag}: {k}={params.get(k)!r}, registered {v!r}" for k, v in expected.items() if params.get(k) != v]
        if params.get("mix") != dict(model.ASK_MIX):
            problems.append(f"{tag}: ask mix {params.get('mix')!r}, registered {dict(model.ASK_MIX)!r}")
        if require_shape and meta.get("shape") is not True:
            problems.append(f"{tag}: no staged shape (phases were labelled by hand)")
        if meta.get("vus") != model.VUS:
            problems.append(f"{tag}: {meta.get('vus')!r} VUs, registered {model.VUS}")
        if meta.get("upload_vus") != model.UPLOAD_VUS:
            problems.append(f"{tag}: {meta.get('upload_vus')!r} upload VUs, registered {model.UPLOAD_VUS}")
    return problems


# =====================================================================================================================
# gates and the verdict
# =====================================================================================================================

def _gate(value, limit, passed: bool | None, note: str = "") -> dict:
    return {"value": value, "limit": limit, "status": "not_measured" if passed is None else ("pass" if passed else "fail"),
            "note": note}


def _ledger_gates(run: Run, term: dict, drops: int, *, cap_required: bool = False) -> tuple[dict, dict]:
    """``(rows gate, overshoot gate)``. ``ledger.json`` counts ONLY granted-lease rows (a denied reserve writes none: RESERVE_COUNTED
    creates the row after its WHERE, the in-process backend returns before ``reserve_row``), by settled outcome; cached rows are
    counted separately. ``cap`` is the day's paid-count cap set below demand; the overshoot is how far the paid rows went past it."""
    ledger = run.ledger
    if ledger is None:
        return _gate(None, None, None, "ledger.json missing"), _gate(None, None, None, "ledger.json missing")
    rows = ledger.get("paid_rows_by_outcome") or {}
    total, unsettled = sum(rows.values()), int(ledger.get("unsettled_rows") or 0)
    problems = []
    if total != term["live_admitted"]:
        problems.append(f"{total} ledger rows vs {term['live_admitted']} admitted live asks")
    if unsettled:
        problems.append(f"{unsettled} rows still reserved (lost)")
    if drops == 0:
        for gen_name, row_name in (("done", "done"), ("error_event", "error")):
            if rows.get(row_name, 0) != term["by_outcome"].get(gen_name, 0):
                problems.append(f"ledger {row_name}={rows.get(row_name, 0)} vs generator {gen_name}={term['by_outcome'].get(gen_name, 0)}")
    rows_gate = _gate({"ledger": total, "generator": term["live_admitted"], "unsettled": unsettled}, "equal, 0 unsettled",
                      not problems, "; ".join(problems))
    cap = ledger.get("cap")
    if cap is None:
        if cap_required:
            return rows_gate, _gate(None, 0, None, "ledger.json has no cap: the zero-overshoot sub-run needs one set below demand")
        return rows_gate, _gate(None, 0, True, "not applicable: no cap below demand in this run")
    over = max(0, total - int(cap))
    return rows_gate, _gate(over, 0, over == 0, f"{total} paid rows against a cap of {cap}")


def _error_gate(metrics: dict) -> dict:
    """Errors < 0.5 % "including admission 429 / shed", with no named denominator. The all-request rate is the most lenient (a
    shed ask is one failure among ~10 requests of its iteration), so the gate fails if ANY of the three reaches 0.5 %: over every
    request, over asks, over live asks. This is an interpretation, named for the owner."""
    rates = {"requests": metrics["error_rate"], "asks": metrics["error_rate_asks"], "live_asks": metrics["error_rate_live_asks"]}
    known = {k: v for k, v in rates.items() if v is not None}
    worst = max(known, key=known.get) if known else None
    passed = None if not known else all(v < model.ERROR_RATE_LIMIT for v in known.values())
    note = ("429 / 503 admission refusals count as errors (no carve-out); fails if any denominator reaches the limit: "
            + ", ".join(f"{k} {v:.4%}" for k, v in known.items()))
    return _gate(None if worst is None else round(known[worst], 5), model.ERROR_RATE_LIMIT, passed, note)


def compute_gates(run: Run, metrics: dict, cpu: dict, term: dict, *, profile: str = "gate") -> dict:
    server = list(cpu["server"].values())
    server_p95 = max((s["cpu_pct_system_p95"] for s in server if s.get("samples")), default=None)
    raw_ttfe = metrics["live_ttfe_p95_raw"]
    rows, overshoot = _ledger_gates(run, term, metrics["dropped"], cap_required=profile == "subrun")
    gates = _gates(metrics, raw_ttfe, server_p95, rows, overshoot)
    if profile == "subrun":                          # the cap is set below demand on purpose: refusals are expected, only rows + overshoot decide
        for name in ("ttfe_p95", "drops", "errors", "server_cpu"):
            gates[name] = {**gates[name], "status": "reported", "would_be": gates[name]["status"]}
    return gates


def _gates(metrics: dict, raw_ttfe, server_p95, rows: dict, overshoot: dict) -> dict:
    return {
        "ttfe_p95": _gate(metrics["live_ttfe_p95"], model.TTFE_P95_LIMIT_S, None if raw_ttfe is None else raw_ttfe <= model.TTFE_P95_LIMIT_S,
                          f"{metrics['live_ttfe_unreached']} admitted live asks never reached a first event" if metrics["live_ttfe_unreached"] else ""),
        "drops": _gate(metrics["dropped"], 0, metrics["dropped"] == 0),
        "errors": _error_gate(metrics),
        "server_cpu": _gate(server_p95, model.SERVER_CPU_LIMIT_PCT, None if server_p95 is None else server_p95 <= model.SERVER_CPU_LIMIT_PCT,
                            "p95 of 1 s whole-machine CPU samples"),
        "ledger_rows": rows, "overshoot": overshoot,
    }


PRE, MISSING, HARNESS = "pre-registered", "input missing", "harness-added"


class Voids:
    """The VOID reasons of one report, each tagged with where its clause comes from: the four pre-registered conditions, a missing
    validity input, or a clause the harness added (the mock's CPU rule, council 5's validity evidence and the model-fidelity
    checks). The tags exist so the owner can see which VOIDs rest on wording he signed and which on wording still pending his
    sign-off."""

    def __init__(self, smoke: bool) -> None:
        self.items: list[tuple[str, str]] = []
        self.skipped: list[str] = []
        self.smoke = smoke

    def add(self, source: str, text: str) -> None:
        self.items.append((source, text))

    def missing(self, what: str) -> None:
        if self.smoke:
            self.skipped.append(what)
        else:
            self.add(MISSING, f"missing input: {what}")


def _void_rate(v: Voids, metrics: dict, vus: int) -> float:
    floor = model.void_floor_rate(vus, scale_to_vus=v.smoke)
    offered = metrics["offered_live_rate"]
    if offered is None or offered < floor:
        base = model.TARGET_LIVE_RATE * (vus / model.VUS if v.smoke else 1)
        v.add(PRE, f"offered live rate {0.0 if offered is None else offered:.3f}/s is below {floor:.3f}/s (0.95 x {base:.2f}): "
                   "the generator did not offer the registered load")
    return floor


def _void_machine_cpu(v: Voids, cpu: dict) -> None:
    """The generator's 70 % is a registered clause; the mock's 50 % is a harness rule (``model.HARNESS_RULE_LABEL``)."""
    for role, limit, source, label in (("generator", model.GENERATOR_CPU_VOID_PCT, PRE, ""),
                                       ("mock", model.MOCK_CPU_VOID_PCT, HARNESS, f"{model.HARNESS_RULE_LABEL}: ")):
        files = cpu[role]
        if not files or not any(f.get("samples") for f in files.values()):
            v.missing(f"{role} CPU samples in the measured window (cpu/*.jsonl with role {role})")
        for name, f in files.items():
            if f.get("samples") and f["cpu_pct_gate_max"] > limit:
                v.add(source, f"{label}{role} {name} CPU reached {f['cpu_pct_gate_max']:.0f} % (limit {limit:.0f} %): "
                              "timing is the machine's, not the server's")


def _void_offline_checks(v: Voids, run: Run) -> None:
    check = run.salt_check
    if check is None:
        v.missing("salt_check.json (council 5's offline checks)")
        return
    if check.get("ok") is not True:
        v.add(PRE, "an offline salt check failed (salt_check.json ok != true)")
    if {m.get("pool_sha256") for m in run.proc_meta} not in (set(), {check.get("pool_sha256")}):
        v.add(HARNESS, "the offline checks ran on a different pool than the generator used")
    if {m.get("salt_format") for m in run.proc_meta} not in (set(), {check.get("salt_format")}):
        v.add(HARNESS, "the offline checks ran on a different salt format than the generator used")


def _void_salt_evidence(v: Voids, evidence: dict) -> None:
    if evidence["live_without_salt"]:
        v.add(HARNESS, f"{evidence['live_without_salt']} live asks were sent without a salt: they can hit the answer cache")
    if evidence["live_cache_hits"]:
        v.add(HARNESS, f"{evidence['live_cache_hits']} salted live asks were answered from the cache: the load was not uncached")
    if evidence["distinct_salted"] != evidence["live_sends"]:
        v.add(HARNESS, f"{evidence['live_sends']} live sends but {evidence['distinct_salted']} distinct salted questions: "
                       "cache keys collided")


def _void_restarts(v: Voids, run: Run) -> None:
    logs = Counter(next((r["worker"] for r in recs if "worker" in r), None) for recs in run.events.values())
    restarted = {w: n for w, n in logs.items() if n > 1}
    if restarted:
        listed = ", ".join(f"{w}: {n} logs" for w, n in sorted(restarted.items(), key=lambda kv: str(kv[0])))
        v.add(HARNESS, f"a generator process restarted mid-run (worker digit(s) {listed}): its phase labels and salt counters "
                       "are not trustworthy")


def _void_cpu_per_ask(v: Voids, run: Run, cpu: dict, fly_embed_s: float | None, measured: tuple[str, ...]) -> float | None:
    server = [f for f in cpu["server"].values() if f.get("samples")]
    admitted = sum(1 for recs in run.events.values() for r in recs if r.get("kind") == "ask" and r["klass"] in model.LIVE_CLASSES
                   and r.get("status") == 200 and r["phase"] in measured)
    per_ask = (sum(f["cpu_time_s"] for f in server) / admitted) if server and admitted else None
    if fly_embed_s is None:
        v.missing("fly_embed_s (the Fly-measured per-embed cost, S6; the desktop figure is not a substitute)")
    if not server:
        v.missing("server CPU samples (cpu/*.jsonl with role server) for CPU-seconds per live ask")
    if fly_embed_s is not None and per_ask is not None and per_ask < model.CPU_PER_ASK_VOID_FRACTION * fly_embed_s:
        v.add(PRE, f"{per_ask:.3f} CPU-s per live ask is below 0.8 x the Fly per-embed cost {fly_embed_s:.3f}: the asks did not embed")
    return per_ask


def compute_void(run: Run, metrics: dict, cpu: dict, evidence: dict, fly_embed_s: float | None, profile: str, vus: int,
                 measured: tuple[str, ...]) -> tuple[list[tuple[str, str]], list[str], dict]:
    """``([(source, reason)], skipped_checks, validity_numbers)``."""
    v = Voids(smoke=profile == "smoke")
    floor = _void_rate(v, metrics, vus)
    _void_machine_cpu(v, cpu)
    _void_offline_checks(v, run)
    _void_salt_evidence(v, evidence)
    _void_restarts(v, run)
    per_ask = _void_cpu_per_ask(v, run, cpu, fly_embed_s, measured)
    if profile != "smoke":
        for problem in preregistered_deviations(run, require_shape=profile != "subrun"):
            v.add(HARNESS, f"not the pre-registered traffic model: {problem}")
    return v.items, v.skipped, {"cpu_s_per_live_ask": None if per_ask is None else round(per_ask, 4), "floor_rate": round(floor, 4)}


# =====================================================================================================================
# the report
# =====================================================================================================================

def reported_not_gated(run: Run, metrics: dict, cpu: dict) -> dict:
    events = [r for recs in run.events.values() for r in recs]
    uploads = [r for r in events if r.get("kind") == "upload"]
    fault = [r for r in events if r.get("kind") in REQUEST_KINDS and r.get("phase") == "fault"]
    server = [f for f in cpu["server"].values() if f.get("samples")]
    return {
        "concurrent_live_streams": {"max": metrics["concurrent_live_max"], "p50": metrics["concurrent_live_p50"],
                                    "asked": model.MIN_CONCURRENT_LIVE},
        "rss_pct_max": max((f["rss_pct_max"] for f in server), default=None),
        "uploads": {"total": len(uploads), "ready": sum(r["outcome"] == "ready" for r in uploads),
                    "by_outcome": dict(Counter(r["outcome"] for r in uploads))},
        "fault_phase": {"requests": len(fault), "http_5xx": sum(1 for r in fault if (r.get("status") or 0) >= 500),
                        "error_events": sum(1 for r in fault if r.get("outcome") == "error_event")},
        "neo4j_p95": "not measured here (S7)", "note": NOT_GATED_NOTE,
    }


def compute_report(run_dir: Path | str, *, profile: str = "gate", phases: tuple[str, ...] = model.MEASURED_PHASES,
                   fly_embed_s: float | None = None, vus: int | None = None) -> dict:
    run = Run(Path(run_dir))
    window = measured_window(run, phases)
    metrics = compute_metrics(run, phases, window)
    cpu = cpu_summary(run, window)
    fly = fly_embed_s if fly_embed_s is not None else (run.meta.get("fly_embed_s") or (run.meta.get("validity") or {}).get("fly_embed_s"))
    evidence, term = salt_evidence(run), terminals(run)
    run_vus = vus or max((m.get("vus") or 0 for m in run.proc_meta), default=0) or model.VUS
    voids, skipped, numbers = compute_void(run, metrics, cpu, evidence, fly, profile, run_vus, phases)
    gates = compute_gates(run, metrics, cpu, term, profile=profile)
    failed = [f"{name}: {g['value']!r} against {g['limit']!r} {g['note']}".strip() for name, g in gates.items() if g["status"] == "fail"]
    unmeasured = [name for name, g in gates.items() if g["status"] == "not_measured"]
    if unmeasured and profile != "smoke":
        voids.append((MISSING, f"missing input: the gate(s) {', '.join(unmeasured)} could not be measured"))
    void = [f"[{source}] {text}" for source, text in voids]
    result = "VOID" if void else ("FAIL" if failed else "PASS")
    server_files = [f for f in cpu["server"].values() if f.get("samples")]
    report = {
        "report_version": REPORT_VERSION, "run_id": run.run_id, "profile": profile, "phases_measured": list(phases),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "inputs": run.input_files(),
        "window": run.meta.get("window"), "fleet": run.meta.get("fleet"), "settings": run.meta.get("settings"),
        "phases": phase_table(run), "metrics": {k: v for k, v in metrics.items() if k != "live_ttfe_p95_raw"},
        "validity": {
            "distinct_salted": evidence["distinct_salted"], "live_sends": evidence["live_sends"],
            "live_without_salt": evidence["live_without_salt"], "cached_with_salt": evidence["cached_with_salt"],
            "ledger_rows_by_outcome": (run.ledger or {}).get("paid_rows_by_outcome"),
            "ledger_unsettled_rows": (run.ledger or {}).get("unsettled_rows"),
            "generator_terminals_by_outcome": term["by_outcome"], "generator_live_admitted": term["live_admitted"],
            "mock_requests": (run.mock or {}).get("requests"),
            "generator_cpu_max": {n: f.get("cpu_pct_gate_max") for n, f in cpu["generator"].items()},
            "mock_cpu_max": max((f["cpu_pct_gate_max"] for f in cpu["mock"].values() if f.get("samples")), default=None),
            "offline_salt_checks": None if run.salt_check is None else {
                "ok": run.salt_check.get("ok"), "local_graph_ran": (run.salt_check.get("local_graph") or {}).get("ran")},
            "cpu_s_per_live_ask": numbers["cpu_s_per_live_ask"], "fly_embed_s": fly, "skipped_checks": skipped},
        "server": {"cpu_pct_p95": max((f["cpu_pct_system_p95"] for f in server_files), default=None),
                   "rss_pct": max((f["rss_pct_max"] for f in server_files), default=None),
                   "loop_lag_warnings": run.server.get("loop_lag_warnings"),
                   "limiter_at_capacity_s": run.server.get("limiter_at_capacity_s")},
        "gates": gates, "reported_not_gated": reported_not_gated(run, metrics, cpu),
        "verdict": {"result": result, "void_reasons": void, "fail_reasons": failed,
                    "void_clauses": [{"source": source, "text": text} for source, text in voids],
                    "void_without_a_pre_registered_clause": bool(voids) and all(s != PRE for s, _ in voids),
                    "also_failing_if_valid": failed if void else [],
                    "max_sustained_live_rate": metrics["max_sustained_live_rate"],
                    "void_fail_split": "PENDING OWNER SIGN-OFF", "note": SIGN_OFF_NOTE},
    }
    warnings = _warnings(run, metrics, evidence, term)
    report["verdict"]["warnings"] = warnings
    return report


def _warnings(run: Run, metrics: dict, evidence: dict, term: dict) -> list[str]:
    out = []
    if run.salt_check is not None and (run.salt_check.get("local_graph") or {}).get("ran") is not True:
        out.append("the local-graph top-8 overlap / context-size salt check did not run (pre-registered: mean overlap >= 0.90, "
                   "median context within 5 %)")
    if metrics["cached_class_misses"]:
        out.append(f"{metrics['cached_class_misses']} 'cached' asks were answered live: the examples were not all cached")
    if evidence["cached_with_salt"]:
        out.append(f"{evidence['cached_with_salt']} cached-example asks carried a salt")
    cached_rows = (run.ledger or {}).get("cached_rows")
    if cached_rows is not None and cached_rows != term["cached_hits"]:
        out.append(f"{cached_rows} cached ledger rows vs {term['cached_hits']} cached answers the generator received")
    mock = (run.mock or {}).get("requests")
    if mock is not None and term["live_admitted"] and mock < 0.9 * term["live_admitted"]:
        out.append(f"the mock saw {mock} requests for {term['live_admitted']} admitted live asks: some asks never reached it")
    return out


# =====================================================================================================================
# output
# =====================================================================================================================

def render(report: dict) -> str:
    v, m, val, g = report["verdict"], report["metrics"], report["validity"], report["gates"]
    skipped = report["validity"].get("skipped_checks") or []
    lines = [f"run {report['run_id']}  profile={report['profile']}  measured={','.join(report['phases_measured'])}  "
             f"window={m['window_s']} s", f"VERDICT: {v['result']}"]
    lines += [f"  VOID: {r}" for r in v["void_reasons"]] + [f"  FAIL: {r}" for r in v["fail_reasons"]]
    if v["void_without_a_pre_registered_clause"]:
        lines.append("  (this VOID rests on no pre-registered clause: only on missing inputs or clauses the harness added, "
                     "which the owner has not signed)")
    if v["result"] == "VOID" and v["also_failing_if_valid"]:
        lines.append("  (would also FAIL on: " + "; ".join(v["also_failing_if_valid"]) + ")")
    lines.append(f"offered live rate {_fmt(m['offered_live_rate'])}/s, max sustained completed live rate "
                 f"{_fmt(v['max_sustained_live_rate'])}/s, live TTFE p50/p95/p99 {m['live_ttfe_p50']}/{m['live_ttfe_p95']}/{m['live_ttfe_p99']} s, "
                 f"errors {m['errors']}/{m['requests']}, dropped {m['dropped']}")
    lines.append("gates: " + " | ".join(f"{k} {x['status']}" for k, x in g.items()))
    lines.append(f"CPU: generators {val['generator_cpu_max']}  mock {val['mock_cpu_max']}  server p95 {report['server']['cpu_pct_p95']}  "
                 f"CPU-s per live ask {val['cpu_s_per_live_ask']} (Fly per-embed {val['fly_embed_s']})")
    if skipped:
        lines.append("skipped (smoke profile): " + "; ".join(skipped))
    lines += [f"WARNING: {w}" for w in v["warnings"]]
    lines += [report["reported_not_gated"]["note"], SIGN_OFF_NOTE]
    return "\n".join(lines)


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PASS / FAIL / VOID for one S2 load-test run, from its raw files.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--profile", choices=("gate", "smoke", "subrun"), default="gate",
                        help="gate: the 103-minute run; subrun: the 10-minute zero-overshoot run (needs ledger.cap); smoke: local")
    parser.add_argument("--phases", default=",".join(model.MEASURED_PHASES), help="comma-separated phases to judge")
    parser.add_argument("--fly-embed-s", type=float, default=None, help="the Fly-measured per-embed CPU-seconds (S6)")
    parser.add_argument("--vus", type=int, default=None, help="VUs the run used (default: from the generator meta)")
    parser.add_argument("--out", type=Path, default=None, help="run.json path (default: RUN_DIR/run.json)")
    args = parser.parse_args(argv)
    try:
        report = compute_report(args.run_dir, profile=args.profile, phases=tuple(p for p in args.phases.split(",") if p),
                                fly_embed_s=args.fly_embed_s, vus=args.vus)
    except InputError as e:
        print(f"loadtest_report: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    out = args.out or args.run_dir / "run.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(render(report))
    print(f"wrote {out}")
    return EXIT_CODES[report["verdict"]["result"]]


if __name__ == "__main__":
    sys.exit(main())
