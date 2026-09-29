"""``scripts/freshness_parity.py`` comparison logic (D2, docs/v2/M4_PLAN.md 4.1, G6).

Unit-tests :func:`compare` and :func:`memoizing_fetch` ONLY, with fakes — never against a real graph or the network
(``run`` is the one function that touches either, and the task explicitly says not to exercise it here; the main
session runs G6 against the real local graph). Imported dynamically, like ``scripts/freshness_heartbeat.py``.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_parity():
    spec = importlib.util.spec_from_file_location("freshness_parity", ROOT / "scripts" / "freshness_parity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parity = _load_parity()


def _row(accession_no, ticker="NVDA", form="10-K"):
    return {"accession_no": accession_no, "ticker": ticker, "form": form, "filing_date": "2026-02-20",
           "period_of_report": None}


# ---------------------------------------------------------------------- compare(): pure

def test_compare_agrees_when_both_sides_see_exactly_the_same_accessions():
    monitor = [_row("a1"), _row("a2")]
    lake = [_row("a2"), _row("a1")]
    result = parity.compare(monitor, lake)
    assert result["agree"] is True
    assert result["only_in_monitor"] == [] and result["only_in_lake"] == []
    assert result["monitor_count"] == 2 and result["lake_count"] == 2


def test_compare_reports_rows_only_the_monitor_sees():
    result = parity.compare([_row("a1")], [])
    assert result["agree"] is False
    assert result["only_in_monitor"] == [_row("a1")]
    assert result["only_in_lake"] == []


def test_compare_reports_rows_only_the_lake_sees_with_form_and_ticker_for_diagnosis():
    """The expected, owner-accepted cause (risk 2): an unparsed amendment the lake holds but the graph never
    loaded. The row must carry enough to recognise that at a glance."""
    result = parity.compare([], [_row("a1", ticker="INTC", form="10-K/A")])
    assert result["agree"] is False
    assert result["only_in_lake"] == [_row("a1", ticker="INTC", form="10-K/A")]
    assert result["only_in_lake"][0]["form"] == "10-K/A"


def test_compare_with_both_empty_agrees_trivially():
    result = parity.compare([], [])
    assert result["agree"] is True and result["monitor_count"] == 0 and result["lake_count"] == 0


def test_compare_deduplicates_by_accession_not_by_row_identity():
    # Two rows that share an accession but arrived from different code paths (different dict objects) count once.
    result = parity.compare([_row("a1")], [dict(_row("a1"))])
    assert result["agree"] is True
    assert result["monitor_count"] == 1 and result["lake_count"] == 1


# ---------------------------------------------------------------------- memoizing_fetch(): pure

def test_memoizing_fetch_calls_the_real_fetch_once_per_distinct_url():
    calls = []

    def real_fetch(url):
        calls.append(url)
        return {"url": url}

    fetch = parity.memoizing_fetch(real_fetch)
    assert fetch("https://a") == {"url": "https://a"}
    assert fetch("https://a") == {"url": "https://a"}
    assert fetch("https://b") == {"url": "https://b"}
    assert calls == ["https://a", "https://b"]


def test_memoizing_fetch_shares_its_cache_across_both_comparison_paths():
    """The whole point of the shared fetch: check_once and pending_filings/federal_register_pending must see the
    SAME response for a URL they both happen to request, so a diff reflects logic, never two racing network reads."""
    calls = {"n": 0}

    def real_fetch(url):
        calls["n"] += 1
        return {"seen": calls["n"]}

    fetch = parity.memoizing_fetch(real_fetch)
    first_caller_result = fetch("https://shared")
    second_caller_result = fetch("https://shared")   # a different call site, same url
    assert first_caller_result is second_caller_result
    assert calls["n"] == 1
