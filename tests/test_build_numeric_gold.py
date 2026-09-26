"""scripts/build_numeric_gold.py: XBRL-derived numeric questions with deterministic expectations."""

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from semigraph.eval.expect import check_expectation

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_numeric_gold.py"
spec = importlib.util.spec_from_file_location("build_numeric_gold", SCRIPT)
bng = importlib.util.module_from_spec(spec)
sys.modules["build_numeric_gold"] = bng
spec.loader.exec_module(bng)

REPO = Path(__file__).resolve().parents[1]


def metrics(rows):
    return pd.DataFrame([{"ticker": t, "cik": 1, "metric": m, "end": e, "val": v, "unit": u, "start": s}
                         for t, m, s, e, v, u in rows])


SERIES = metrics([("NVDA", "revenue", "2024-01-29", "2025-01-26", 130497e6, "USD"),
                  ("NVDA", "revenue", "2025-01-27", "2026-01-25", 215938e6, "USD"),
                  ("NVDA", "net_income", "2025-01-27", "2026-01-25", 120067e6, "USD"),
                  ("INTC", "revenue", "2024-01-01", "2024-12-28", 53101e6, "USD"),
                  ("INTC", "revenue", "2024-12-29", "2025-12-27", 52853e6, "USD"),
                  ("INTC", "net_income", "2025-01-01", "2025-12-27", -267e6, "USD"),
                  ("TSM", "revenue", "2025-01-01", "2025-12-31", 3809054.3e6, "TWD")])


def test_yoy_question_names_both_year_ends_and_expects_the_rounded_change_with_its_direction():
    q, expect, points = bng._yoy(bng._series(SERIES, "NVDA", "revenue"), 0, "NVDA", "revenue")
    assert "January 26, 2025" in q and "January 25, 2026" in q and "Nvidia" in q
    assert expect == {"pct": 65.5, "direction": "up"} and [p["end"] for p in points] == ["2026-01-25", "2025-01-26"]


def test_a_decline_expects_the_down_direction_and_a_loss_demands_a_down_word():
    _, expect, _ = bng._yoy(bng._series(SERIES, "INTC", "revenue"), 0, "INTC", "revenue")
    assert expect == {"pct": -0.5, "direction": "down"}
    _, loss, _ = bng._currency(bng._series(SERIES, "INTC", "net_income"), "INTC", "net_income")
    assert loss["direction"] == "down" and loss["value"] == -267e6
    assert check_expectation({"value": loss["value"], "direction": "down"}, "Intel reported a net loss of $267 million.")


def test_currency_question_requires_the_native_currency_word():
    q, expect, _ = bng._currency(bng._series(SERIES, "TSM", "revenue"), "TSM", "revenue")
    assert "currency" in q and check_expectation(expect, "TSMC's revenue was NT$3,809.1 billion (TWD).")
    assert not check_expectation(expect, "TSMC's revenue was 3,809 billion.")


def test_a_period_gap_that_is_not_one_fiscal_year_is_refused():
    gap = metrics([("X", "revenue", "2020-01-01", "2020-12-31", 1.0, "USD"), ("X", "revenue", "2022-01-01", "2022-12-31", 2.0, "USD")])
    with pytest.raises(SystemExit, match="no clean prior year"):
        bng._yoy(bng._series(gap, "X", "revenue"), 0, "X", "revenue")


def test_the_plan_yields_24_unique_questions_each_with_an_expectation_and_xbrl_ids():
    committed = REPO / "artifacts" / "gold" / "numeric_questions.json"
    if not committed.exists():
        pytest.skip("numeric gold not built")
    questions = json.loads(committed.read_text(encoding="utf-8"))
    assert len(questions) == 24 == len({q["id"] for q in questions}) == len({q["q"] for q in questions})
    assert all(q["expect"] and q["metric_ids"] and all(i.startswith("xbrl:") for i in q["metric_ids"]) for q in questions)
    assert {q["window"] for q in questions} == {"recent", "older"}


def test_the_committed_gold_matches_a_fresh_build_when_the_metrics_lake_is_present():
    if not list((REPO / "data" / "processed" / "xbrl").glob("*_key_metrics.parquet")):
        pytest.skip("no XBRL metrics lake")
    assert bng.main(["--check", "--out", str(REPO / "artifacts" / "gold" / "numeric_questions.json")]) == 0
