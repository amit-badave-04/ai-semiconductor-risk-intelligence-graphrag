"""Build the numeric gold questions (M1b): 24 questions whose expected values are computed from XBRL, never from the graph.

    python scripts/build_numeric_gold.py                      # writes artifacts/gold/numeric_questions.json
    python scripts/build_numeric_gold.py --check              # exits 1 if the committed file differs from a fresh build

Every question names its periods by fiscal-year END DATE (unambiguous across 52/53-week and non-calendar years) and carries the
citable XBRL ids that support it (``metric_ids``), the deterministic expectation (``expect``: see eval/expect.py), a ``window``
tag (``recent`` = inside the last three fiscal years the answer context shows, ``older`` = needs period-aware metric
retrieval) and the sub-type. The selection is an explicit table (PLAN), not a random draw, so the file is reviewable.
"""

import argparse
import glob
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from semigraph.retrieval.ids import xbrl_id

METRICS_GLOB = "data/processed/xbrl/*_key_metrics.parquet"
DEFAULT_OUT = Path("artifacts/gold/numeric_questions.json")
YOY_GAP_DAYS = (350, 380)
RECENT_PERIODS = 3
NAMES = {"NVDA": "Nvidia", "AMD": "AMD", "INTC": "Intel", "MU": "Micron", "AVGO": "Broadcom", "QCOM": "Qualcomm",
         "AAPL": "Apple", "MSFT": "Microsoft", "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta", "TSM": "TSMC",
         "ASML": "ASML"}
LABELS = {"revenue": "total revenue", "net_income": "net income", "rnd": "research and development expense",
          "capex": "capital expenditures"}
CURRENCY_WORDS = {"USD": None, "TWD": ["TWD", "NT$", "New Taiwan dollar"], "EUR": ["EUR", "euro", "€"]}

# (sub-type, ticker, metric(s), period index counted back from the latest fiscal year: 0 = latest)
PLAN = [
    ("yoy", "NVDA", ("revenue",), (0,)), ("yoy", "AMD", ("net_income",), (0,)), ("yoy", "INTC", ("revenue",), (0,)),
    ("yoy", "MU", ("rnd",), (0,)), ("yoy", "AVGO", ("revenue",), (0,)), ("yoy", "QCOM", ("net_income",), (2,)),
    ("yoy", "META", ("rnd",), (0,)), ("yoy", "MSFT", ("revenue",), (1,)), ("yoy", "AMZN", ("net_income",), (0,)),
    ("yoy", "GOOGL", ("revenue",), (3,)),
    ("pair", "NVDA", ("revenue", "net_income"), (0,)), ("pair", "AMD", ("revenue", "rnd"), (1,)),
    ("pair", "META", ("revenue", "net_income"), (0,)), ("pair", "AAPL", ("revenue", "net_income"), (1,)),
    ("pair", "MU", ("revenue", "capex"), (0,)), ("pair", "AVGO", ("revenue", "net_income"), (2,)),
    ("years", "NVDA", ("rnd",), (0, 3)), ("years", "INTC", ("net_income",), (0, 2)), ("years", "QCOM", ("revenue",), (0, 4)),
    ("years", "MSFT", ("net_income",), (1, 3)), ("years", "AMZN", ("revenue",), (0, 3)), ("years", "GOOGL", ("rnd",), (0, 2)),
    ("currency", "TSM", ("revenue",), (0,)), ("currency", "ASML", ("net_income",), (0,)),
]


def load_metrics(pattern: str = METRICS_GLOB) -> pd.DataFrame:
    frames = [pd.read_parquet(f) for f in sorted(glob.glob(pattern))]
    if not frames:
        raise SystemExit(f"no key-metrics parquets match {pattern}")
    return pd.concat(frames, ignore_index=True)


def _series(metrics: pd.DataFrame, ticker: str, metric: str) -> list[dict]:
    rows = metrics[(metrics["ticker"] == ticker) & (metrics["metric"] == metric)].sort_values("end", ascending=False)
    return [{"cik": int(r.cik), "end": str(r.end), "value": float(r.val), "unit": r.unit} for r in rows.itertuples()]


def _long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _at(series: list[dict], index: int, ticker: str, metric: str) -> dict:
    if index >= len(series):
        raise SystemExit(f"{ticker} {metric}: only {len(series)} fiscal years, need index {index}")
    return series[index]


def _direction(values: list[float], *, change: bool = False) -> dict:
    """The number parsers read magnitudes, so the sign is a separate check: a negative figure (a loss) demands a "down"
    word; for a percentage CHANGE the direction is required either way (up for growth, down for a decline)."""
    if change:
        return {"direction": "up" if values[0] > 0 else "down"} if values[0] != 0 else {}
    return {"direction": "down"} if any(v < 0 for v in values) else {}


def _window(indices: tuple[int, ...]) -> str:
    return "recent" if all(i < RECENT_PERIODS for i in indices) else "older"


def _yoy(series, index, ticker, metric):
    cur, prior = _at(series, index, ticker, metric), _at(series, index + 1, ticker, metric)
    gap = (date.fromisoformat(cur["end"]) - date.fromisoformat(prior["end"])).days
    if not YOY_GAP_DAYS[0] <= gap <= YOY_GAP_DAYS[1] or prior["value"] <= 0:
        raise SystemExit(f"{ticker} {metric}: no clean prior year for {cur['end']} (gap {gap} days)")
    pct = round(100 * (cur["value"] - prior["value"]) / prior["value"], 1)
    q = (f"By what percentage did {NAMES[ticker]}'s {LABELS[metric]} change from the fiscal year ended "
         f"{_long_date(prior['end'])} to the fiscal year ended {_long_date(cur['end'])}?")
    return q, {"pct": pct, **_direction([pct], change=True)}, [cur, prior]


def _pair(metrics_df, ticker, names, index):
    points = [_at(_series(metrics_df, ticker, m), index, ticker, m) for m in names]
    if len({p["end"] for p in points}) != 1:
        raise SystemExit(f"{ticker} {names}: metrics disagree on the period end at index {index}")
    q = (f"What were {NAMES[ticker]}'s {LABELS[names[0]]} and {LABELS[names[1]]} for the fiscal year ended "
         f"{_long_date(points[0]['end'])}?")
    values = [p["value"] for p in points]
    return q, {"values": values, **_direction(values)}, points


def _years(series, indices, ticker, metric):
    points = [_at(series, i, ticker, metric) for i in indices]
    q = (f"What were {NAMES[ticker]}'s {LABELS[metric]} for the fiscal years ended {_long_date(points[0]['end'])} "
         f"and {_long_date(points[1]['end'])}?")
    values = [p["value"] for p in points]
    return q, {"values": values, **_direction(values)}, points


def _currency(series, ticker, metric):
    point = _at(series, 0, ticker, metric)
    q = (f"What was {NAMES[ticker]}'s {LABELS[metric]} for the fiscal year ended {_long_date(point['end'])}, "
         "and in which currency is it reported?")
    expect = {"value": point["value"], "any_of": CURRENCY_WORDS[point["unit"]], **_direction([point["value"]])}
    return q, expect, [point]


def build(metrics: pd.DataFrame) -> list[dict]:
    out = []
    for n, (kind, ticker, names, indices) in enumerate(PLAN, 1):
        if kind == "yoy":
            q, expect, points = _yoy(_series(metrics, ticker, names[0]), indices[0], ticker, names[0])
            metric_names = [names[0]] * len(points)
        elif kind == "pair":
            q, expect, points = _pair(metrics, ticker, names, indices[0])
            metric_names = list(names)
        elif kind == "years":
            q, expect, points = _years(_series(metrics, ticker, names[0]), indices, ticker, names[0])
            metric_names = [names[0]] * len(points)
        else:
            q, expect, points = _currency(_series(metrics, ticker, names[0]), ticker, names[0])
            metric_names = [names[0]]
        used = tuple(indices) + ((indices[0] + 1,) if kind == "yoy" else ())
        out.append({"id": f"NG{n:02d}", "type": "numeric", "gold": "xbrl", "subtype": kind, "ticker": ticker, "q": q,
                    "expect": expect, "window": _window(used),
                    "metric_ids": [xbrl_id(p["cik"], m, p["end"]) for p, m in zip(points, metric_names)]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed file differs from a fresh build")
    args = ap.parse_args(argv)
    questions = build(load_metrics())
    text = json.dumps(questions, ensure_ascii=False, indent=1) + "\n"
    if args.check:
        same = args.out.exists() and args.out.read_text(encoding="utf-8") == text
        print("numeric gold is up to date" if same else f"{args.out} differs from a fresh build")
        return 0 if same else 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{len(questions)} questions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
