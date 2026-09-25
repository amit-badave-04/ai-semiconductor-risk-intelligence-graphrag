"""XBRL ingestion: refresh flags, native-unit curation and the TSM gap.

No network: raw companyfacts JSON is built in-test and downloads go through an
injected ``fetch``; filing-level XBRL comes from an injected loader returning an
edgartools-shaped facts DataFrame. The TSM numbers are the REAL values read from
the FY2025 20-F (accession 0001628280-26-025362) inline XBRL via edgartools on
2026-09-25; the FY2023/FY2024 figures equal what the Company Facts API serves.
"""

import json
import logging
import random
import sys
import types

import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.ingestion import edgar as E
from semigraph.ingestion import xbrl as X

TSM_CIK = 1046179
TSM_FY25_ACCN = "0001628280-26-025362"

# real TSM values (TWD, full-year): (revenue, capex, rnd, net income)
TSM = {
    2023: (2_161_735_800_000, 949_816_800_000, 182_370_200_000, 851_027_700_000),
    2024: (2_894_307_700_000, 956_006_500_000, 204_181_800_000, 1_157_523_900_000),
    2025: (3_809_054_300_000, 1_272_410_500_000, 246_427_200_000, 1_695_124_900_000),
}
TSM_USD_2025 = (121_423_500_000, 40_561_400_000, 7_855_500_000, 54_036_500_000)  # convenience translation


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(X, "SEC_PAUSE_S", 0)
    return Settings(data_dir=tmp_path / "data", sec_user_agent="Test test@example.com", _env_file=None)


# ------------------------------------------------- companyfacts fixtures

def fact(end: str, val: float, accn: str, filed: str, *, start: str | None = None, form: str = "10-K") -> dict:
    return {
        "start": start if start is not None else f"{end[:4]}-01-01",
        "end": end, "val": val, "accn": accn, "fy": int(end[:4]), "fp": "FY", "form": form, "filed": filed,
    }


def companyfacts(*entries: tuple[str, str, str, list[dict]]) -> dict:
    """entries: (taxonomy, concept, unit, facts)."""
    facts: dict = {}
    for taxonomy, concept, unit, rows in entries:
        payload = facts.setdefault(taxonomy, {}).setdefault(concept, {"units": {}})
        payload["units"].setdefault(unit, []).extend(rows)
    return {"facts": facts}


def tsm_facts_json(*, with_fy2025: bool = False, usd_tie_2021: bool = False) -> dict:
    """TSM as the live Company Facts API serves it: ifrs-full, TWD (+ USD convenience)."""
    idx = {"revenue": 0, "capex": 1, "rnd": 2, "net_income": 3}
    concept = {
        "revenue": "Revenue",
        "capex": "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "rnd": "ResearchAndDevelopmentExpense",
        "net_income": "ProfitLoss",
    }
    accn = {2023: "0001193125-24-099840", 2024: "0001193125-25-083423", 2025: TSM_FY25_ACCN}
    filed = {2023: "2024-04-18", 2024: "2025-04-17", 2025: "2026-04-16"}
    years = (2023, 2024, 2025) if with_fy2025 else (2023, 2024)
    entries = []
    for metric, i in idx.items():
        rows = [fact(f"{y}-12-31", TSM[y][i], accn[y], filed[y], form="20-F") for y in years]
        entries.append(("ifrs-full", concept[metric], "TWD", rows))
        if usd_tie_2021:
            entries.append(("ifrs-full", concept[metric], "TWD", [fact("2021-12-31", 1.5e12, "A-21", "2022-04-14", form="20-F")]))
            entries.append(("ifrs-full", concept[metric], "USD", [fact("2021-12-31", 5.7e10, "A-21", "2022-04-14", form="20-F")]))
    return companyfacts(*entries)


def units_of(df: pd.DataFrame) -> dict:
    return {(r.metric, r.end): r.unit for r in df.itertuples()}


# ------------------------------------------- curate_metrics: native units

class TestCurateKeepsNativeUnits:
    def test_us_gaap_keeps_usd(self):
        fj = companyfacts(("us-gaap", "Revenues", "USD", [fact("2025-12-31", 1e9, "N-1", "2026-02-01")]))
        df = X.curate_metrics(fj, "NVDA", 1)
        assert list(df.columns) == X.CURATED_COLUMNS and units_of(df) == {("revenue", "2025-12-31"): "USD"}

    def test_ifrs_full_keeps_twd(self):
        df = X.curate_metrics(tsm_facts_json(), "TSM", TSM_CIK)
        assert set(df["unit"]) == {"TWD"}
        row = df[(df.metric == "revenue") & (df.end == "2024-12-31")].iloc[0]
        assert row.val == TSM[2024][0] and row.unit == "TWD"

    def test_us_gaap_taxonomy_with_eur_keeps_eur(self):
        # ASML's raw companyfacts carries only dei + us-gaap, yet in EUR
        fj = companyfacts(("us-gaap", "NetIncomeLoss", "EUR", [fact("2025-12-31", 9.6e9, "A-25", "2026-02-25", form="20-F")]))
        df = X.curate_metrics(fj, "ASML", 937966)
        assert units_of(df) == {("net_income", "2025-12-31"): "EUR"}

    def test_native_currency_beats_usd_convenience_translation_of_the_same_period(self):
        """Regression: v1 kept TSM FY2021 as 5.7e10 'USD' among TWD rows — same filing, same
        `filed`, both units — because an unstable sort + groupby.first picked one arbitrarily."""
        df = X.curate_metrics(tsm_facts_json(usd_tie_2021=True), "TSM", TSM_CIK)
        fy21 = df[df.end == "2021-12-31"]
        assert set(fy21["unit"]) == {"TWD"} and set(fy21["val"]) == {1.5e12}
        assert set(df["unit"]) == {"TWD"}

    def test_unit_choice_is_independent_of_fact_order(self):
        base = tsm_facts_json(usd_tie_2021=True)
        rng = random.Random(7)
        for _ in range(8):
            shuffled = json.loads(json.dumps(base))
            for taxonomy in shuffled["facts"].values():
                for payload in taxonomy.values():
                    payload["units"] = dict(rng.sample(list(payload["units"].items()), len(payload["units"])))
            df = X.curate_metrics(shuffled, "TSM", TSM_CIK)
            assert set(df[df.end == "2021-12-31"]["unit"]) == {"TWD"}

    def test_a_period_with_only_a_foreign_unit_keeps_that_unit_honestly(self):
        fj = companyfacts(
            ("ifrs-full", "Revenue", "TWD", [fact("2023-12-31", 2.1e12, "A", "2024-04-18", form="20-F"),
                                              fact("2024-12-31", 2.9e12, "B", "2025-04-17", form="20-F")]),
            ("ifrs-full", "Revenue", "USD", [fact("2021-12-31", 5.7e10, "C", "2022-04-14", form="20-F")]),
        )
        assert units_of(X.curate_metrics(fj, "TSM", 1))[("revenue", "2021-12-31")] == "USD"


class TestCurateRules:
    def test_first_disclosure_wins_across_filings(self):
        fj = companyfacts(("us-gaap", "Revenues", "USD", [
            fact("2024-12-31", 100, "LATER-restated", "2026-02-01"),
            fact("2024-12-31", 90, "FIRST", "2025-02-01"),
        ]))
        row = X.curate_metrics(fj, "X", 1).iloc[0]
        assert (row.val, row.accn) == (90, "FIRST")

    def test_only_full_year_annual_periods(self):
        fj = companyfacts(("us-gaap", "Revenues", "USD", [
            fact("2025-03-31", 10, "Q", "2025-05-01", start="2025-01-01", form="10-K"),   # 89 days
            fact("2025-12-31", 40, "Y", "2026-02-01"),
            fact("2025-12-31", 41, "QF", "2026-02-01", form="10-Q"),                      # not an annual form
        ]))
        df = X.curate_metrics(fj, "X", 1)
        assert list(df["end"]) == ["2025-12-31"] and list(df["val"]) == [40]

    def test_concept_tie_is_broken_by_key_concept_order_not_row_order(self):
        rows = [fact("2025-12-31", 7, "A", "2026-02-01")]
        for order in (("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                      ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")):
            fj = companyfacts(*[("us-gaap", c, "USD", list(rows)) for c in order])
            assert X.curate_metrics(fj, "X", 1).iloc[0]["concept"] == "Revenues"

    def test_ifrs_contract_revenue_concept_is_recognised(self):
        # the FY2025 TSM 20-F tags revenue as RevenueFromContractsWithCustomers, not Revenue
        assert "RevenueFromContractsWithCustomers" in X.KEY_CONCEPTS["revenue"]
        fj = companyfacts(("ifrs-full", "RevenueFromContractsWithCustomers", "TWD",
                           [fact("2025-12-31", 3.8e12, "A", "2026-04-16", form="20-F")]))
        assert X.curate_metrics(fj, "TSM", 1).iloc[0]["metric"] == "revenue"

    def test_only_short_periods_yield_an_empty_frame(self):
        fj = companyfacts(("us-gaap", "Revenues", "USD", [fact("2025-03-31", 1, "Q", "2025-05-01", start="2025-01-01")]))
        assert X.curate_metrics(fj, "X", 1).empty

    def test_metric_order_and_end_sorting_are_stable(self):
        df = X.curate_metrics(tsm_facts_json(), "TSM", TSM_CIK)
        assert list(dict.fromkeys(df["metric"])) == ["revenue", "capex", "rnd", "net_income"]
        for _, grp in df.groupby("metric", sort=False):
            assert list(grp["end"]) == sorted(grp["end"])

    def test_empty_input_gives_an_empty_frame_with_the_columns(self):
        df = X.curate_metrics({"facts": {}}, "X", 1)
        assert df.empty and list(df.columns) == X.CURATED_COLUMNS


# ---------------------------------------------- filing-XBRL supplement (TSM)

def filing_facts(*, year: int = 2025, currencies: tuple[str, ...] = ("TWD", "USD")) -> pd.DataFrame:
    """An edgartools ``facts.to_dataframe()``-shaped frame for the FY2025 20-F."""
    concept = {
        "revenue": "ifrs-full:RevenueFromContractsWithCustomers",   # NOT ifrs-full:Revenue
        "capex": "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "rnd": "ifrs-full:ResearchAndDevelopmentExpense",
        "net_income": "ifrs-full:ProfitLoss",
    }
    rows = []
    for i, metric in enumerate(concept):
        for y in (2023, 2024, 2025):
            if "TWD" in currencies:
                rows.append(_frow(concept[metric], TSM[y][i], "TWD", f"{y}-01-01", f"{y}-12-31"))
        if "USD" in currencies:
            rows.append(_frow(concept[metric], TSM_USD_2025[i], "USD", "2025-01-01", "2025-12-31"))
    rows += [  # noise the curation must ignore
        _frow("ifrs-full:ProfitLossBeforeTax", 2_041_654_700_000, "TWD", "2025-01-01", "2025-12-31"),
        _frow("ifrs-full:ProfitLossAttributableToOwnersOfParent", 1_697_604_000_000, "TWD", "2025-01-01", "2025-12-31"),
        _frow("tsm:ResearchAndDevelopmentExpense", 1.0, "TWD", "2025-01-01", "2025-12-31"),
        _frow(concept["revenue"], 5.0, "TWD", "2025-01-01", "2025-12-31", dimensioned=True),
        _frow(concept["revenue"], 9.0, "TWD", "2025-10-01", "2025-12-31"),                    # a quarter
        {"concept": "dei:EntityRegistrantName", "numeric_value": float("nan"), "currency": None,
         "period_type": "duration", "period_start": "2025-01-01", "period_end": "2025-12-31",
         "is_dimensioned": False},
    ]
    return pd.DataFrame(rows)


def _frow(concept: str, val: float, currency: str, start: str, end: str, *, dimensioned: bool = False) -> dict:
    return {"concept": concept, "numeric_value": float(val), "currency": currency, "period_type": "duration",
            "period_start": start, "period_end": end, "is_dimensioned": dimensioned}


def tsm_curated() -> pd.DataFrame:
    return X.curate_metrics(tsm_facts_json(), "TSM", TSM_CIK)


class TestSupplementFromFilingXbrl:
    def test_appends_the_missing_fiscal_year_in_native_currency(self):
        out = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        new = out[out.end == "2025-12-31"]
        assert list(out.columns) == X.CURATED_COLUMNS
        assert {r.metric: r.val for r in new.itertuples()} == {
            "revenue": TSM[2025][0], "capex": TSM[2025][1], "rnd": TSM[2025][2], "net_income": TSM[2025][3]}
        assert set(new["unit"]) == {"TWD"}                       # never the USD convenience rows
        assert set(new["accn"]) == {TSM_FY25_ACCN} and set(new["ticker"]) == {"TSM"} and set(new["cik"]) == {TSM_CIK}
        assert {r.metric: r.concept for r in new.itertuples()} == {
            "revenue": "RevenueFromContractsWithCustomers",
            "capex": "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "rnd": "ResearchAndDevelopmentExpense", "net_income": "ProfitLoss"}
        assert set(new["start"]) == {"2025-01-01"}

    def test_existing_rows_are_untouched_first_disclosure_semantics(self):
        base = tsm_curated()
        out = X.supplement_metrics_from_filing_xbrl(
            base, filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert len(out) == len(base) + 4
        pd.testing.assert_frame_equal(out.iloc[: len(base)].reset_index(drop=True), base.reset_index(drop=True))
        # FY2023/FY2024 comparatives in the new filing are NOT re-attributed to it
        assert TSM_FY25_ACCN not in set(out[out.end < "2025-12-31"]["accn"])

    def test_is_idempotent(self):
        once = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        twice = X.supplement_metrics_from_filing_xbrl(
            once, filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        pd.testing.assert_frame_equal(once, twice)

    def test_does_not_mutate_its_inputs(self):
        base, facts = tsm_curated(), filing_facts()
        base_copy, facts_copy = base.copy(), facts.copy()
        X.supplement_metrics_from_filing_xbrl(base, facts, ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        pd.testing.assert_frame_equal(base, base_copy)
        pd.testing.assert_frame_equal(facts, facts_copy)

    def test_a_disagreeing_overlap_blocks_the_append(self, caplog):
        facts = filing_facts()
        facts.loc[(facts.concept == "ifrs-full:ProfitLoss") & (facts.period_end == "2024-12-31"), "numeric_value"] *= 1.5
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.supplement_metrics_from_filing_xbrl(
                tsm_curated(), facts, ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert len(out) == len(tsm_curated()) and "net_income" in caplog.text and TSM_FY25_ACCN in caplog.text

    def test_no_overlap_to_cross_check_means_no_append(self, caplog):
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.supplement_metrics_from_filing_xbrl(
                pd.DataFrame(columns=X.CURATED_COLUMNS), filing_facts(),
                ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert out.empty and "cross-check" in caplog.text

    def test_a_frame_without_the_edgartools_columns_is_a_warning_not_a_crash(self, caplog):
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.supplement_metrics_from_filing_xbrl(
                tsm_curated(), pd.DataFrame({"unrelated": [1]}), ticker="TSM", cik=TSM_CIK, accession_no="A")
        assert len(out) == len(tsm_curated()) and "no usable key-concept facts" in caplog.text

    def test_every_period_conflicting_yields_no_append(self):
        doubled = filing_facts().assign(numeric_value=lambda d: d.numeric_value * 2)
        out = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), pd.concat([filing_facts(), doubled]),
            ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert len(out) == len(tsm_curated())

    def test_a_metric_the_filing_does_not_tag_is_skipped_not_guessed(self, caplog):
        facts = filing_facts()
        facts = facts[~facts.concept.str.contains("ResearchAndDevelopment") | facts.concept.str.startswith("tsm:")]
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.supplement_metrics_from_filing_xbrl(
                tsm_curated(), facts, ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert set(out[out.end == "2025-12-31"]["metric"]) == {"revenue", "capex", "net_income"}
        assert "rnd" in caplog.text

    def test_conflicting_values_for_one_metric_period_are_not_guessed(self):
        facts = pd.concat([filing_facts(), pd.DataFrame([
            _frow("ifrs-full:ProfitLoss", 1_600_000_000_000, "TWD", "2025-01-01", "2025-12-31")])])
        out = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), facts, ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert "net_income" not in set(out[out.end == "2025-12-31"]["metric"])

    def test_revenue_prefers_the_earlier_key_concept_when_both_are_tagged(self):
        both = pd.concat([filing_facts(), pd.DataFrame([
            _frow("ifrs-full:Revenue", TSM[2025][0], "TWD", "2025-01-01", "2025-12-31")])])
        out = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), both, ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert out[(out.metric == "revenue") & (out.end == "2025-12-31")].iloc[0]["concept"] == "Revenue"

    def test_currency_is_the_one_with_the_most_periods(self):
        out = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        assert "USD" not in set(out["unit"])

    def test_a_filing_with_nothing_new_returns_the_input_unchanged(self):
        base = X.supplement_metrics_from_filing_xbrl(
            tsm_curated(), filing_facts(), ticker="TSM", cik=TSM_CIK, accession_no=TSM_FY25_ACCN)
        old_only = filing_facts()
        old_only = old_only[old_only.period_end != "2025-12-31"]
        out = X.supplement_metrics_from_filing_xbrl(base, old_only, ticker="TSM", cik=TSM_CIK, accession_no="A")
        pd.testing.assert_frame_equal(out, base)


# ------------------------------------------------------------ gap detection

def manifest_rows(*rows: tuple[str, str, str]) -> list[dict]:
    return [{"ticker": "TSM", "cik": TSM_CIK, "form": f, "filing_date": d, "accession_no": a} for f, d, a in rows]


class TestFilingsWithoutCuratedMetrics:
    def test_flags_the_annual_filing_with_no_curated_row(self):
        rows = manifest_rows(("20-F", "2025-04-17", "0001193125-25-083423"), ("20-F", "2026-04-16", TSM_FY25_ACCN))
        gaps = X.filings_without_curated_metrics(rows, tsm_curated())
        assert [g["accession_no"] for g in gaps] == [TSM_FY25_ACCN]

    def test_amendments_and_quarterlies_never_count(self):
        rows = manifest_rows(("10-K/A", "2026-02-04", "AMEND"), ("10-Q", "2026-05-06", "Q1"),
                             ("20-F", "2025-04-17", "0001193125-25-083423"))
        assert X.filings_without_curated_metrics(rows, tsm_curated()) == []

    def test_oldest_gap_first(self):
        rows = manifest_rows(("20-F", "2026-04-16", "B"), ("20-F", "2025-04-17", "A"))
        assert [g["accession_no"] for g in X.filings_without_curated_metrics(rows, tsm_curated())] == ["A", "B"]

    def test_empty_curated_frame_flags_every_annual(self):
        rows = manifest_rows(("20-F", "2025-04-17", "A"))
        assert len(X.filings_without_curated_metrics(rows, pd.DataFrame(columns=X.CURATED_COLUMNS))) == 1


# --------------------------------------------- download / extract (refresh)

class FakeSEC:
    def __init__(self, payload: dict):
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return json.dumps(self.payload).encode("utf-8")


def write_manifest(settings: Settings, rows: list[dict]) -> None:
    E.edgar_dir(settings).mkdir(parents=True, exist_ok=True)
    E.manifest_path(settings).write_text(json.dumps({"TSM": rows}), encoding="utf-8")


class TestDownloadCompanyfacts:
    def test_downloads_once_then_reuses_the_raw_cache(self, settings):
        fake = FakeSEC(tsm_facts_json())
        X.download_companyfacts(settings, TSM_CIK, fetch=fake)
        X.download_companyfacts(settings, TSM_CIK, fetch=fake)
        assert fake.urls == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0001046179.json"]

    def test_refresh_redownloads_and_replaces_the_raw_file(self, settings):
        X.download_companyfacts(settings, TSM_CIK, fetch=FakeSEC(tsm_facts_json()))
        newer = FakeSEC(tsm_facts_json(with_fy2025=True))
        got = X.download_companyfacts(settings, TSM_CIK, refresh=True, fetch=newer)
        assert len(newer.urls) == 1
        raw = X.xbrl_raw_dir(settings) / "CIK0001046179_companyfacts.json"
        assert json.loads(raw.read_text(encoding="utf-8")) == got == tsm_facts_json(with_fy2025=True)

    def test_default_path_sends_the_identity_and_pauses(self, settings, monkeypatch):
        seen: list = []
        sleeps: list[float] = []

        def fake_get(url: str, ua: str) -> bytes:
            seen.append((url, ua))
            return json.dumps(tsm_facts_json()).encode()

        monkeypatch.setattr(X, "_sec_get", fake_get)
        monkeypatch.setattr(X, "SEC_PAUSE_S", 0.15)
        monkeypatch.setattr(X.time, "sleep", sleeps.append)
        X.download_companyfacts(settings, TSM_CIK)
        assert seen == [("https://data.sec.gov/api/xbrl/companyfacts/CIK0001046179.json", "Test test@example.com")]
        assert sleeps == [0.15]

    def test_a_failed_refresh_keeps_the_previous_raw_file(self, settings):
        X.download_companyfacts(settings, TSM_CIK, fetch=FakeSEC(tsm_facts_json()))

        def down(url: str) -> bytes:
            raise OSError("SEC down")

        with pytest.raises(OSError):
            X.download_companyfacts(settings, TSM_CIK, refresh=True, fetch=down)
        raw = X.xbrl_raw_dir(settings) / "CIK0001046179_companyfacts.json"
        assert json.loads(raw.read_text(encoding="utf-8")) == tsm_facts_json()


def no_filing_xbrl(ticker: str, accession_no: str) -> pd.DataFrame:
    raise AssertionError("filing XBRL must not be consulted here")


def filing_loader(ticker: str, accession_no: str) -> pd.DataFrame:
    assert (ticker, accession_no) == ("TSM", TSM_FY25_ACCN)
    return filing_facts()


class TestDefaultFilingLoader:
    """The real loader wraps edgartools: Company(ticker).get_filings(accession_number=...)[0].xbrl()."""

    def install(self, monkeypatch, filings: list) -> list:
        calls: list = []

        class Company:
            def __init__(self, ticker: str):
                calls.append(("company", ticker))

            def get_filings(self, *, accession_number: str):
                calls.append(("get_filings", accession_number))
                return filings

        module = types.ModuleType("edgar")
        module.set_identity = lambda ident: calls.append(("identity", ident))
        module.Company = Company
        monkeypatch.setitem(sys.modules, "edgar", module)
        return calls

    def xbrl_filing(self, xbrl):
        return types.SimpleNamespace(xbrl=lambda: xbrl)

    def test_returns_the_facts_dataframe(self, settings, monkeypatch):
        expected = filing_facts()
        xbrl = types.SimpleNamespace(facts=types.SimpleNamespace(to_dataframe=lambda: expected))
        calls = self.install(monkeypatch, [self.xbrl_filing(xbrl)])
        got = X._edgartools_filing_facts(settings, "TSM", TSM_FY25_ACCN)
        assert got is expected
        assert calls == [("identity", "Test test@example.com"), ("company", "TSM"), ("get_filings", TSM_FY25_ACCN)]

    def test_unknown_accession_is_a_lookup_error(self, settings, monkeypatch):
        self.install(monkeypatch, [])
        with pytest.raises(LookupError, match="not found"):
            X._edgartools_filing_facts(settings, "TSM", "nope")

    def test_filing_without_inline_xbrl_is_a_lookup_error(self, settings, monkeypatch):
        self.install(monkeypatch, [self.xbrl_filing(None)])
        with pytest.raises(LookupError, match="no inline XBRL"):
            X._edgartools_filing_facts(settings, "TSM", TSM_FY25_ACCN)


class TestExtractMetrics:
    ROWS = staticmethod(lambda: manifest_rows(
        ("20-F", "2025-04-17", "0001193125-25-083423"), ("20-F", "2026-04-16", TSM_FY25_ACCN)))

    def test_default_run_reuses_the_parquet_and_touches_nothing(self, settings):
        write_manifest(settings, self.ROWS())
        X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=lambda t, a: filing_facts())
        again = X.extract_metrics(settings, ["TSM"], fetch=lambda u: pytest.fail("network"),
                                  filing_facts_loader=no_filing_xbrl)
        assert again["TSM"]["cached"] is True and again["TSM"]["rows"] > 0

    def test_refresh_redownloads_recurates_and_supplements(self, settings):
        write_manifest(settings, self.ROWS())
        first = FakeSEC(tsm_facts_json())
        X.extract_metrics(settings, ["TSM"], fetch=first, filing_facts_loader=lambda t, a: pd.DataFrame())
        second = FakeSEC(tsm_facts_json())

        out = X.extract_metrics(settings, ["TSM"], refresh=True, fetch=second, filing_facts_loader=filing_loader)

        assert len(second.urls) == 1 and out["TSM"]["cached"] is False
        df = pd.read_parquet(X.xbrl_out_dir(settings) / "TSM_key_metrics.parquet")
        assert set(df["unit"]) == {"TWD"}
        new = df[df.end == "2025-12-31"]
        assert len(new) == 4 and set(new["accn"]) == {TSM_FY25_ACCN}
        assert out["TSM"]["supplemented"] == [TSM_FY25_ACCN] and out["TSM"]["gaps"] == []

    def test_unresolved_gap_logs_a_clear_warning_with_the_accession(self, settings, caplog):
        write_manifest(settings, self.ROWS())
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()),
                                    filing_facts_loader=lambda t, a: pd.DataFrame())
        assert out["TSM"]["gaps"] == [TSM_FY25_ACCN] and out["TSM"]["supplemented"] == []
        assert TSM_FY25_ACCN in caplog.text and "no curated metric period" in caplog.text
        df = pd.read_parquet(X.xbrl_out_dir(settings) / "TSM_key_metrics.parquet")
        assert df["end"].max() == "2024-12-31"           # nothing guessed

    def test_a_crashing_loader_degrades_to_a_warning(self, settings, caplog):
        write_manifest(settings, self.ROWS())

        def boom(ticker: str, accession_no: str) -> pd.DataFrame:
            raise RuntimeError("edgartools exploded")

        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.xbrl"):
            out = X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=boom)
        assert out["TSM"]["gaps"] == [TSM_FY25_ACCN] and "edgartools exploded" in caplog.text

    def test_no_gap_means_the_loader_is_never_called(self, settings):
        write_manifest(settings, manifest_rows(("20-F", "2025-04-17", "0001193125-25-083423")))
        out = X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)
        assert out["TSM"]["gaps"] == [] and out["TSM"]["supplemented"] == []

    def test_cik_falls_back_to_company_tickers_when_not_in_the_manifest(self, settings):
        (E.edgar_dir(settings)).mkdir(parents=True, exist_ok=True)
        (E.edgar_dir(settings) / "company_tickers.json").write_text(
            json.dumps({"0": {"cik_str": TSM_CIK, "ticker": "TSM", "title": "TSMC"}}), encoding="utf-8")
        fake = FakeSEC(tsm_facts_json())
        X.extract_metrics(settings, ["TSM"], fetch=fake, filing_facts_loader=no_filing_xbrl)
        assert fake.urls == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0001046179.json"]

    def test_result_keeps_the_legacy_summary_keys(self, settings):
        write_manifest(settings, manifest_rows(("20-F", "2025-04-17", "0001193125-25-083423")))
        out = X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)
        assert {"rows", "cached", "metrics"} <= set(out["TSM"])
        assert out["TSM"]["metrics"] == ["capex", "net_income", "revenue", "rnd"]


class TestAtomicParquetWrite:
    """A crash mid-write must never leave a truncated parquet that the next run
    treats as a finished cache hit (``extract_metrics`` skips a filer whose file exists)."""

    ROWS = staticmethod(lambda: manifest_rows(("20-F", "2025-04-17", "0001193125-25-083423")))

    def parquet(self, settings: Settings):
        return X.xbrl_out_dir(settings) / "TSM_key_metrics.parquet"

    def test_a_write_that_dies_midway_keeps_the_previous_parquet_and_leaves_no_temp_file(self, settings, monkeypatch):
        write_manifest(settings, self.ROWS())
        X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)
        before = self.parquet(settings).read_bytes()

        def dies_midway(self_df, path, *args, **kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"PAR1-truncated")
            raise OSError("disk full")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", dies_midway)
        with pytest.raises(OSError, match="disk full"):
            X.extract_metrics(settings, ["TSM"], refresh=True, fetch=FakeSEC(tsm_facts_json(with_fy2025=True)),
                              filing_facts_loader=no_filing_xbrl)

        assert self.parquet(settings).read_bytes() == before
        assert [p.name for p in X.xbrl_out_dir(settings).iterdir()] == ["TSM_key_metrics.parquet"]

    def test_a_first_write_that_dies_leaves_no_parquet_so_the_next_run_recomputes(self, settings, monkeypatch):
        write_manifest(settings, self.ROWS())

        def dies_midway(self_df, path, *args, **kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"PAR1-truncated")
            raise OSError("disk full")

        with monkeypatch.context() as broken, pytest.raises(OSError):
            broken.setattr(pd.DataFrame, "to_parquet", dies_midway)
            X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)

        assert not self.parquet(settings).exists()
        out = X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)
        assert out["TSM"]["cached"] is False and out["TSM"]["rows"] > 0

    def test_a_successful_write_leaves_only_the_parquet(self, settings):
        write_manifest(settings, self.ROWS())
        X.extract_metrics(settings, ["TSM"], fetch=FakeSEC(tsm_facts_json()), filing_facts_loader=no_filing_xbrl)
        assert [p.name for p in X.xbrl_out_dir(settings).iterdir()] == ["TSM_key_metrics.parquet"]
        assert len(pd.read_parquet(self.parquet(settings))) > 0
