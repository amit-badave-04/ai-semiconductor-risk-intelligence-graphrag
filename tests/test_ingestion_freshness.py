"""Polite new-filing detection (semigraph.ingestion.freshness).

Fixtures follow the REAL submissions-JSON shape probed on 2026-09-25: parallel
arrays under ``filings.recent`` (form, filingDate, accessionNumber, reportDate,
...) and older pages listed under ``filings.files``. No network anywhere.
"""

import json
import logging
from datetime import date

import pytest

from semigraph.config import Settings
from semigraph.ingestion import edgar as E
from semigraph.ingestion import freshness as F

NVDA_CIK, MSFT_CIK, TSM_CIK = 1045810, 789019, 1046179


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(F, "SEC_PAUSE_S", 0)
    return Settings(data_dir=tmp_path / "data", sec_user_agent="Test test@example.com", _env_file=None)


def submissions(rows: list[tuple[str, str, str, str]], older_pages: int = 0) -> dict:
    """rows: (form, filingDate, accessionNumber, reportDate) -> submissions JSON."""
    return {
        "cik": "0001045810",
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": [r[2] for r in rows],
                "filingDate": [r[1] for r in rows],
                "reportDate": [r[3] for r in rows],
                "form": [r[0] for r in rows],
                "isXBRL": [1] * len(rows),
            },
            "files": [
                {"name": f"CIK-submissions-{i:03d}.json", "filingCount": 1487}
                for i in range(1, older_pages + 1)
            ],
        },
    }


NVDA_ROWS = [
    ("4", "2026-09-23", "0001696841-26-000014", "2026-09-21"),        # insider noise
    ("10-Q", "2026-08-26", "0001045810-26-000075", "2026-07-26"),     # NEW
    ("8-K", "2026-08-26", "0001045810-26-000074", ""),
    ("10-Q", "2026-05-20", "0001045810-26-000052", "2026-04-26"),
    ("10-K", "2026-02-25", "0001045810-26-000021", "2026-01-25"),
    ("10-K", "2022-03-18", "0001045810-22-000036", "2022-01-30"),     # before annual_since
    ("10-Q", "2025-11-19", "0001045810-25-000230", "2025-10-26"),     # before the latest 10-K
]


class FakeSEC:
    """``fetch(url)`` over canned submissions, keyed by CIK; logs every call."""

    def __init__(self, by_cik: dict[int, dict]):
        self.by_cik = by_cik
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        return self.by_cik[cik]


def write_manifest(settings: Settings, manifest: dict) -> None:
    E.edgar_dir(settings).mkdir(parents=True, exist_ok=True)
    E.manifest_path(settings).write_text(json.dumps(manifest), encoding="utf-8")


def manifest_row(ticker: str, cik: int, form: str, day: str, acc: str) -> dict:
    return {"ticker": ticker, "cik": cik, "form": form, "filing_date": day, "accession_no": acc}


@pytest.fixture
def lake(settings) -> Settings:
    write_manifest(settings, {
        "NVDA": [
            manifest_row("NVDA", NVDA_CIK, "10-K", "2026-02-25", "0001045810-26-000021"),
            manifest_row("NVDA", NVDA_CIK, "10-Q", "2026-05-20", "0001045810-26-000052"),
        ],
        "TSM": [manifest_row("TSM", TSM_CIK, "20-F", "2026-04-16", "0001628280-26-025362")],
    })
    return settings


# ------------------------------------------------------------- submissions

class TestFetchSubmissions:
    def test_url_is_zero_padded_cik(self):
        fake = FakeSEC({NVDA_CIK: submissions(NVDA_ROWS)})
        got = F.fetch_submissions(NVDA_CIK, fetch=fake)
        assert fake.urls == ["https://data.sec.gov/submissions/CIK0001045810.json"]
        assert got["filings"]["recent"]["form"][0] == "4"

    def test_default_fetch_sends_the_declared_identity_and_sleeps(self, settings, monkeypatch):
        seen: dict = {}
        sleeps: list[float] = []

        def fake_get(url: str, user_agent: str) -> dict:
            seen.update(url=url, ua=user_agent)
            return {"filings": {"recent": {}}}

        monkeypatch.setattr(F, "SEC_PAUSE_S", 0.15)
        monkeypatch.setattr(F, "_http_get_json", fake_get)
        monkeypatch.setattr(F.time, "sleep", sleeps.append)

        F.fetch_submissions(NVDA_CIK, settings=settings)

        assert seen == {"url": "https://data.sec.gov/submissions/CIK0001045810.json",
                        "ua": "Test test@example.com"}
        assert sleeps == [0.15]

    def test_missing_identity_is_refused_before_any_request(self, tmp_path):
        no_id = Settings(data_dir=tmp_path / "data", sec_user_agent="", _env_file=None)
        with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
            F.fetch_submissions(NVDA_CIK, settings=no_id)


class TestFetchSubmissionsErrors:
    def test_http_get_json_sends_the_user_agent_and_parses(self, monkeypatch):
        seen: dict = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return b'{"filings": {"recent": {}}}'

        def fake_urlopen(req, timeout):
            seen.update(ua=req.get_header("User-agent"), timeout=timeout)
            return Resp()

        monkeypatch.setattr(F.urllib.request, "urlopen", fake_urlopen)
        assert F._http_get_json("https://data.sec.gov/x", "Me me@example.com") == {"filings": {"recent": {}}}
        assert seen == {"ua": "Me me@example.com", "timeout": F.HTTP_TIMEOUT_S}

    def test_network_failures_are_wrapped_with_the_cik(self, settings, monkeypatch):
        def down(url: str, ua: str) -> dict:
            raise OSError("connection reset")

        monkeypatch.setattr(F, "_http_get_json", down)
        with pytest.raises(RuntimeError, match="CIK 1045810.*connection reset"):
            F.fetch_submissions(NVDA_CIK, settings=settings)


class TestRecordsFromSubmissions:
    def test_parses_parallel_arrays(self):
        recs = F.records_from_submissions("NVDA", NVDA_CIK, submissions(NVDA_ROWS))
        tenq = next(r for r in recs if r.accession_no == "0001045810-26-000075")
        assert (tenq.ticker, tenq.form, tenq.filing_date, tenq.cik) == (
            "NVDA", "10-Q", date(2026, 8, 26), NVDA_CIK)
        assert tenq.period_of_report == "2026-07-26"
        assert len(recs) == len(NVDA_ROWS)

    def test_blank_report_date_becomes_none(self):
        recs = F.records_from_submissions("NVDA", NVDA_CIK, submissions(NVDA_ROWS))
        assert next(r for r in recs if r.form == "8-K").period_of_report is None

    def test_rows_with_a_bad_date_are_skipped_and_logged(self, caplog):
        sub = submissions([("10-K", "not-a-date", "A", ""), ("10-K", "2026-01-01", "B", "")])
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.freshness"):
            recs = F.records_from_submissions("NVDA", NVDA_CIK, sub)
        assert [r.accession_no for r in recs] == ["B"] and "not-a-date" in caplog.text

    def test_arrays_of_different_lengths_are_logged_and_the_complete_rows_kept(self, caplog):
        sub = submissions([("10-K", "2026-01-01", "A", ""), ("10-Q", "2026-02-01", "B", "")])
        sub["filings"]["recent"]["accessionNumber"].pop()          # one accession short
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.freshness"):
            recs = F.records_from_submissions("NVDA", NVDA_CIK, sub)
        assert [r.accession_no for r in recs] == ["A"] and "disagree in length" in caplog.text

    def test_empty_or_odd_payloads_yield_nothing(self):
        assert F.records_from_submissions("NVDA", 1, {}) == []
        assert F.records_from_submissions("NVDA", 1, {"filings": {"recent": {}}}) == []


# ----------------------------------------------------------- pending_filings

class TestPendingFilings:
    def test_lists_only_new_in_scope_filings(self, lake):
        fake = FakeSEC({NVDA_CIK: submissions(NVDA_ROWS)})
        got = F.pending_filings(lake, ["NVDA"], fetch=fake)
        assert got == [{
            "ticker": "NVDA", "cik": NVDA_CIK, "form": "10-Q", "filing_date": "2026-08-26",
            "accession_no": "0001045810-26-000075", "period_of_report": "2026-07-26",
        }]

    def test_annual_since_window_keeps_old_filings_out(self, lake):
        # The 2022 10-K and the pre-FY26 10-Q are absent from the manifest yet must not be "pending".
        got = F.pending_filings(lake, ["NVDA"], fetch=FakeSEC({NVDA_CIK: submissions(NVDA_ROWS)}))
        assert {r["accession_no"] for r in got} == {"0001045810-26-000075"}

    def test_pending_matches_what_download_would_fetch(self, lake):
        """One shared selector: after ingesting the pending rows nothing is pending."""
        fake = FakeSEC({NVDA_CIK: submissions(NVDA_ROWS)})
        pending = F.pending_filings(lake, ["NVDA"], fetch=fake)
        manifest = E.load_manifest(lake)
        manifest["NVDA"] += [manifest_row("NVDA", NVDA_CIK, p["form"], p["filing_date"], p["accession_no"])
                             for p in pending]
        write_manifest(lake, manifest)
        assert F.pending_filings(lake, ["NVDA"], fetch=fake) == []

    def test_as_of_bounds_filing_date(self, lake):
        fake = FakeSEC({NVDA_CIK: submissions(NVDA_ROWS)})
        assert F.pending_filings(lake, ["NVDA"], as_of="2026-08-25", fetch=fake) == []
        assert len(F.pending_filings(lake, ["NVDA"], as_of=date(2026, 8, 26), fetch=fake)) == 1

    def test_annual_and_its_amendment_are_in_scope_amendments_of_quarterlies_are_not(self, lake):
        rows = NVDA_ROWS + [
            ("10-K", "2026-09-01", "NEW-10K", "2026-01-25"),
            ("10-K/A", "2026-09-02", "NEW-10KA", "2026-01-25"),
            ("10-Q/A", "2026-09-03", "NEW-10QA", "2026-07-26"),
        ]
        got = F.pending_filings(lake, ["NVDA"], fetch=FakeSEC({NVDA_CIK: submissions(rows)}))
        assert {r["accession_no"] for r in got} >= {"NEW-10K", "NEW-10KA"}
        assert "NEW-10QA" not in {r["accession_no"] for r in got}

    def test_twenty_f_filer_only_annual_forms(self, lake):
        sub = submissions([
            ("6-K", "2026-09-10", "SIX-K", "2026-08-31"),
            ("20-F", "2027-04-16", "NEW-20F", "2026-12-31"),
            ("20-F", "2026-04-16", "0001628280-26-025362", "2025-12-31"),
        ])
        got = F.pending_filings(lake, ["TSM"], fetch=FakeSEC({TSM_CIK: sub}))
        assert [r["accession_no"] for r in got] == ["NEW-20F"]

    def test_cik_comes_from_the_manifest_without_extra_requests(self, lake):
        fake = FakeSEC({NVDA_CIK: submissions(NVDA_ROWS), TSM_CIK: submissions([])})
        F.pending_filings(lake, ["NVDA", "TSM"], fetch=fake)
        assert fake.urls == [
            "https://data.sec.gov/submissions/CIK0001045810.json",
            "https://data.sec.gov/submissions/CIK0001046179.json",
        ]

    def test_ticker_missing_from_the_manifest_uses_company_tickers(self, lake):
        (E.edgar_dir(lake) / "company_tickers.json").write_text(
            json.dumps({"0": {"cik_str": MSFT_CIK, "ticker": "MSFT", "title": "Microsoft"}}), encoding="utf-8")
        sub = submissions([("10-K", "2026-07-29", "0001193125-26-323660", "2026-06-30")])
        got = F.pending_filings(lake, ["MSFT"], fetch=FakeSEC({MSFT_CIK: sub}))
        assert got[0]["accession_no"] == "0001193125-26-323660" and got[0]["cik"] == MSFT_CIK

    def test_a_ticker_unknown_to_company_tickers_raises_key_error(self, lake):
        (E.edgar_dir(lake) / "company_tickers.json").write_text(json.dumps({}), encoding="utf-8")
        with pytest.raises(KeyError, match="no CIK known"):
            F.pending_filings(lake, ["MSFT"], fetch=FakeSEC({}))

    def test_unknown_ticker_raises_key_error(self, lake):
        with pytest.raises(KeyError, match="NOPE"):
            F.pending_filings(lake, ["NOPE"], fetch=FakeSEC({}))

    def test_rows_sorted_by_ticker_order_then_date(self, lake):
        sub_n = submissions(NVDA_ROWS + [("10-Q", "2026-09-05", "N-LATER", "2026-08-30")])
        sub_t = submissions([("20-F", "2027-04-16", "NEW-20F", "2026-12-31")])
        got = F.pending_filings(lake, ["TSM", "NVDA"], fetch=FakeSEC({NVDA_CIK: sub_n, TSM_CIK: sub_t}))
        assert [r["accession_no"] for r in got] == ["NEW-20F", "0001045810-26-000075", "N-LATER"]

    def test_older_pages_are_reported_when_the_window_may_be_incomplete(self, lake, caplog):
        # oldest 'recent' filing (2025-11-19) is later than 2023-01-01 while older pages exist
        sub = submissions(NVDA_ROWS[:2] + NVDA_ROWS[-1:], older_pages=1)
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.freshness"):
            F.pending_filings(lake, ["NVDA"], fetch=FakeSEC({NVDA_CIK: sub}))
        assert "older" in caplog.text.lower() and "NVDA" in caplog.text

    def test_older_pages_are_quiet_when_recent_reaches_the_window(self, lake, caplog):
        sub = submissions(NVDA_ROWS, older_pages=1)          # includes a 2022 filing
        with caplog.at_level(logging.WARNING, logger="semigraph.ingestion.freshness"):
            F.pending_filings(lake, ["NVDA"], fetch=FakeSEC({NVDA_CIK: sub}))
        assert caplog.text == ""


# ------------------------------------------------- federal_register_pending

def fr_cache(settings: Settings, dates: list[str], legacy: bool = False) -> None:
    path = settings.raw_dir / "federal_register_bis_rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"results": [{"document_number": f"d{i}", "publication_date": d} for i, d in enumerate(dates)]}
    if not legacy:
        payload.update(count=len(dates), retrieved_at="2026-09-25T00:00:00+00:00")
    path.write_text(json.dumps(payload), encoding="utf-8")


class FakeFRCount:
    def __init__(self, count: int):
        self.count = count
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        return {"count": self.count, "total_pages": 9, "results": [{"document_number": "x"}] * 20}


class TestFederalRegisterPending:
    def test_reports_stored_live_and_new(self, settings):
        fr_cache(settings, ["2025-09-30", "2026-01-15", "2025-11-12"])
        fake = FakeFRCount(5)
        got = F.federal_register_pending(settings, fetch=fake)
        assert got == {"stored_count": 3, "stored_latest_date": "2026-01-15",
                       "live_count": 5, "new_since_stored": 2}

    def test_uses_a_count_only_query(self, settings):
        fr_cache(settings, ["2026-01-15"])
        fake = FakeFRCount(1)
        F.federal_register_pending(settings, fetch=fake)
        (url,) = fake.urls
        assert "industry-and-security-bureau" in url and "RULE" in url and "2022-01-01" in url
        assert "fields%5B%5D=document_number" in url and "term" not in url

    def test_as_of_bounds_both_sides_like_for_like(self, settings):
        fr_cache(settings, ["2025-09-30", "2026-01-15", "2026-08-24"])
        fake = FakeFRCount(2)
        got = F.federal_register_pending(settings, as_of="2026-01-31", fetch=fake)
        assert "publication_date%5D%5Blte%5D=2026-01-31" in fake.urls[0]
        assert got["stored_count"] == 2 and got["stored_latest_date"] == "2026-01-15"
        assert got["new_since_stored"] == 0

    def test_no_cache_means_everything_is_new(self, settings):
        got = F.federal_register_pending(settings, fetch=FakeFRCount(166))
        assert got == {"stored_count": 0, "stored_latest_date": None,
                       "live_count": 166, "new_since_stored": 166}

    def test_legacy_v1_cache_is_counted_as_stored(self, settings):
        fr_cache(settings, ["2025-09-16"] * 13, legacy=True)
        got = F.federal_register_pending(settings, fetch=FakeFRCount(166))
        assert got["stored_count"] == 13 and got["new_since_stored"] == 153

    def test_a_shrinking_live_count_never_reports_negative_new(self, settings):
        fr_cache(settings, ["2026-01-15", "2026-01-16"])
        assert F.federal_register_pending(settings, fetch=FakeFRCount(1))["new_since_stored"] == 0
