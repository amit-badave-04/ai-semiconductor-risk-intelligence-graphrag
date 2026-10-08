"""probe.json from the raw lines of ``burn.py`` (M5a I5, the fresh-machine throttle probe).

The analysis answers four questions with numbers, not adjectives:

* the BASELINE: the median iterations per second of the first ``baseline_s`` seconds (a fresh machine's burst);
* the ONSET: the first second from which the throughput stayed below ``drop_fraction`` (90 %) of the baseline for ``min_run``
  (5) seconds in a row (a single slow second is noise, and is not a throttle);
* the SUSTAINED rate: the median of the last ``sustained_s`` seconds, as a share of the baseline;
* the REFILL: the pulses of the idle stretch as a share of the baseline, and the first idle second at which one is back to 90 %.

And the steal column of ``/proc/stat`` before and after the onset, so the report can say whether the hypervisor took the core.
With fewer than ``baseline_s + min_run`` seconds it says so and claims nothing (``throttled: null``).

    python -m tools.probe.report RUN_DIR [--out probe.json]
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

BASELINE_S = 10
DROP_FRACTION = 0.9
MIN_RUN = 5
SUSTAINED_S = 300
REFILL_FRACTION = 0.9


def load_samples(path: Path) -> list[dict]:
    """The JSON lines of ``path``; a line that does not parse (the last one of a run that was killed) is skipped."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _onset(rates: list[float], threshold: float, min_run: int) -> int | None:
    run = 0
    for index, rate in enumerate(rates):
        run = run + 1 if rate < threshold else 0
        if run == min_run:
            return index - min_run + 1
    return None


def _steal(burn: list[dict], onset_index: int | None) -> dict:
    steals = [(i, s["steal_frac"]) for i, s in enumerate(burn) if s.get("steal_frac") is not None]
    if not steals:
        return {"available": False, "mean_before_onset": None, "mean_after_onset": None, "max": None}
    cut = len(burn) if onset_index is None else onset_index
    return {"available": True, "mean_before_onset": _mean([v for i, v in steals if i < cut]),
            "mean_after_onset": _mean([v for i, v in steals if i >= cut]), "max": max(v for _, v in steals)}


def _pulses(samples: list[dict], baseline: float) -> tuple[list[dict], int | None]:
    pulses = [{"t_after_burn_s": s["t"], "iter_per_s": s["iter_per_s"], "fraction_of_baseline": s["iter_per_s"] / baseline}
              for s in sorted((s for s in samples if s.get("phase") == "pulse"), key=lambda s: s["t"])]
    refill = next((p["t_after_burn_s"] for p in pulses if p["fraction_of_baseline"] >= REFILL_FRACTION), None)
    return pulses, refill


def _summary(result: dict) -> str:
    if result["throttled"] is None:
        return (f"Insufficient data: {result['burn_seconds']} s of burn, at least {result['needed_s']} s are needed "
                "to set a baseline and judge a throttle.")
    base = f"baseline {result['baseline_iter_s']:,.0f} iterations/s"
    if not result["throttled"]:
        text = (f"No throttle in {result['burn_seconds']} s: the throughput never stayed below {DROP_FRACTION:.0%} of the "
                f"{base}; sustained {result['sustained_fraction']:.0%}.")
    else:
        text = (f"Throttled from second {result['onset_s']}: the throughput stayed below {DROP_FRACTION:.0%} of the {base}; "
                f"it sustained {result['sustained_iter_s']:,.0f} iterations/s, {result['sustained_fraction']:.0%} of the baseline.")
    if result["pulses"]:
        refill = result["refill_to_baseline_s"]
        text += (f" An idle pulse was back to {REFILL_FRACTION:.0%} of the baseline after {refill} s of rest." if refill is not None
                 else f" No idle pulse got back to {REFILL_FRACTION:.0%} of the baseline.")
    return text


def analyze(samples: list[dict], *, baseline_s: int = BASELINE_S, drop_fraction: float = DROP_FRACTION,
            min_run: int = MIN_RUN, sustained_s: int = SUSTAINED_S) -> dict:
    burn = sorted((s for s in samples if s.get("phase", "burn") == "burn"), key=lambda s: s["t"])
    rates = [s["iter_per_s"] for s in burn]
    needed = baseline_s + min_run
    result = {"burn_seconds": len(burn), "needed_s": needed, "baseline_s": baseline_s, "drop_fraction": drop_fraction,
              "min_run": min_run, "baseline_iter_s": None, "throttled": None, "onset_s": None, "sustained_iter_s": None,
              "sustained_fraction": None, "steal": _steal(burn, None), "pulses": [], "refill_to_baseline_s": None}
    if len(burn) < needed:
        return {**result, "summary": _summary(result)}
    baseline = statistics.median(rates[:baseline_s])
    onset_index = _onset(rates, baseline * drop_fraction, min_run)
    tail = rates[onset_index:] if onset_index is not None else rates
    sustained = statistics.median(tail[-sustained_s:])
    pulses, refill = _pulses(samples, baseline)
    result.update(baseline_iter_s=baseline, throttled=onset_index is not None,
                  onset_s=None if onset_index is None else burn[onset_index]["t"], sustained_iter_s=sustained,
                  sustained_fraction=sustained / baseline, steal=_steal(burn, onset_index), pulses=pulses,
                  refill_to_baseline_s=refill)
    return {**result, "summary": _summary(result)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.probe.report", description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="default: RUN_DIR/probe.json")
    args = parser.parse_args(argv)
    raw = args.run_dir / "samples.jsonl"
    samples = load_samples(raw) if raw.is_file() else []
    if not samples:
        print(f"no samples in {raw} (run python -m tools.probe.burn first)", file=sys.stderr)
        return 2
    meta_path = args.run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    per_second = [{"t": s["t"], "iter_per_s": s["iter_per_s"], "steal_frac": s.get("steal_frac")}
                  for s in sorted((s for s in samples if s.get("phase", "burn") == "burn"), key=lambda s: s["t"])]
    doc = {"meta": meta, "analysis": analyze(samples), "per_second": per_second}
    (args.out or args.run_dir / "probe.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(doc["analysis"]["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
