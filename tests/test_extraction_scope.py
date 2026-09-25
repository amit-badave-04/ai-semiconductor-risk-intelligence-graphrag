"""extraction_scope / build_extraction_plan tests — synthetic chunk parquets in
a tmp data lake; no network, no LLM.

The scope governs NEW extraction spend only (the graph loader takes every
already-extracted chunk), so what matters here is which filings are picked as
"latest" — by filing_date, never by accession string.
"""

import json

import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.extraction import extractor
from semigraph.extraction.extractor import (
    build_extraction_plan,
    chunk_parquet_path,
    extraction_scope,
)

SCOPE_COLUMNS = extractor._SCOPE_COLUMNS


def make_settings(tmp_path) -> Settings:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    settings.chunks_dir.mkdir(parents=True)
    return settings


def filing_rows(ticker, accession, form, filing_date, sections=("I.1", "I.1A", "II.7")):
    """Two chunks per section for one synthetic filing."""
    return [
        {
            "chunk_id": f"{accession}:{sid}:{k:04d}", "ticker": ticker, "form": form,
            "filing_date": filing_date, "accession_no": accession,
            "section_id": sid, "section_title": f"title {sid}", "sub_heading": None,
            "text": f"{accession} {sid} chunk {k}", "n_tokens": 100,
        }
        for sid in sections
        for k in range(2)
    ]


def write_chunks(settings, ticker, rows) -> None:
    pd.DataFrame(rows).to_parquet(chunk_parquet_path(settings, ticker), index=False)


def accessions(scope: pd.DataFrame) -> set[str]:
    return set(scope["accession_no"])


# ------------------------------------------------------ latest quarterly

class TestLatestQuarterly:
    def test_picks_latest_10q_by_filing_date_not_accession_string(self, tmp_path):
        """MSFT-style: the filing agent changed its accession prefix, so the
        NEWER 10-Q has the lexicographically SMALLER accession number."""
        settings = make_settings(tmp_path)
        older_but_larger = "0001564590-24-000500"   # filed 2024-04-25
        newer_but_smaller = "0000950170-25-061046"  # filed 2025-04-30
        assert older_but_larger > newer_but_smaller  # string order disagrees with date order
        write_chunks(settings, "MSFT",
                     filing_rows("MSFT", older_but_larger, "10-Q", "2024-04-25", ("I.2", "II.1A"))
                     + filing_rows("MSFT", newer_but_smaller, "10-Q", "2025-04-30", ("I.2", "II.1A")))

        scope = extraction_scope(settings, "MSFT")

        assert accessions(scope) == {newer_but_smaller}

    def test_same_filing_date_ties_break_on_accession_no(self, tmp_path):
        settings = make_settings(tmp_path)
        lower, higher = "0000000001-25-000001", "0000000001-25-000002"
        write_chunks(settings, "MSFT",
                     filing_rows("MSFT", higher, "10-Q", "2025-04-30", ("I.2",))
                     + filing_rows("MSFT", lower, "10-Q", "2025-04-30", ("I.2",)))
        assert accessions(extraction_scope(settings, "MSFT")) == {higher}

    def test_only_the_latest_quarterly_is_in_scope(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", "Q-A", "10-Q", "2025-05-28", ("I.2",))
                     + filing_rows("NVDA", "Q-B", "10-Q", "2025-08-27", ("I.2",))
                     + filing_rows("NVDA", "Q-C", "10-Q", "2025-11-19", ("I.2",)))
        assert accessions(extraction_scope(settings, "NVDA")) == {"Q-C"}


# --------------------------------------------------------------- annuals

class TestAnnualOrdering:
    def test_annuals_are_ordered_by_filing_date_not_accession(self, tmp_path):
        settings = make_settings(tmp_path)
        # accession strings run opposite to the filing dates
        newest = "0000000001-26-000001"   # filed 2026-02-25
        middle = "0000000002-25-000001"   # filed 2025-02-26
        oldest = "0000000003-24-000001"   # filed 2024-02-21
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", oldest, "10-K", "2024-02-21")
                     + filing_rows("NVDA", middle, "10-K", "2025-02-26")
                     + filing_rows("NVDA", newest, "10-K", "2026-02-25"))

        scope = extraction_scope(settings, "NVDA")

        # HIST_ANNUALS = 1: latest annual in full + ONE prior annual, risk-only
        assert accessions(scope) == {newest, middle}
        assert set(scope[scope["accession_no"] == newest]["section_id"]) == {"I.1", "I.1A", "II.7"}
        assert set(scope[scope["accession_no"] == middle]["section_id"]) == {"I.1A"}

    def test_hist_annuals_setting_widens_the_risk_only_history(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", "K-1", "10-K", "2023-02-24")
                     + filing_rows("NVDA", "K-2", "10-K", "2024-02-21")
                     + filing_rows("NVDA", "K-3", "10-K", "2025-02-26"))
        monkeypatch.setattr(extractor, "HIST_ANNUALS", 2)

        scope = extraction_scope(settings, "NVDA")

        assert accessions(scope) == {"K-1", "K-2", "K-3"}
        for prior in ("K-1", "K-2"):
            assert set(scope[scope["accession_no"] == prior]["section_id"]) == {"I.1A"}

    def test_annual_filing_date_tie_is_deterministic(self, tmp_path):
        """Two annuals sharing a filing date: input row order must not matter."""
        settings = make_settings(tmp_path)
        a, b = "0000000001-25-000001", "0000000001-25-000002"
        rows_ab = filing_rows("NVDA", a, "10-K", "2025-02-26") + filing_rows("NVDA", b, "10-K", "2025-02-26")
        write_chunks(settings, "NVDA", rows_ab)
        scope_ab = extraction_scope(settings, "NVDA")
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", b, "10-K", "2025-02-26") + filing_rows("NVDA", a, "10-K", "2025-02-26"))
        scope_ba = extraction_scope(settings, "NVDA")
        assert set(scope_ab["chunk_id"]) == set(scope_ba["chunk_id"])
        # the higher accession is "latest" (full), the lower is the prior (risk-only)
        assert set(scope_ab[scope_ab["accession_no"] == b]["section_id"]) == {"I.1", "I.1A", "II.7"}


# ----------------------------------------------------------- composition

class TestScopeComposition:
    def test_latest_annual_plus_latest_quarterly_plus_prior_risk_only(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", "K-old", "10-K", "2024-02-21")
                     + filing_rows("NVDA", "K-mid", "10-K", "2025-02-26")
                     + filing_rows("NVDA", "K-new", "10-K", "2026-02-25")
                     + filing_rows("NVDA", "Q-old", "10-Q", "2026-05-20", ("I.2", "II.1A"))
                     + filing_rows("NVDA", "Q-new", "10-Q", "2026-08-26", ("I.2", "II.1A")))

        scope = extraction_scope(settings, "NVDA")

        assert accessions(scope) == {"K-mid", "K-new", "Q-new"}  # K-old, Q-old excluded
        assert scope["chunk_id"].is_unique
        assert set(scope[scope["accession_no"] == "Q-new"]["section_id"]) == {"I.2", "II.1A"}

    def test_foreign_filer_without_quarterly_form(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "TSM",
                     filing_rows("TSM", "F-1", "20-F", "2024-04-16", ("I.3", "I.4", "I.5"))
                     + filing_rows("TSM", "F-2", "20-F", "2025-04-17", ("I.3", "I.4", "I.5")))
        scope = extraction_scope(settings, "TSM")
        assert accessions(scope) == {"F-1", "F-2"}
        assert set(scope[scope["accession_no"] == "F-1"]["section_id"]) == {"I.3"}  # risk only
        assert set(scope[scope["accession_no"] == "F-2"]["section_id"]) == {"I.3", "I.4", "I.5"}

    def test_quarterly_only_filer(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD", filing_rows("AMD", "Q-1", "10-Q", "2026-08-05", ("I.2",)))
        assert accessions(extraction_scope(settings, "AMD")) == {"Q-1"}

    def test_missing_parquet_returns_empty_scope_with_columns(self, tmp_path):
        settings = make_settings(tmp_path)
        scope = extraction_scope(settings, "NVDA")
        assert scope.empty and list(scope.columns) == SCOPE_COLUMNS

    def test_empty_parquet_returns_empty_scope(self, tmp_path):
        settings = make_settings(tmp_path)
        pd.DataFrame(columns=["chunk_id"]).to_parquet(chunk_parquet_path(settings, "NVDA"), index=False)
        assert extraction_scope(settings, "NVDA").empty


# ------------------------------------------------------------------ plan

class TestExtractionPlan:
    def test_todo_excludes_chunks_already_extracted(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "NVDA", filing_rows("NVDA", "K-1", "10-K", "2026-02-25", ("I.1A",)))
        settings.extractions_dir.mkdir(parents=True)
        (settings.extractions_dir / "nvda_extractions.jsonl").write_text(
            json.dumps({"chunk_id": "K-1:I.1A:0000"}) + "\n", encoding="utf-8"
        )

        scopes, todo = build_extraction_plan(settings, ["NVDA"])

        assert len(scopes["NVDA"]) == 2
        assert todo["NVDA"]["chunk_id"].tolist() == ["K-1:I.1A:0001"]

    def test_unknown_ticker_is_rejected(self, tmp_path):
        with pytest.raises(KeyError, match="unknown ticker"):
            build_extraction_plan(make_settings(tmp_path), ["NOPE"])



# ------------------------------------------- filing versions (amendments, rolled quarters)

class TestScopeFollowsFilingVersions:
    """The scope is derived from versions.py: a parsed 10-K/A stands in for its
    original, and a quarterly that a newer annual has rolled forward is no
    longer worth new extraction spend."""

    def test_parsed_amendment_is_the_latest_annual_and_its_original_is_excluded(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD",
                     filing_rows("AMD", "a-25", "10-K", "2025-02-05")
                     + filing_rows("AMD", "a-26", "10-K", "2026-02-04")
                     + filing_rows("AMD", "a-26A", "10-K/A", "2026-02-04"))

        scope = extraction_scope(settings, "AMD")

        assert accessions(scope) == {"a-26A", "a-25"}   # the corrected original is not extracted
        assert set(scope[scope["accession_no"] == "a-26A"]["section_id"]) == {"I.1", "I.1A", "II.7"}
        assert set(scope[scope["accession_no"] == "a-25"]["section_id"]) == {"I.1A"}

    def test_an_amendment_without_chunks_leaves_the_original_in_place(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD",
                     filing_rows("AMD", "a-25", "10-K", "2025-02-05")
                     + filing_rows("AMD", "a-26", "10-K", "2026-02-04"))   # no 10-K/A rows: unparsed
        assert accessions(extraction_scope(settings, "AMD")) == {"a-26", "a-25"}

    def test_a_quarterly_older_than_the_latest_annual_is_not_in_scope(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "MSFT",
                     filing_rows("MSFT", "k-25", "10-K", "2025-07-30")
                     + filing_rows("MSFT", "q-apr", "10-Q", "2026-04-29", ("I.2", "II.1A"))
                     + filing_rows("MSFT", "k-26", "10-K", "2026-07-29"))
        assert accessions(extraction_scope(settings, "MSFT")) == {"k-26", "k-25"}

    def test_a_quarterly_filed_after_the_latest_annual_is_in_scope(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "NVDA",
                     filing_rows("NVDA", "k-26", "10-K", "2026-02-25")
                     + filing_rows("NVDA", "q-may", "10-Q", "2026-05-20", ("I.2", "II.1A"))
                     + filing_rows("NVDA", "q-aug", "10-Q", "2026-08-26", ("I.2", "II.1A")))
        assert accessions(extraction_scope(settings, "NVDA")) == {"k-26", "q-aug"}

    def test_filing_dates_stored_as_datetimes_are_handled(self, tmp_path):
        settings = make_settings(tmp_path)
        rows = (filing_rows("NVDA", "k-25", "10-K", "2025-02-26")
                + filing_rows("NVDA", "k-26", "10-K", "2026-02-25"))
        for r in rows:
            r["filing_date"] = pd.Timestamp(r["filing_date"])
        write_chunks(settings, "NVDA", rows)
        assert accessions(extraction_scope(settings, "NVDA")) == {"k-26", "k-25"}


class TestPartialAmendmentOverlay:
    """AMD 2026-02-04: the 10-K/A restates Item 7 (MD&A) ONLY. The corrected MD&A
    comes from the amendment; Business and Risk Factors stay with the original."""

    def test_latest_annual_is_original_business_and_risks_plus_amended_mdna(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD",
                     filing_rows("AMD", "a-25", "10-K", "2025-02-05")
                     + filing_rows("AMD", "a-26", "10-K", "2026-02-04")
                     + filing_rows("AMD", "a-26A", "10-K/A", "2026-02-04", sections=("II.7",)))

        scope = extraction_scope(settings, "AMD")

        by_filing = {acc: set(g["section_id"]) for acc, g in scope.groupby("accession_no")}
        assert by_filing["a-26"] == {"I.1", "I.1A"}          # the original's uncorrected MD&A is NOT extracted
        assert by_filing["a-26A"] == {"II.7"}                # the corrected MD&A is
        assert by_filing["a-25"] == {"I.1A"}                 # prior period: risk-only history

    def test_a_full_amendment_still_replaces_the_original_completely(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD",
                     filing_rows("AMD", "a-26", "10-K", "2026-02-04")
                     + filing_rows("AMD", "a-26A", "10-K/A", "2026-02-04"))
        assert accessions(extraction_scope(settings, "AMD")) == {"a-26A"}

    def test_history_keeps_only_the_effective_risk_sections_of_an_amended_prior_period(self, tmp_path):
        settings = make_settings(tmp_path)
        write_chunks(settings, "AMD",
                     filing_rows("AMD", "a-25", "10-K", "2025-02-05")
                     + filing_rows("AMD", "a-25A", "10-K/A", "2025-02-06", sections=("II.7",))
                     + filing_rows("AMD", "a-26", "10-K", "2026-02-04"))
        scope = extraction_scope(settings, "AMD")
        hist = scope[scope["accession_no"].isin({"a-25", "a-25A"})]
        assert set(hist["accession_no"]) == {"a-25"} and set(hist["section_id"]) == {"I.1A"}
