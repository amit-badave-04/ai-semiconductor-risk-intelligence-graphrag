"""Per-accession incremental EDGAR download (semigraph.ingestion.edgar).

No network: filings are listed through the injected ``lister`` seam and every
``html()`` is a counted fake. The v1 bug under test: a ticker already in the
manifest was skipped entirely, so newly filed 10-Qs / 10-Ks were never fetched.
"""

import json
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pytest

from semigraph.config import Settings
from semigraph.ingestion import edgar as E
from semigraph.ingestion.edgar import (
    FilingRecord,
    download_filings,
    load_manifest,
    merge_manifest_rows,
    parse_as_of,
    select_targets,
)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setattr(E, "SEC_PAUSE_S", 0)
    return Settings(data_dir=tmp_path / "data", _env_file=None)


class FakeEdgar:
    """A lister with a call log; mimics edgartools' amendments=True behaviour
    (asking for '10-K' also yields '10-K/A', asking for '10-Q' yields '10-Q/A')."""

    def __init__(self, filings: dict[str, list[tuple[str, str, str]]]):
        # ticker -> [(form, iso date, accession)]
        self.filings = filings
        self.html_calls: list[str] = []
        self.list_calls: list[tuple[str, str]] = []

    def record(self, ticker: str, form: str, day: str, acc: str) -> FilingRecord:
        def html() -> str:
            self.html_calls.append(acc)
            return f"<html>{ticker} {form} {acc}</html>"

        return FilingRecord(
            ticker=ticker,
            form=form,
            filing_date=date.fromisoformat(day),
            accession_no=acc,
            cik=1234,
            source_url=f"https://www.sec.gov/Archives/{acc}.htm",
            html=html,
        )

    def __call__(self, ticker: str, form: str) -> list[FilingRecord]:
        self.list_calls.append((ticker, form))
        return [
            self.record(ticker, f, d, a)
            for f, d, a in self.filings.get(ticker, [])
            if f == form or f == f"{form}/A"
        ]


def nvda_filings() -> dict[str, list[tuple[str, str, str]]]:
    return {
        "NVDA": [
            ("10-K", "2026-02-25", "0001-26-000021"),
            ("10-K", "2025-02-26", "0001-25-000023"),
            ("10-K", "2022-03-18", "0001-22-000036"),          # before annual_since
            ("10-Q", "2025-11-19", "0001-25-000230"),          # before the latest 10-K
            ("10-Q", "2026-05-20", "0001-26-000052"),
            ("10-Q", "2026-08-26", "0001-26-000075"),
        ]
    }


def read_manifest(settings: Settings) -> dict:
    return json.loads(E.manifest_path(settings).read_text(encoding="utf-8"))


def html_files(settings: Settings, ticker: str) -> list[str]:
    return sorted(p.name for p in (E.edgar_dir(settings) / ticker).glob("*.html"))


# ------------------------------------------------------------- pure helpers

class TestParseAsOf:
    def test_none_stays_none(self):
        assert parse_as_of(None) is None

    def test_iso_string(self):
        assert parse_as_of("2026-09-25") == date(2026, 9, 25)

    def test_date_passthrough(self):
        assert parse_as_of(date(2026, 9, 25)) == date(2026, 9, 25)

    def test_garbage_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_as_of("not-a-date")

    def test_datetime_is_narrowed_to_its_date(self):
        assert parse_as_of(datetime(2026, 9, 25, 13, 30)) == date(2026, 9, 25)


def rec(form: str, day: str, acc: str = "") -> FilingRecord:
    return FilingRecord(
        ticker="NVDA", form=form, filing_date=date.fromisoformat(day),
        accession_no=acc or f"acc-{form}-{day}", cik=1, source_url="",
    )


class TestSelectTargets:
    def test_annuals_from_annual_since_only(self):
        recs = [rec("10-K", "2022-03-18"), rec("10-K", "2023-02-24"), rec("10-K", "2026-02-25")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert [r.filing_date.isoformat() for r in got] == ["2023-02-24", "2026-02-25"]

    def test_amendment_of_annual_is_included(self):
        recs = [rec("10-K", "2026-02-04"), rec("10-K/A", "2026-02-04")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert sorted(r.form for r in got) == ["10-K", "10-K/A"]

    def test_quarterlies_only_after_latest_annual(self):
        recs = [
            rec("10-K", "2026-02-25"),
            rec("10-Q", "2025-11-19"),     # before the annual: not current-year
            rec("10-Q", "2026-05-20"),
            rec("10-Q", "2026-08-26"),
        ]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert [r.filing_date.isoformat() for r in got if r.form == "10-Q"] == [
            "2026-05-20", "2026-08-26",
        ]

    def test_quarterly_amendments_are_not_targets(self):
        recs = [rec("10-K", "2026-02-25"), rec("10-Q/A", "2026-06-01"), rec("10-Q", "2026-05-20")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert "10-Q/A" not in [r.form for r in got]

    def test_late_annual_amendment_does_not_move_the_quarterly_cutoff(self):
        # a Part III 10-K/A filed after the 10-Q must not drop that 10-Q
        recs = [rec("10-K", "2026-02-25"), rec("10-Q", "2026-05-20"), rec("10-K/A", "2026-06-30")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert "2026-05-20" in [r.filing_date.isoformat() for r in got if r.form == "10-Q"]

    def test_as_of_is_an_inclusive_upper_bound(self):
        recs = [rec("10-K", "2026-02-25"), rec("10-Q", "2026-05-20"), rec("10-Q", "2026-08-26")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=date(2026, 5, 20))
        assert [r.filing_date.isoformat() for r in got] == ["2026-02-25", "2026-05-20"]

    def test_as_of_can_hide_the_latest_annual(self):
        # as-of before the FY26 10-K: the previous annual anchors the quarterlies
        recs = [rec("10-K", "2025-02-26"), rec("10-K", "2026-02-25"), rec("10-Q", "2025-05-28")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=date(2025, 12, 31))
        assert [(r.form, r.filing_date.isoformat()) for r in got] == [
            ("10-K", "2025-02-26"), ("10-Q", "2025-05-28"),
        ]

    def test_no_quarterly_form_yields_annuals_only(self):
        recs = [rec("20-F", "2026-04-16"), rec("20-F", "2025-04-17"), rec("6-K", "2026-06-01")]
        got = select_targets(recs, "20-F", None, annual_since=2023, as_of=None)
        assert {r.form for r in got} == {"20-F"} and len(got) == 2

    def test_no_annual_target_falls_back_to_annual_since_window(self):
        recs = [rec("10-Q", "2022-11-01"), rec("10-Q", "2023-05-01")]
        got = select_targets(recs, "10-K", "10-Q", annual_since=2023, as_of=None)
        assert [r.filing_date.isoformat() for r in got] == ["2023-05-01"]

    def test_unrelated_forms_are_ignored(self):
        recs = [rec("8-K", "2026-03-01"), rec("4", "2026-03-02"), rec("10-K", "2026-02-25")]
        assert [r.form for r in select_targets(recs, "10-K", "10-Q", 2023, None)] == ["10-K"]

    def test_result_is_sorted_and_duplicates_collapse(self):
        a = rec("10-K", "2026-02-25", "A")
        got = select_targets([a, a, rec("10-K", "2025-02-26", "B")], "10-K", "10-Q", 2023, None)
        assert [r.accession_no for r in got] == ["B", "A"]


class TestMergeManifestRows:
    def row(self, acc: str, day: str, **extra) -> dict:
        return {"accession_no": acc, "filing_date": day, "form": "10-K", **extra}

    def test_existing_rows_preserved_verbatim(self):
        old = self.row("A", "2024-01-01", legacy_field="keep me", local_path="data\\x.html")
        merged = merge_manifest_rows([old], [self.row("B", "2025-01-01")])
        assert old in merged and merged[0] is old

    def test_same_accession_is_not_rewritten(self):
        old = self.row("A", "2024-01-01", size_bytes=1)
        merged = merge_manifest_rows([old], [self.row("A", "2024-01-01", size_bytes=999)])
        assert merged == [old]

    def test_rows_outside_the_new_rules_are_never_dropped(self):
        old = self.row("OLD", "2019-01-01")
        assert old in merge_manifest_rows([old], [])

    def test_sorted_by_filing_date_then_accession(self):
        merged = merge_manifest_rows(
            [self.row("Z", "2025-01-01"), self.row("A", "2026-01-01")],
            [self.row("B", "2025-01-01"), self.row("C", "2024-06-01")],
        )
        assert [r["accession_no"] for r in merged] == ["C", "B", "Z", "A"]

    def test_inputs_are_not_mutated(self):
        old = [self.row("A", "2024-01-01")]
        new = [self.row("B", "2023-01-01")]
        merge_manifest_rows(old, new)
        assert [r["accession_no"] for r in old] == ["A"] and len(new) == 1


# ------------------------------------------------------------ download_filings

class TestDownloadFilings:
    def test_fresh_download_writes_files_and_manifest(self, settings):
        fake = FakeEdgar(nvda_filings())
        out = download_filings(settings, ["NVDA"], lister=fake)

        rows = read_manifest(settings)["NVDA"]
        # 2 annuals >= 2023 + the two 10-Qs after the latest annual
        assert [(r["form"], r["filing_date"]) for r in rows] == [
            ("10-K", "2025-02-26"), ("10-K", "2026-02-25"),
            ("10-Q", "2026-05-20"), ("10-Q", "2026-08-26"),
        ]
        assert out["tickers"]["NVDA"]["filings"] == 4
        assert out["tickers"]["NVDA"]["cached"] is False
        assert out["tickers"]["NVDA"]["new"] == [r["accession_no"] for r in rows]
        assert out["total_filings"] == 4
        assert out["manifest_path"] == str(E.manifest_path(settings))
        assert len(html_files(settings, "NVDA")) == 4

    def test_manifest_row_shape_and_layout(self, settings):
        download_filings(settings, ["NVDA"], lister=FakeEdgar(nvda_filings()))
        row = read_manifest(settings)["NVDA"][0]
        assert set(row) == {
            "ticker", "cik", "form", "filing_date", "accession_no",
            "source_url", "local_path", "size_bytes",
        }
        assert row["cik"] == 1234 and row["ticker"] == "NVDA"
        local = E.resolve_local_path(settings, row)
        assert local.exists() and local.name == "10-K_2025-02-26_0001-25-000023.html"
        assert local.parent.name == "NVDA" and local.parent.parent.name == "edgar"
        assert row["size_bytes"] == local.stat().st_size

    def test_slash_in_form_becomes_dash_in_filename(self, settings):
        fake = FakeEdgar({"NVDA": [("10-K", "2026-02-04", "A-1"), ("10-K/A", "2026-02-04", "A-2")]})
        download_filings(settings, ["NVDA"], lister=fake)
        assert "10-K-A_2026-02-04_A-2.html" in html_files(settings, "NVDA")

    def test_new_filing_for_already_manifested_ticker_is_fetched(self, settings):
        """The v1 bug: a ticker already in the manifest was skipped wholesale."""
        first = FakeEdgar(
            {"NVDA": [f for f in nvda_filings()["NVDA"] if f[2] != "0001-26-000075"]}
        )
        download_filings(settings, ["NVDA"], lister=first)
        assert "0001-26-000075" not in {r["accession_no"] for r in read_manifest(settings)["NVDA"]}

        second = FakeEdgar(nvda_filings())          # the Aug-26 10-Q has now been filed
        out = download_filings(settings, ["NVDA"], lister=second)

        assert out["tickers"]["NVDA"]["new"] == ["0001-26-000075"]
        assert out["tickers"]["NVDA"]["cached"] is False
        assert second.html_calls == ["0001-26-000075"]      # only the new one is downloaded
        assert "0001-26-000075" in {r["accession_no"] for r in read_manifest(settings)["NVDA"]}

    def test_existing_rows_are_untouched(self, settings):
        legacy = {
            "ticker": "NVDA", "cik": 1045810, "form": "10-K", "filing_date": "2026-02-25",
            "accession_no": "0001-26-000021", "source_url": "https://legacy/url",
            "local_path": "data\\raw\\edgar\\NVDA\\legacy-name.html", "size_bytes": 42,
            "note": "hand-edited",
        }
        outside_rules = dict(legacy, accession_no="OLD-2019", filing_date="2019-02-01",
                             form="10-K", local_path="data\\raw\\edgar\\NVDA\\old.html")
        E.edgar_dir(settings).mkdir(parents=True)
        E.manifest_path(settings).write_text(
            json.dumps({"NVDA": [legacy, outside_rules]}), encoding="utf-8"
        )
        fake = FakeEdgar(nvda_filings())

        download_filings(settings, ["NVDA"], lister=fake)

        rows = {r["accession_no"]: r for r in read_manifest(settings)["NVDA"]}
        assert rows["0001-26-000021"] == legacy                     # verbatim, incl. odd fields
        assert rows["OLD-2019"] == outside_rules                    # not dropped by the new rules
        assert "0001-26-000021" not in fake.html_calls              # not re-fetched

    def test_second_run_is_idempotent(self, settings):
        download_filings(settings, ["NVDA"], lister=FakeEdgar(nvda_filings()))
        before = E.manifest_path(settings).read_bytes()
        again = FakeEdgar(nvda_filings())

        out = download_filings(settings, ["NVDA"], lister=again)

        assert again.html_calls == []
        assert out["tickers"]["NVDA"]["new"] == []
        assert out["tickers"]["NVDA"]["cached"] is True
        assert out["tickers"]["NVDA"]["filings"] == 4
        assert E.manifest_path(settings).read_bytes() == before

    def test_a_ticker_with_nothing_new_keeps_its_v1_manifest_bytes(self, settings):
        """First v2 run over a v1 manifest (newest-first rows): no new filing => no rewrite, so
        the manifest bytes (and the snapshot id derived from them) only change when data does."""
        rows = [
            {"ticker": "TSM", "cik": 1046179, "form": "20-F", "filing_date": "2026-04-16",
             "accession_no": "T-26", "source_url": "u", "local_path": "x", "size_bytes": 1},
            {"ticker": "TSM", "cik": 1046179, "form": "20-F", "filing_date": "2025-04-17",
             "accession_no": "T-25", "source_url": "u", "local_path": "y", "size_bytes": 1},
        ]
        E.edgar_dir(settings).mkdir(parents=True)
        E.manifest_path(settings).write_text(json.dumps({"TSM": rows}, indent=2), encoding="utf-8")
        before = E.manifest_path(settings).read_bytes()
        fake = FakeEdgar({"TSM": [("20-F", "2026-04-16", "T-26"), ("20-F", "2025-04-17", "T-25")]})

        out = download_filings(settings, ["TSM"], lister=fake)

        assert out["tickers"]["TSM"]["cached"] is True and fake.html_calls == []
        assert E.manifest_path(settings).read_bytes() == before

    def test_html_is_downloaded_only_when_the_file_is_missing(self, settings):
        cdir = E.edgar_dir(settings) / "NVDA"
        cdir.mkdir(parents=True)
        (cdir / "10-K_2026-02-25_0001-26-000021.html").write_text("already here", encoding="utf-8")
        fake = FakeEdgar({"NVDA": [("10-K", "2026-02-25", "0001-26-000021")]})

        download_filings(settings, ["NVDA"], lister=fake)

        assert fake.html_calls == []
        assert (cdir / "10-K_2026-02-25_0001-26-000021.html").read_text(encoding="utf-8") == "already here"
        row = read_manifest(settings)["NVDA"][0]
        assert row["size_bytes"] == len("already here")

    def test_as_of_bounds_filing_date(self, settings):
        fake = FakeEdgar(nvda_filings())
        download_filings(settings, ["NVDA"], as_of="2026-05-20", lister=fake)
        dates = [r["filing_date"] for r in read_manifest(settings)["NVDA"]]
        assert max(dates) == "2026-05-20" and "0001-26-000075" not in fake.html_calls

    def test_as_of_accepts_date_objects(self, settings):
        download_filings(settings, ["NVDA"], as_of=date(2025, 12, 31), lister=FakeEdgar(nvda_filings()))
        # FY25 10-K (2025-02-26) anchors the quarterlies; the FY26 10-K/10-Qs are past as_of
        assert {r["filing_date"] for r in read_manifest(settings)["NVDA"]} == {
            "2025-02-26", "2025-11-19",
        }

    def test_twenty_f_filer_has_no_quarterlies(self, settings):
        fake = FakeEdgar({"TSM": [("20-F", "2026-04-16", "T-26"), ("20-F", "2025-04-17", "T-25"),
                                  ("6-K", "2026-06-01", "T-6K")]})
        out = download_filings(settings, ["TSM"], lister=fake)
        assert out["tickers"]["TSM"]["filings"] == 2
        assert ("TSM", "20-F") in fake.list_calls and not any(f == "10-Q" for _, f in fake.list_calls)

    def test_unknown_ticker_raises_key_error_before_any_work(self, settings):
        fake = FakeEdgar(nvda_filings())
        with pytest.raises(KeyError, match="NOPE"):
            download_filings(settings, ["NVDA", "NOPE"], lister=fake)
        assert fake.list_calls == [] and not E.manifest_path(settings).exists()

    def test_manifest_is_written_after_each_ticker(self, settings):
        """An interruption on ticker 2 must not lose ticker 1."""
        fake = FakeEdgar({**nvda_filings(), "TSM": [("20-F", "2026-04-16", "T-26")]})

        def flaky(ticker: str, form: str):
            if ticker == "TSM":
                raise RuntimeError("SEC hiccup")
            return fake(ticker, form)

        with pytest.raises(RuntimeError, match="SEC hiccup"):
            download_filings(settings, ["NVDA", "TSM"], lister=flaky)
        assert len(read_manifest(settings)["NVDA"]) == 4

    def test_manifest_write_is_atomic_and_leaves_no_temp_files(self, settings):
        download_filings(settings, ["NVDA"], lister=FakeEdgar(nvda_filings()))
        leftovers = [p.name for p in E.edgar_dir(settings).iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_rows_sorted_by_date_then_accession(self, settings):
        fake = FakeEdgar({"NVDA": [("10-K", "2026-02-25", "Z"), ("10-K", "2026-02-25", "A"),
                                   ("10-K", "2025-02-26", "M")]})
        download_filings(settings, ["NVDA"], lister=fake)
        assert [r["accession_no"] for r in read_manifest(settings)["NVDA"]] == ["M", "A", "Z"]

    def test_only_requested_tickers_are_touched_and_counted(self, settings):
        fake = FakeEdgar({**nvda_filings(), "TSM": [("20-F", "2026-04-16", "T-26")]})
        download_filings(settings, ["NVDA"], lister=fake)
        out = download_filings(settings, ["TSM"], lister=fake)
        assert set(read_manifest(settings)) == {"NVDA", "TSM"}
        assert out["total_filings"] == 1 and set(out["tickers"]) == {"TSM"}

    def test_default_tickers_is_the_whole_universe(self, settings):
        fake = FakeEdgar({})
        download_filings(settings, lister=fake)
        assert {t for t, _ in fake.list_calls} == set(E.FILERS)

    def test_a_filing_that_cannot_be_fetched_is_reported_and_the_rest_still_land(self, settings):
        def broken() -> str:
            raise OSError("404")

        good = FakeEdgar({"NVDA": [("10-K", "2026-02-25", "OK-1")]})
        bad = FilingRecord("NVDA", "10-K", date(2026, 3, 1), "BAD-1", 1, "u", html=broken)

        def lister(ticker: str, form: str):
            return good(ticker, form) + [bad]

        out = download_filings(settings, ["NVDA"], lister=lister)

        accessions = {r["accession_no"] for r in load_manifest(settings).get("NVDA", [])}
        assert "OK-1" in accessions and "BAD-1" not in accessions       # no manifest row for the failure
        assert not any("BAD-1" in n for n in html_files(settings, "NVDA"))
        failed = out["tickers"]["NVDA"]["failed"]
        assert [f["accession_no"] for f in failed] == ["BAD-1"] and "404" in failed[0]["error"]
        assert out["failed_total"] == 1

    def test_a_failure_in_one_ticker_does_not_stop_the_next_ticker(self, settings):
        def broken() -> str:
            raise OSError("boom")

        ok = FakeEdgar({"AMD": [("10-K", "2026-02-04", "AMD-1")]})

        def lister(ticker: str, form: str):
            if ticker == "NVDA":
                return [FilingRecord("NVDA", form, date(2026, 3, 1), "BAD-2", 1, "u", html=broken)] if form == "10-K" else []
            return ok(ticker, form)

        out = download_filings(settings, ["NVDA", "AMD"], lister=lister)

        assert out["tickers"]["AMD"]["new"] == ["AMD-1"]
        assert out["failed_total"] == 1

    def test_html_fallback_rescues_a_filing_whose_primary_fetch_raises(self, settings):
        """INTC 2026-07-24: edgartools found no primary document (AttributeError); the
        submissions metadata names it, so a direct fetch recovers the filing."""
        def no_primary() -> str:
            raise AttributeError("'NoneType' object has no attribute 'download'")

        rec = FilingRecord("INTC", "10-Q", date(2026, 7, 24), "0000050863-26-000157", 50863, "u", html=no_primary)
        used: list[str] = []

        def fallback(r: FilingRecord) -> str:
            used.append(r.accession_no)
            return "<html>direct</html>"

        out = download_filings(settings, ["INTC"],
                               lister=lambda t, f: [rec] if f == "10-Q" else [], html_fallback=fallback)

        assert used == ["0000050863-26-000157"]
        assert out["tickers"]["INTC"]["new"] == ["0000050863-26-000157"] and out["failed_total"] == 0
        assert "direct" in next(iter((Path(settings.data_dir) / "raw" / "edgar" / "INTC").glob("*.html"))).read_text(encoding="utf-8")

    def test_when_the_fallback_also_fails_both_errors_are_reported(self, settings):
        def no_primary() -> str:
            raise AttributeError("no primary document")

        rec = FilingRecord("INTC", "10-Q", date(2026, 7, 24), "F-1", 1, "u", html=no_primary)

        def fallback(_r: FilingRecord) -> str:
            raise OSError("403 forbidden")

        out = download_filings(settings, ["INTC"], lister=lambda t, f: [rec] if f == "10-Q" else [],
                               html_fallback=fallback)

        err = out["tickers"]["INTC"]["failed"][0]["error"]
        assert "no primary document" in err and "403 forbidden" in err


class TestEdgartoolsWrapper:
    """The default lister wraps edgartools; ``filing.document`` costs one SEC
    request per filing, so it must stay deferred until a filing is a target."""

    class FakeFiling:
        form = "10-Q"
        filing_date = date(2026, 8, 26)
        accession_no = "0001045810-26-000075"
        cik = 1045810
        report_date = "2026-07-26"
        document_reads = 0

        @property
        def period_of_report(self):  # edgartools fetches the homepage here
            raise AssertionError("period_of_report must not be read")

        @property
        def document(self):
            type(self).document_reads += 1
            return type("Doc", (), {"url": "https://www.sec.gov/Archives/x.htm"})()

        def html(self) -> str:
            return "<html>fake</html>"

    def test_record_is_built_without_touching_document(self):
        self.FakeFiling.document_reads = 0
        rec = E._record_from_edgartools("NVDA", self.FakeFiling())
        assert (rec.form, rec.filing_date, rec.cik, rec.period_of_report) == (
            "10-Q", date(2026, 8, 26), 1045810, "2026-07-26",
        )
        assert rec.html() == "<html>fake</html>"
        assert self.FakeFiling.document_reads == 0

    def test_source_url_is_resolved_only_for_new_rows(self, settings):
        self.FakeFiling.document_reads = 0
        fresh = E._record_from_edgartools("NVDA", self.FakeFiling())

        def looked_up() -> str:
            raise AssertionError("URL of an already-manifested filing was looked up")

        old_10k = FilingRecord("NVDA", "10-K", date(2026, 2, 25), "OLD", 1,
                               resolve_source_url=looked_up)
        E.edgar_dir(settings).mkdir(parents=True)
        E.manifest_path(settings).write_text(
            json.dumps({"NVDA": [{"accession_no": "OLD", "filing_date": "2026-02-25"}]}),
            encoding="utf-8",
        )

        def lister(ticker: str, form: str):
            return [old_10k] if form == "10-K" else [fresh]

        download_filings(settings, ["NVDA"], lister=lister)

        assert self.FakeFiling.document_reads == 1
        rows = {r["accession_no"]: r for r in read_manifest(settings)["NVDA"]}
        assert rows["0001045810-26-000075"]["source_url"] == "https://www.sec.gov/Archives/x.htm"


class TestDefaultLister:
    """download_filings without an injected lister goes through edgartools."""

    def fake_edgartools(self, monkeypatch):
        log: dict = {"identity": [], "companies": [], "get_filings": []}

        class Filing:
            def __init__(self, form: str, day: str, acc: str):
                self.form, self.filing_date, self.accession_no, self.cik = form, date.fromisoformat(day), acc, 1045810
                self.report_date = "2026-01-25"
                self.document = types.SimpleNamespace(url=f"https://www.sec.gov/Archives/{acc}.htm")

            def html(self) -> str:
                return f"<html>{self.accession_no}</html>"

        class Company:
            def __init__(self, ticker: str):
                log["companies"].append(ticker)

            def get_filings(self, *, form: str):
                log["get_filings"].append(form)
                return {"10-K": [Filing("10-K", "2026-02-25", "K-1"), Filing("10-K/A", "2026-02-26", "KA-1")],
                        "10-Q": [Filing("10-Q", "2026-05-20", "Q-1"), Filing("10-Q/A", "2026-06-01", "QA-1")]}[form]

        module = types.ModuleType("edgar")
        module.set_identity = log["identity"].append
        module.Company = Company
        monkeypatch.setitem(sys.modules, "edgar", module)
        return log

    def test_lists_through_company_get_filings_and_downloads(self, tmp_path, monkeypatch):
        log = self.fake_edgartools(monkeypatch)
        st = Settings(data_dir=tmp_path / "data", sec_user_agent="Jane jane@example.com", _env_file=None)

        out = download_filings(st, ["NVDA"])

        assert log["identity"] == ["Jane jane@example.com"]
        assert log["companies"] == ["NVDA"]                       # one Company per ticker
        assert log["get_filings"] == ["10-K", "10-Q"]
        rows = {r["accession_no"]: r for r in read_manifest(st)["NVDA"]}
        assert set(rows) == {"K-1", "KA-1", "Q-1"}               # 10-Q/A excluded, 10-K/A kept
        assert rows["K-1"]["source_url"] == "https://www.sec.gov/Archives/K-1.htm"
        assert E.resolve_local_path(st, rows["Q-1"]).read_text(encoding="utf-8") == "<html>Q-1</html>"
        assert out["tickers"]["NVDA"]["filings"] == 3

    def test_missing_identity_is_refused(self, tmp_path, monkeypatch):
        self.fake_edgartools(monkeypatch)
        st = Settings(data_dir=tmp_path / "data", sec_user_agent="", _env_file=None)
        with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
            download_filings(st, ["NVDA"])


class TestAtomicWrite:
    def test_retries_a_transient_permission_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(E.time, "sleep", lambda s: None)
        real_replace, calls = E.os.replace, {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("OneDrive holds the file")
            real_replace(src, dst)

        monkeypatch.setattr(E.os, "replace", flaky)
        target = tmp_path / "m.json"
        E.atomic_write_text(target, "{}")
        assert target.read_text(encoding="utf-8") == "{}" and calls["n"] == 2
        assert list(tmp_path.glob("*.tmp")) == []

    def test_gives_up_after_the_retries_and_cleans_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(E.time, "sleep", lambda s: None)

        def locked(src, dst):
            raise PermissionError("locked")

        monkeypatch.setattr(E.os, "replace", locked)
        with pytest.raises(PermissionError):
            E.atomic_write_text(tmp_path / "m.json", "{}")
        assert list(tmp_path.iterdir()) == []


class TestRecordWithoutHtml:
    def test_a_target_with_no_html_source_is_reported_not_silently_skipped(self, settings):
        bare = FilingRecord("NVDA", "10-K", date(2026, 2, 25), "BARE-1", 1, "u")
        out = download_filings(settings, ["NVDA"], lister=lambda t, f: [bare] if f == "10-K" else [])
        failed = out["tickers"]["NVDA"]["failed"]
        assert failed[0]["accession_no"] == "BARE-1" and "no html" in failed[0]["error"]


class TestSubmissionsFallback:
    """The real fallback: primaryDocument from the submissions JSON -> direct Archive URL."""

    SUBMISSIONS = {"filings": {"recent": {
        "accessionNumber": ["0000050863-26-000157"], "primaryDocument": ["intc-20260627.htm"]}}}

    def test_builds_the_archive_url_from_the_primary_document(self):
        urls: list[str] = []

        def fetch(url: str) -> bytes:
            urls.append(url)
            return json.dumps(self.SUBMISSIONS).encode() if "submissions" in url else "<html>ok</html>".encode()

        rec = FilingRecord("INTC", "10-Q", date(2026, 7, 24), "0000050863-26-000157", 50863, "u")

        assert E.submissions_html_fallback(fetch)(rec) == "<html>ok</html>"
        assert urls == ["https://data.sec.gov/submissions/CIK0000050863.json",
                        "https://www.sec.gov/Archives/edgar/data/50863/000005086326000157/intc-20260627.htm"]

    def test_the_submissions_document_is_fetched_once_per_cik(self):
        calls: list[str] = []

        def fetch(url: str) -> bytes:
            calls.append(url)
            return json.dumps(self.SUBMISSIONS).encode() if "submissions" in url else b"<html/>"

        fallback = E.submissions_html_fallback(fetch)
        rec = FilingRecord("INTC", "10-Q", date(2026, 7, 24), "0000050863-26-000157", 50863, "u")
        fallback(rec)
        fallback(rec)

        assert sum("submissions" in c for c in calls) == 1

    def test_an_accession_missing_from_recent_is_an_explicit_error(self):
        fallback = E.submissions_html_fallback(lambda url: json.dumps(self.SUBMISSIONS).encode())
        rec = FilingRecord("INTC", "10-Q", date(2020, 1, 1), "0000050863-20-000001", 50863, "u")
        with pytest.raises(LookupError, match="0000050863-20-000001"):
            fallback(rec)


class TestLoadTickerToCik:
    def test_downloads_once_then_reads_the_persisted_file(self, settings, monkeypatch):
        st = Settings(data_dir=settings.data_dir, sec_user_agent="Jane jane@example.com", _env_file=None)
        calls: list[str] = []

        def fake_get(url: str, ua: str) -> bytes:
            calls.append(ua)
            return json.dumps({"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}}).encode()

        monkeypatch.setattr(E, "_sec_get", fake_get)
        assert E.load_ticker_to_cik(st) == {"NVDA": 1045810}
        assert E.load_ticker_to_cik(st) == {"NVDA": 1045810}
        assert calls == ["Jane jane@example.com"]


class TestBackwardCompatibleHelpers:
    def test_load_manifest_empty_when_absent(self, settings):
        assert load_manifest(settings) == {}

    def test_resolve_local_path_is_relative_to_project_root(self, settings):
        row = {"local_path": "data/raw/edgar/NVDA/x.html"}
        assert E.resolve_local_path(settings, row) == E.project_root(settings) / "data/raw/edgar/NVDA/x.html"
        assert isinstance(E.resolve_local_path(settings, row), Path)
