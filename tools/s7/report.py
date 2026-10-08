"""``s7.json`` from the raw files of a replay run (stdlib only): the pre-registered S7 verdict, computed, never typed.

A run directory holds one sub-directory per phase (``baseline``, ``soak``, ``L5``, ``control``, ``L10``, ``L20``), each written by
``replay.py``: ``level.json``, ``samples.jsonl``, ``ledger_rows.json``, ``server.jsonl``, ``driver.jsonl``. This module reads them
and, for each gated level (L5, L10, L20), judges the pre-registered limits (``limits.py``; docs/v2/M5_DECISIONS.md section 3) on
the last ``judged_s`` seconds of the level:

* ``pre_stream_p95`` / ``pre_stream_p99``: the state operations an ask makes before its stream starts;
* ``writes_p95``: every write the state makes;
* ``retrieval_vs_baseline``: the retrieval p95 against 1.5 x its own p95 in the ``baseline`` phase (0.5 live asks/s);
* ``error_rate``: operations that failed, were denied or found no state slot, over all operations of the window;
* ``lost_rows``: the ledger after the drain holds exactly the rows the asks made, none still reserved;
* ``neo4j_restart`` / ``neo4j_oom``: from the probe stream and, when ``--evidence`` is given, the database log and machine events.

Verdict of a level: ``VOID`` if the driver could not deliver the load (its CPU, the rate it offered, how late it started asks:
the database was not the bottleneck, so no gate is judged), else ``FAIL`` if any criterion failed, else ``INCOMPLETE`` if any
could not be judged (missing evidence is never a pass), else ``PASS``. The baseline, the soak and the control are reported, not
judged (``INFORMATION``). M5a requires level 5; levels 10 and 20 chart capacity.

    python -m tools.s7.report RUN_DIR --out s7.json [--evidence SNAPSHOT_DIR]
"""

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from tools.s7 import evidence, limits, record

OK = "ok"
INFORMATION = "INFORMATION"
GATED = tuple(f"L{n}" for n in limits.LEVELS)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest rank: the ``ceil(q * n)``-th smallest. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(round(q * len(ordered), 9)))
    return ordered[min(rank, len(ordered)) - 1]


def _group_of(op: str) -> list[str]:
    return [name for name, ops in limits.GROUPS.items() if op in ops]


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class _Window:
    """The judged part of one level's samples."""

    def __init__(self, meta: dict, samples_path: Path):
        judged = meta.get("judged_s")
        self.start = float(meta["duration_s"]) - float(judged) if judged else 0.0
        self.by_group: dict[str, list[float]] = {name: [] for name in limits.GROUPS}
        self.by_op: dict[str, list[float]] = {}
        self.total = self.errors = 0
        self.error_statuses: Counter = Counter()
        self.statuses: set[str] = set()
        for t, op, total_ms, _exec_ms, status in record.iter_samples(samples_path):
            self.statuses.add(status)
            if t < self.start:
                continue
            self.total += 1
            if status != OK:
                self.errors += 1
                self.error_statuses[f"{op}:{status}"] += 1
                continue
            self.by_op.setdefault(op, []).append(total_ms)
            for group in _group_of(op):
                self.by_group[group].append(total_ms)

    def group(self, name: str) -> dict:
        values = self.by_group[name]
        return {"n": len(values), "p95_ms": percentile(values, 0.95), "p99_ms": percentile(values, 0.99)}

    def ops(self) -> dict:
        return {op: {"n": len(v), "p95_ms": percentile(v, 0.95), "p99_ms": percentile(v, 0.99)}
                for op, v in sorted(self.by_op.items())}


def _criterion(cid: str, observed: float | None, limit: float | None, detail: str, *, passed: bool | None = None) -> dict:
    if passed is None and observed is not None and limit is not None:
        passed = observed <= limit
    return {"id": cid, "passed": passed, "observed": observed, "limit": limit, "detail": detail}


def _lost_rows(meta: dict, rows: dict | None) -> dict:
    if rows is None:
        return {"id": "lost_rows", "passed": None, "observed": None, "limit": 0, "detail": "ledger_rows.json is missing"}
    expected = meta.get("expected", {})
    problems = []
    reserved = int(rows.get("reserved_rows", 0))
    if reserved:
        problems.append(f"{reserved} row(s) still reserved after the drain")
    for name in ("paid_rows", "cached_rows"):
        want, got = expected.get(name), rows.get(name)
        if want is None or got is None:
            problems.append(f"{name} unknown")
        elif got != want:
            problems.append(f"{name}: the asks made {want}, the ledger holds {got}")
    return {"id": "lost_rows", "passed": not problems, "observed": len(problems), "limit": 0,
            "detail": "; ".join(problems) or "every ask's row is there and settled"}


def _database(meta: dict, window: _Window, level_dir: Path, evidence_dir: Path | None) -> list[dict]:
    start, end = float(meta["wall_start"]), float(meta["wall_end"])
    ev = evidence.load(level_dir, evidence_dir, tuple(sorted(window.statuses)))
    out = []
    for cid, verdict in (("neo4j_restart", evidence.restart(ev, start, end)),
                         ("neo4j_oom", evidence.out_of_memory(ev, start, end))):
        detail = ("; ".join(verdict.found) if verdict.found
                  else f"nothing found in {', '.join(verdict.sources)}" if verdict.sources else "no evidence to judge")
        if verdict.notes:
            detail += f" ({'; '.join(verdict.notes)})"
        out.append({"id": cid, "passed": verdict.passed, "observed": len(verdict.found), "limit": 0, "detail": detail,
                    "sources": list(verdict.sources)})
    return out


def _void_reasons(meta: dict) -> list[str]:
    reasons = []
    cpu = (meta.get("driver") or {}).get("cpu_p95")
    if cpu is not None and cpu > limits.DRIVER_CPU_MAX:
        reasons.append(f"driver_cpu: the driver used {cpu:.0%} of its core at p95 (limit {limits.DRIVER_CPU_MAX:.0%})")
    asked = float(meta["live_rate"]) * float(meta["duration_s"])
    started = (meta.get("asks") or {}).get("live_started", 0)
    if asked > 0 and started / asked < limits.OFFERED_RATE_FLOOR:
        reasons.append(f"offered_rate: {started} live asks started of {asked:.0f} asked "
                       f"({started / asked:.0%}, floor {limits.OFFERED_RATE_FLOOR:.0%})")
    lag = (meta.get("lag") or {}).get("p99_s")
    if lag is not None and lag > limits.ARRIVAL_LAG_P99_MAX_S:
        reasons.append(f"arrival_lag: asks started {lag:.2f} s late at p99 (limit {limits.ARRIVAL_LAG_P99_MAX_S:g} s)")
    if meta.get("driver_errors"):
        reasons.append(f"driver_errors: {meta['driver_errors']} job(s) of the replay itself crashed, so the asks were not all made")
    return reasons


def _verdict(criteria: list[dict], void: list[str]) -> dict:
    if void:
        return {"result": "VOID", "reasons": void}
    failed = [f"{c['id']}: {c['detail']}" for c in criteria if c["passed"] is False]
    if failed:
        return {"result": "FAIL", "reasons": failed}
    unknown = [f"{c['id']}: not judged ({c['detail']})" for c in criteria if c["passed"] is None]
    if unknown:
        return {"result": "INCOMPLETE", "reasons": unknown}
    return {"result": "PASS", "reasons": []}


def _judge(meta: dict, window: _Window, baseline_p95: float | None, level_dir: Path,
           evidence_dir: Path | None) -> list[dict]:
    pre, writes, retrieval = window.group("pre_stream"), window.group("writes"), window.group("retrieval")
    rate = window.errors / window.total if window.total else None
    limit = None if baseline_p95 is None else limits.RETRIEVAL_P95_FACTOR * baseline_p95
    return [
        _criterion("pre_stream_p95", pre["p95_ms"], limits.PRE_STREAM_P95_MS, f"{pre['n']} samples"),
        _criterion("pre_stream_p99", pre["p99_ms"], limits.PRE_STREAM_P99_MS, f"{pre['n']} samples"),
        _criterion("writes_p95", writes["p95_ms"], limits.WRITES_P95_MS, f"{writes['n']} samples"),
        _criterion("retrieval_vs_baseline", retrieval["p95_ms"], limit,
                   f"{retrieval['n']} samples against {limits.RETRIEVAL_P95_FACTOR:g} x the baseline p95 "
                   f"({'missing' if baseline_p95 is None else f'{baseline_p95:.1f} ms'})",
                   passed=None if limit is None or retrieval["p95_ms"] is None else retrieval["p95_ms"] <= limit),
        _criterion("error_rate", rate, limits.ERROR_RATE_MAX,
                   f"{window.errors} of {window.total} operations; {dict(window.error_statuses.most_common(5))}"),
        _lost_rows(meta, _read_json(level_dir / "ledger_rows.json")),
        *_database(meta, window, level_dir, evidence_dir),
    ]


def _evidence_for(phase: str, evidence_dir: Path | None) -> Path | None:
    """``EVIDENCE/<phase>/`` when the operator took one snapshot per level (the advice: after each level), else ``EVIDENCE``."""
    if evidence_dir is None:
        return None
    return evidence_dir / phase if (evidence_dir / phase).is_dir() else evidence_dir


def _level(phase: str, level_dir: Path, baseline_p95: float | None, evidence_dir: Path | None) -> dict | None:
    evidence_dir = _evidence_for(phase, evidence_dir)
    meta = _read_json(level_dir / "level.json")
    if meta is None or not (level_dir / "samples.jsonl").is_file():
        return None
    window = _Window(meta, level_dir / "samples.jsonl")
    out = {"phase": phase, "mix": meta.get("mix"), "live_rate": meta.get("live_rate"), "judged_s": meta.get("judged_s"),
           "asks": meta.get("asks"), "errors": meta.get("errors"), "denied": meta.get("denied"),
           "groups": {name: window.group(name) for name in limits.GROUPS}, "ops": window.ops(),
           "driver": meta.get("driver"), "lag": meta.get("lag"), "statements": meta.get("statements"),
           "table_counts": meta.get("table_counts")}
    if phase in GATED:
        out["criteria"] = _judge(meta, window, baseline_p95, level_dir, evidence_dir)
        out["verdict"] = _verdict(out["criteria"], _void_reasons(meta))
    else:
        out["verdict"] = {"result": INFORMATION, "reasons": []}
    return out


def build(run_dir: Path, evidence_dir: Path | None = None) -> dict:
    run_dir = Path(run_dir)
    phases = sorted(p for p in run_dir.iterdir() if p.is_dir() and (p / "level.json").is_file())
    baseline_dir = run_dir / "baseline"
    base = _level("baseline", baseline_dir, None, evidence_dir) if (baseline_dir / "level.json").is_file() else None
    baseline_p95 = None if base is None else base["groups"]["retrieval"]["p95_ms"]
    levels = {}
    for path in phases:
        level = base if path.name == "baseline" and base is not None else _level(path.name, path, baseline_p95, evidence_dir)
        if level is not None:
            levels[path.name] = level
    required = f"L{limits.REQUIRED_LEVEL}"
    verdict = levels[required]["verdict"] if required in levels else {"result": "NOT_RUN", "reasons": [f"{required} has no data"]}
    return {"spike": "S7", "run": run_dir.name,
            "limits": {"pre_stream_p95_ms": limits.PRE_STREAM_P95_MS, "pre_stream_p99_ms": limits.PRE_STREAM_P99_MS,
                       "writes_p95_ms": limits.WRITES_P95_MS, "retrieval_p95_factor": limits.RETRIEVAL_P95_FACTOR,
                       "error_rate_max": limits.ERROR_RATE_MAX, "judged_window_s": limits.JUDGED_WINDOW_S},
            "baseline": {"phase": "baseline", "retrieval_p95_ms": baseline_p95,
                         "retrieval_n": None if base is None else base["groups"]["retrieval"]["n"]},
            "levels": levels, "m5a": {"level": limits.REQUIRED_LEVEL, **verdict}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.s7.report", description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None,
                        help="`staging.py snapshot` output (logs, machine events): EVIDENCE/<phase>/ per level if it exists, else EVIDENCE/")
    args = parser.parse_args(argv)
    doc = build(args.run_dir, args.evidence)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    for phase, level in doc["levels"].items():
        print(f"{phase}: {level['verdict']['result']}" + "".join(f"\n  {r}" for r in level["verdict"]["reasons"]))
    print(f"M5a (level {limits.REQUIRED_LEVEL}): {doc['m5a']['result']}")
    return 0 if doc["m5a"]["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
