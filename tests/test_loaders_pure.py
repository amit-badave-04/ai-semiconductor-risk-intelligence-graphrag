"""Pure-function tests for the graph loaders — no Neo4j, no models, no network.

Covers the filing-state derivation (version props, validity interval,
retrievability), which chunks become EvidenceSpans, row building, the export
control relevance / keyword / cap logic, and snapshot stamping.
"""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.graph import loaders
from semigraph.hashing import content_hash
from semigraph.versions import compute_filing_versions

OPEN_END = "9999-12-31"


# ------------------------------------------------------------------ fixtures

def m(acc, form, filing_date, ticker="AMD"):
    return {"ticker": ticker, "cik": 2488, "accession_no": acc, "form": form,
            "filing_date": filing_date, "source_url": f"https://sec.example/{acc}"}


# AMD, as in the real manifest: a same-day 10-K/A that was never segmented
AMD_MANIFEST = [
    m("a-24", "10-K", "2024-01-31"), m("a-25", "10-K", "2025-02-05"),
    m("a-26", "10-K", "2026-02-04"), m("a-26A", "10-K/A", "2026-02-04"),
    m("q-may", "10-Q", "2026-05-06"), m("q-aug", "10-Q", "2026-08-05"),
]
ANNUAL_SECTIONS = {"I.1", "I.1A", "II.7"}
# accession -> section ids present in its chunks (the unparsed 10-K/A a-26A has none)
AMD_SECTIONS = {"a-24": ANNUAL_SECTIONS, "a-25": ANNUAL_SECTIONS, "a-26": ANNUAL_SECTIONS,
                "q-may": {"II.1A"}, "q-aug": {"II.1A"}}
FULL_AMENDMENT = {**AMD_SECTIONS, "a-26A": ANNUAL_SECTIONS}   # restates every section
MDNA_AMENDMENT = {**AMD_SECTIONS, "a-26A": {"II.7"}}          # AMD's real 10-K/A: Item 7 only


def states_for(manifest=AMD_MANIFEST, sections=AMD_SECTIONS, ticker="AMD"):
    versions = loaders.versions_for_ticker(ticker, manifest, sections)
    return loaders.derive_filing_states(manifest, versions, sections), versions


def chunk_rows(acc, form, filing_date, sections=("I.1", "I.1A"), n=2, ticker="AMD"):
    return [{
        "chunk_id": f"{acc}:{sid}:{k:04d}", "ticker": ticker, "cik": 2488, "form": form,
        "filing_date": filing_date, "accession_no": acc, "section_id": sid,
        "section_title": f"title {sid}", "sub_heading": None, "kind": "prose",
        "text": f"{acc} {sid} text {k}", "char_start": 0, "char_end": 10, "n_tokens": 12,
        "source_url": f"https://sec.example/{acc}",
    } for sid in sections for k in range(n)]


def frame(*filings, **kw):
    rows = [r for f in filings for r in chunk_rows(*f, **kw)]
    return pd.DataFrame(rows)


def accessions(df):
    return set(df["accession_no"])


# ------------------------------------------------------------ filing states

class TestFilingStates:
    def test_current_annual_is_open_ended_and_retrievable(self):
        states, _ = states_for()
        s = states["a-26"]
        assert (s.status, s.is_current, s.retrievable) == ("current", True, True)
        assert (s.valid_from, s.valid_to) == ("2026-02-04", OPEN_END)
        assert s.superseded_by is None and s.supersede_kind is None

    def test_superseded_annual_stays_retrievable_and_ends_when_replaced(self):
        states, _ = states_for()
        s = states["a-25"]
        assert (s.status, s.supersede_kind, s.superseded_by) == ("superseded", "rolled", "a-26")
        assert (s.is_current, s.retrievable) == (False, True)  # historical annual risk text stays searchable
        assert (s.valid_from, s.valid_to) == ("2025-02-05", "2026-02-04")

    def test_superseded_quarterly_is_not_retrievable(self):
        states, _ = states_for()
        s = states["q-may"]
        assert (s.status, s.superseded_by) == ("superseded", "q-aug")
        assert (s.is_current, s.retrievable) == (False, False)
        assert s.valid_to == "2026-08-05"

    def test_latest_quarterly_is_current(self):
        states, _ = states_for()
        assert (states["q-aug"].is_current, states["q-aug"].valid_to) == (True, OPEN_END)

    def test_unparsed_amendment_is_inert_and_never_valid(self):
        states, _ = states_for()
        s = states["a-26A"]
        assert (s.status, s.is_current, s.retrievable) == ("amendment", False, False)
        assert s.superseded_by is None
        assert s.valid_to == s.valid_from == "2026-02-04"  # empty interval, never null

    def test_parsed_amendment_corrects_its_original_on_the_same_day(self):
        states, _ = states_for(sections=FULL_AMENDMENT)
        original, amendment = states["a-26"], states["a-26A"]
        assert (original.status, original.supersede_kind, original.superseded_by) == ("corrected", "corrected", "a-26A")
        assert (original.is_current, original.retrievable) == (False, False)  # corrected originals are NOT retrievable
        assert original.valid_from == original.valid_to == "2026-02-04"
        assert (amendment.status, amendment.is_current, amendment.valid_to) == ("current", True, OPEN_END)

    def test_form_outside_the_three_families_is_inert_not_lost(self):
        manifest = AMD_MANIFEST + [m("q-A", "10-Q/A", "2026-06-01")]
        states, _ = states_for(manifest)
        s = states["q-A"]
        assert (s.status, s.is_current, s.retrievable, s.valid_to) == ("amendment", False, False, "2026-06-01")

    def test_every_manifest_row_gets_a_state(self):
        states, _ = states_for()
        assert set(states) == {r["accession_no"] for r in AMD_MANIFEST}

    def test_at_most_one_current_filing_per_family(self):
        states, versions = states_for()
        family = {v.accession_no: v.family for v in versions}
        current = [(family[a], a) for a, s in states.items() if s.is_current and a in family]
        assert sorted(f for f, _ in current) == ["annual", "quarterly"]

    def test_foreign_filer_has_annuals_only(self):
        manifest = [m("f-25", "20-F", "2025-03-05", "ASML"), m("f-26", "20-F", "2026-02-25", "ASML")]
        states, _ = states_for(manifest, {"f-25": {"I.3"}, "f-26": {"I.3"}}, ticker="ASML")
        assert (states["f-26"].is_current, states["f-25"].retrievable) == (True, True)

    def test_new_annual_retires_a_quarterly_that_uses_a_later_smaller_accession(self):
        # MSFT: the filing agent changed accession prefix; order is by date, never by string
        manifest = [m("0001564590-99", "10-Q", "2026-01-28", "MSFT"), m("0000950170-01", "10-Q", "2026-04-29", "MSFT"),
                    m("k-26", "10-K", "2026-07-29", "MSFT")]
        states, _ = states_for(manifest, {r["accession_no"]: {"I.1A"} for r in manifest}, ticker="MSFT")
        assert states["0001564590-99"].superseded_by == "0000950170-01"
        assert states["0000950170-01"].superseded_by == "k-26"
        assert states["0000950170-01"].valid_to == "2026-07-29"
        assert states["k-26"].is_current

    def test_versions_come_from_the_shared_supersession_rules(self):
        _, versions = states_for()
        expected = compute_filing_versions(AMD_MANIFEST, annual_form="10-K", quarterly_form="10-Q",
                                           parsed=set(AMD_SECTIONS), sections=AMD_SECTIONS)
        assert versions == expected


class TestFilingAndSupersedesRows:
    def test_filing_rows_carry_manifest_props_and_version_props(self):
        states, _ = states_for()
        rows = {r["accession_no"]: r for r in loaders.build_filing_rows(AMD_MANIFEST, states)}
        old = rows["a-25"]
        assert old["ticker"] == "AMD" and old["form"] == "10-K" and old["filing_date"] == "2025-02-05"
        assert old["source_url"] == "https://sec.example/a-25"
        assert (old["status"], old["supersede_kind"], old["superseded_by"], old["is_current"]) == (
            "superseded", "rolled", "a-26", False)
        cur = rows["a-26"]
        assert cur["superseded_by"] is None and cur["supersede_kind"] is None and cur["is_current"] is True

    def test_supersedes_edges_run_newer_to_older_for_every_superseded_filing(self):
        states, _ = states_for()
        edges = loaders.build_supersedes_rows(states)
        assert {(e["newer"], e["older"], e["kind"]) for e in edges} == {
            ("a-25", "a-24", "rolled"), ("a-26", "a-25", "rolled"), ("q-aug", "q-may", "rolled")}
        assert len(edges) == 3  # the current filings and the inert amendment are never the older end

    def test_corrected_original_is_the_older_end_of_a_corrected_edge(self):
        states, _ = states_for(sections=FULL_AMENDMENT)
        edges = {(e["newer"], e["older"], e["kind"]) for e in loaders.build_supersedes_rows(states)}
        assert ("a-26A", "a-26", "corrected") in edges
        assert ("a-26A", "a-25", "rolled") in edges


# ------------------------------------------- section-aware amendments (AMD shape)
#
# AMD's 10-K/A (a-26A, same day as the 10-K) restates ONLY Item 7 (II.7): the
# original a-26 {I.1, I.1A, II.7} must keep its Business and Risk Factors.

def span_flags(states, accession, section):
    f = loaders.span_freshness(states[accession], section)
    return (f.status, f.is_current, f.retrievable, f.valid_to)


class TestPartialAmendmentOverlay:
    def states(self, manifest=AMD_MANIFEST, sections=MDNA_AMENDMENT):
        return states_for(manifest, sections)

    def test_original_stays_current_and_the_overlay_shares_its_status(self):
        states, versions = self.states()
        assert (states["a-26"].status, states["a-26"].is_current) == ("current", True)
        assert (states["a-26A"].status, states["a-26A"].is_current) == ("current", True)
        assert states["a-26A"].supersede_kind is None and states["a-26"].superseded_by is None

    def test_each_filing_owns_only_the_sections_it_is_the_effective_source_of(self):
        states, _ = self.states()
        assert states["a-26"].owned_sections == frozenset({"I.1", "I.1A"})
        assert states["a-26A"].owned_sections == frozenset({"II.7"})
        assert states["a-25"].owned_sections == frozenset(ANNUAL_SECTIONS)

    def test_corrected_sections_are_present_but_not_owned(self):
        states, _ = self.states()
        assert states["a-26"].corrected_sections == ("II.7",)
        assert states["a-26A"].corrected_sections == () and states["a-25"].corrected_sections == ()
        assert states["q-aug"].corrected_sections == ()

    def test_the_overlay_amends_its_original_with_the_sections_it_restates(self):
        states, _ = self.states()
        assert states["a-26A"].amends == "a-26" and states["a-26"].amends is None
        assert loaders.build_amends_rows(states) == [{"newer": "a-26A", "older": "a-26", "sections": ["II.7"]}]

    def test_no_amends_or_corrected_sections_without_a_parsed_amendment(self):
        states, _ = states_for()
        assert loaders.build_amends_rows(states) == []
        assert all(s.corrected_sections == () for s in states.values())

    def test_a_full_amendment_still_retires_the_original_whole(self):
        states, _ = states_for(sections=FULL_AMENDMENT)
        assert states["a-26"].status == "corrected" and states["a-26"].owned_sections == frozenset()
        assert states["a-26"].corrected_sections == ("I.1", "I.1A", "II.7")
        assert states["a-26A"].owned_sections == frozenset(ANNUAL_SECTIONS)
        assert loaders.build_amends_rows(states) == []  # a replacement is SUPERSEDES, not AMENDS

    def test_current_period_span_flags_per_section(self):
        states, _ = self.states()
        assert span_flags(states, "a-26", "I.1A") == ("current", True, True, OPEN_END)
        assert span_flags(states, "a-26", "I.1") == ("current", True, True, OPEN_END)
        assert span_flags(states, "a-26A", "II.7") == ("current", True, True, OPEN_END)

    def test_the_originals_restated_section_is_neither_current_nor_retrievable(self):
        states, _ = self.states()
        # valid_to = the amending filing's date (same day for AMD: an empty interval)
        assert span_flags(states, "a-26", "II.7") == ("corrected", False, False, "2026-02-04")

    def test_restated_section_ends_on_the_amendments_date_when_it_came_later(self):
        manifest = [m(r["accession_no"], r["form"], "2026-03-20" if r["accession_no"] == "a-26A" else r["filing_date"])
                    for r in AMD_MANIFEST]
        states, _ = self.states(manifest)
        assert span_flags(states, "a-26", "II.7") == ("corrected", False, False, "2026-03-20")

    def test_a_superseded_period_keeps_its_owned_sections_retrievable_but_not_the_corrected_one(self):
        manifest = AMD_MANIFEST + [m("a-27", "10-K", "2027-02-03")]
        sections = {**MDNA_AMENDMENT, "a-27": ANNUAL_SECTIONS}
        states, _ = states_for(manifest, sections)
        assert span_flags(states, "a-26", "I.1A") == ("superseded", False, True, "2027-02-03")
        assert span_flags(states, "a-26A", "II.7") == ("superseded", False, True, "2027-02-03")
        assert span_flags(states, "a-26", "II.7") == ("corrected", False, False, "2026-02-04")
        assert states["a-26A"].superseded_by == "a-27" and states["a-26"].superseded_by == "a-27"

    def test_fully_corrected_original_has_no_owned_or_retrievable_span(self):
        states, _ = states_for(sections=FULL_AMENDMENT)
        assert span_flags(states, "a-26", "I.1A") == ("corrected", False, False, "2026-02-04")

    def test_quarterly_and_annual_flags_without_amendments_are_unchanged(self):
        states, _ = states_for()
        assert span_flags(states, "q-may", "II.1A") == ("superseded", False, False, "2026-08-05")
        assert span_flags(states, "a-25", "II.7") == ("superseded", False, True, "2026-02-04")
        assert span_flags(states, "q-aug", "II.1A") == ("current", True, True, OPEN_END)

    def test_unknown_sections_fall_back_to_filing_level_flags(self):
        manifest = [r for r in AMD_MANIFEST if r["accession_no"] != "a-26A"]
        versions = loaders.versions_for_ticker("AMD", manifest, None)
        states = loaders.derive_filing_states(manifest, versions)  # sections not supplied
        assert states["a-26"].owned_sections is None
        assert span_flags(states, "a-26", "II.7") == ("current", True, True, OPEN_END)

    def test_risks_follow_the_flags_of_their_evidence_section(self):
        states, _ = self.states()
        restated = loaders.risk_stamp(states["a-26"], 2488, "II.7")
        assert (restated["is_current"], restated["valid_to"], restated["form"]) == (False, "2026-02-04", "10-K")
        owned = loaders.risk_stamp(states["a-26"], 2488, "I.1A")
        assert (owned["is_current"], owned["valid_to"]) == (True, OPEN_END)
        overlay = loaders.risk_stamp(states["a-26A"], 2488, "II.7")
        assert (overlay["is_current"], overlay["form"]) == (True, "10-K/A")

    def test_filing_rows_carry_the_corrected_sections(self):
        states, _ = self.states()
        rows = {r["accession_no"]: r for r in loaders.build_filing_rows(AMD_MANIFEST, states)}
        assert rows["a-26"]["corrected_sections"] == ["II.7"]
        assert rows["a-26A"]["corrected_sections"] == [] and rows["q-aug"]["corrected_sections"] == []

    def test_supersedes_edges_still_cover_the_rolled_period_of_the_overlay(self):
        manifest = AMD_MANIFEST + [m("a-27", "10-K", "2027-02-03")]
        states, _ = states_for(manifest, {**MDNA_AMENDMENT, "a-27": ANNUAL_SECTIONS})
        edges = {(e["newer"], e["older"], e["kind"]) for e in loaders.build_supersedes_rows(states)}
        assert {("a-27", "a-26", "rolled"), ("a-27", "a-26A", "rolled")} <= edges


class TestCurrentFilingsWithoutChunks:
    def test_lists_current_filings_that_have_no_chunks(self):
        manifest = AMD_MANIFEST + [m("q-nov", "10-Q", "2026-11-05")]
        states, _ = states_for(manifest)
        assert loaders.current_filings_without_chunks(states, set(AMD_SECTIONS)) == ["q-nov"]

    def test_nothing_to_report_when_every_current_filing_has_chunks(self):
        states, _ = states_for()
        assert loaders.current_filings_without_chunks(states, set(AMD_SECTIONS)) == []

    def test_inert_and_superseded_filings_are_not_reported(self):
        states, _ = states_for()
        assert loaders.current_filings_without_chunks(states, set()) == ["a-26", "q-aug"]


# -------------------------------------------------------------------- scope

class TestSpanScope:
    def test_nvda_keeps_every_chunk(self):
        ch = frame(("n-23", "10-K", "2023-02-24"), ("n-24", "10-K", "2024-02-21"), ("n-26", "10-K", "2026-02-25"))
        assert len(loaders._span_scope(ch, "NVDA")) == len(ch)

    def test_latest_annual_in_full_and_history_risk_sections_only(self):
        ch = frame(("a-24", "10-K", "2024-01-31"), ("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"))
        scope = loaders._span_scope(ch, "AMD", hist_annuals=1)
        assert set(scope[scope["accession_no"] == "a-26"]["section_id"]) == {"I.1", "I.1A"}
        assert set(scope[scope["accession_no"] == "a-25"]["section_id"]) == {"I.1A"}
        assert "a-24" not in accessions(scope)

    def test_latest_quarterly_is_chosen_by_filing_date_not_accession_string(self):
        older_but_larger = "0001564590-24-000500"
        newer_but_smaller = "0000950170-24-000100"
        ch = frame(("k-23", "10-K", "2023-07-27"), (older_but_larger, "10-Q", "2024-01-25"),
                   (newer_but_smaller, "10-Q", "2024-04-25"), ticker="MSFT")
        scope = loaders._span_scope(ch, "MSFT")
        assert newer_but_smaller in accessions(scope) and older_but_larger not in accessions(scope)

    def test_a_new_annual_leaves_no_current_quarterly_in_scope(self):
        # MSFT FY26: the 10-K (07-29) is newer than the last 10-Q (04-29), which is retired
        ch = frame(("k-25", "10-K", "2025-07-30"), ("q-apr", "10-Q", "2026-04-29"), ("k-26", "10-K", "2026-07-29"),
                   ticker="MSFT")
        scope = loaders._span_scope(ch, "MSFT")
        assert accessions(scope) == {"k-25", "k-26"}

    def test_a_parsed_amendment_is_the_latest_annual(self):
        ch = frame(("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"), ("a-26A", "10-K/A", "2026-02-04"))
        scope = loaders._span_scope(ch, "AMD", hist_annuals=1)
        assert accessions(scope) == {"a-25", "a-26A"}

    def test_an_mdna_only_amendment_overlays_the_original_it_does_not_replace_it(self):
        ch = pd.concat([frame(("a-25", "10-K", "2025-02-05"), sections=("I.1", "I.1A", "II.7")),
                        frame(("a-26", "10-K", "2026-02-04"), sections=("I.1", "I.1A", "II.7")),
                        frame(("a-26A", "10-K/A", "2026-02-04"), sections=("II.7",))], ignore_index=True)
        scope = loaders._span_scope(ch, "AMD", hist_annuals=1)
        by_acc = {a: set(g["section_id"]) for a, g in scope.groupby("accession_no")}
        assert by_acc["a-26"] == {"I.1", "I.1A"}          # the original's restated Item 7 is left out
        assert by_acc["a-26A"] == {"II.7"}                 # ... it comes from the amendment
        assert by_acc["a-25"] == {"I.1A"}                  # history: risk factors only

    def test_history_takes_only_the_sections_a_period_owns(self):
        ch = pd.concat([frame(("a-25", "10-K", "2025-02-05"), sections=("I.1A", "II.7")),
                        frame(("a-25A", "10-K/A", "2025-06-01"), sections=("I.1A",)),
                        frame(("a-26", "10-K", "2026-02-04"), sections=("I.1A",))], ignore_index=True)
        scope = loaders._span_scope(ch, "AMD", hist_annuals=1)
        by_acc = {a: set(g["section_id"]) for a, g in scope.groupby("accession_no")}
        assert "a-25" not in by_acc      # the amendment now owns I.1A: history reads it from there
        assert by_acc["a-25A"] == {"I.1A"} and by_acc["a-26"] == {"I.1A"}

    def test_explicit_versions_decide_the_scope(self):
        ch = frame(("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"), ("a-26A", "10-K/A", "2026-02-04"))
        # a-26A exists in the manifest but was not parsed -> it must not replace a-26
        _, versions = states_for(sections={k: AMD_SECTIONS[k] for k in ("a-25", "a-26")})
        scope = loaders._span_scope(ch, "AMD", hist_annuals=1, versions=versions)
        assert "a-26" in accessions(scope)

    def test_row_order_of_the_parquet_is_preserved(self):
        ch = frame(("a-26", "10-K", "2026-02-04"), ("a-25", "10-K", "2025-02-05"))
        scope = loaders._span_scope(ch, "AMD")
        assert scope["chunk_id"].tolist() == [c for c in ch["chunk_id"] if c in set(scope["chunk_id"])]

    def test_empty_or_formless_frames_give_an_empty_scope(self):
        assert loaders._span_scope(pd.DataFrame(), "AMD").empty
        assert loaders._span_scope(pd.DataFrame({"chunk_id": ["x"]}), "AMD").empty


class TestSelectSpanChunks:
    def ch(self):
        return frame(("a-24", "10-K", "2024-01-31"), ("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"),
                     ("q-may", "10-Q", "2026-05-06"), ("q-aug", "10-Q", "2026-08-05"))

    def test_history_with_an_extraction_record_is_never_dropped(self):
        ch = self.ch()
        extracted = {"q-may:I.1A:0000", "a-24:I.1:0001", "a-24:I.1A:0000"}
        picked = set(loaders.select_span_chunks(ch, "AMD", extracted)["chunk_id"])
        assert extracted <= picked  # out-of-scope quarterly + old annual chunks survive

    def test_current_scope_is_loaded_even_without_extraction_records(self):
        picked = loaders.select_span_chunks(self.ch(), "AMD", set())
        assert {"a-26", "q-aug"} <= accessions(picked)

    def test_out_of_scope_chunks_without_extraction_are_left_out(self):
        picked = loaders.select_span_chunks(self.ch(), "AMD", set())
        assert "q-may" not in accessions(picked)
        assert "a-24" not in accessions(picked)

    def test_extraction_ids_that_are_not_chunks_are_ignored(self):
        picked = loaders.select_span_chunks(self.ch(), "AMD", {"ghost:I.1:0000"})
        assert "ghost:I.1:0000" not in set(picked["chunk_id"])

    def test_nvda_still_loads_every_chunk(self):
        ch = frame(("n-23", "10-K", "2023-02-24"), ("n-26", "10-K", "2026-02-25"), ("n-q", "10-Q", "2026-05-20"))
        assert len(loaders.select_span_chunks(ch, "NVDA", set())) == len(ch)

    def test_result_is_in_parquet_order_without_duplicates(self):
        ch = self.ch()
        picked = loaders.select_span_chunks(ch, "AMD", set(ch["chunk_id"]))
        assert picked["chunk_id"].tolist() == ch["chunk_id"].tolist()


# ---------------------------------------------------------------- span rows

class TestBuildSpanRows:
    def rows(self, mentioned=lambda t: [1]):
        states, _ = states_for()
        ch = frame(("a-25", "10-K", "2025-02-05"), ("q-may", "10-Q", "2026-05-06"), ("a-26", "10-K", "2026-02-04"), n=1)
        vecs = np.arange(len(ch) * 3, dtype=float).reshape(len(ch), 3)
        return {r["chunk_id"]: r for r in loaders.build_span_rows(ch, vecs, states, mentioned)}

    def test_every_contract_property_is_present(self):
        row = self.rows()["a-26:I.1A:0000"]
        assert row["content_hash"] == content_hash(row["text"])
        assert row["filer_cik"] == 2488 and isinstance(row["filer_cik"], int)
        assert (row["form"], row["accession_no"], row["section_id"]) == ("10-K", "a-26", "I.1A")
        assert row["filing_date"] == row["valid_from"] == "2026-02-04"
        assert (row["valid_to"], row["is_current"], row["retrievable"], row["status"]) == (OPEN_END, True, True, "current")
        assert row["source_type"] == "sec_filing"
        assert row["section_key"] == "a-26:I.1A" and row["mentions"] == [1]

    def test_historical_annual_span_is_retrievable_but_not_current(self):
        row = self.rows()["a-25:I.1A:0000"]
        assert (row["is_current"], row["retrievable"], row["status"], row["valid_to"]) == (
            False, True, "superseded", "2026-02-04")

    def test_superseded_quarterly_span_is_neither_current_nor_retrievable(self):
        row = self.rows()["q-may:I.1A:0000"]
        assert (row["is_current"], row["retrievable"], row["valid_to"]) == (False, False, "2026-08-05")

    def test_embedding_is_a_plain_list_of_floats_aligned_to_the_chunk(self):
        rows = self.rows()
        assert rows["a-25:I.1:0000"]["embedding"] == [0.0, 1.0, 2.0]
        assert type(rows["a-25:I.1:0000"]["embedding"][0]) is float

    def test_existing_span_props_are_kept(self):
        row = self.rows()["a-26:I.1:0000"]
        assert row["kind"] == "prose" and row["n_tokens"] == 12 and row["char_end"] == 10
        assert row["source_url"] == "https://sec.example/a-26"

    def test_content_hash_ignores_whitespace_differences_between_chunkings(self):
        states, _ = states_for()
        ch = frame(("a-26", "10-K", "2026-02-04"), n=1)
        ch.loc[0, "text"] = "Export   controls\n apply"
        (row, *_) = loaders.build_span_rows(ch, np.zeros((len(ch), 3)), states, lambda t: [])
        assert row["content_hash"] == content_hash("Export controls apply")

    def test_chunk_of_a_filing_missing_from_the_manifest_is_an_explicit_error(self):
        states, _ = states_for()
        ch = frame(("ghost", "10-K", "2026-02-04"), n=1)
        with pytest.raises(ValueError, match="ghost"):
            loaders.build_span_rows(ch, np.zeros((len(ch), 3)), states, lambda t: [])


class TestRiskStamp:
    def test_risk_inherits_its_filings_validity(self):
        states, _ = states_for()
        current = loaders.risk_stamp(states["a-26"], 2488, "I.1A")
        assert current == {"filer_cik": 2488, "form": "10-K", "filing_date": "2026-02-04",
                           "valid_from": "2026-02-04", "valid_to": OPEN_END, "is_current": True}
        old = loaders.risk_stamp(states["q-may"], 2488, "II.1A")
        assert (old["is_current"], old["valid_to"], old["form"]) == (False, "2026-08-05", "10-Q")


# ---------------------------------------------------------- export controls

def rule(doc, title="Rule", published="2026-01-15", kind="advanced_computing", relevant=True, topics=()):
    return {"document_number": doc, "title": title, "publication_date": published,
            "html_url": f"https://fr.example/{doc}", "abstract": "abstract",
            "kind": kind, "topics": list(topics), "relevant": relevant}


class TestNormalizeRule:
    def test_new_style_rule_passes_through(self):
        r = rule("2026-1", kind="licensing_policy", relevant=True, topics=["license review"])
        assert loaders.normalize_rule(r) == r

    def test_legacy_rule_without_classification_falls_back(self):
        legacy = {"document_number": "2024-9", "title": "Some rule", "publication_date": "2024-01-01"}
        out = loaders.normalize_rule(legacy)
        assert (out["kind"], out["relevant"], out["topics"]) == ("legacy", True, [])
        assert out["document_number"] == "2024-9"

    def test_irrelevant_flag_is_respected(self):
        assert loaders.normalize_rule(rule("x", kind="other", relevant=False))["relevant"] is False

    def test_kind_without_relevant_flag_is_relevant_only_for_known_kinds(self):
        no_flag = {"document_number": "1", "title": "t", "publication_date": "2026-01-01", "kind": "advanced_computing"}
        assert loaders.normalize_rule(no_flag)["relevant"] is True
        assert loaders.normalize_rule({**no_flag, "kind": "other"})["relevant"] is False

    def test_input_is_not_mutated(self):
        legacy = {"document_number": "2024-9", "title": "t", "publication_date": "2024-01-01"}
        loaders.normalize_rule(legacy)
        assert "kind" not in legacy


class TestRuleMatching:
    @pytest.mark.parametrize("kind,text", [
        ("advanced_computing", "Restrictions on advanced computing items"),
        ("advanced_computing", "sales of our AI chip products"),
        ("advanced_computing", "GPUs and other accelerators"),
        ("advanced_computing", "the H200 and H20 were affected"),
        ("advanced_computing", "A100 and H100 products"),
        ("semiconductor_equipment", "semiconductor manufacturing equipment"),
        ("semiconductor_equipment", "advanced lithography tools"),
        ("affiliates_rule", "the Entity List rule"),
        ("affiliates_rule", "our affiliates in China"),
        ("licensing_policy", "we must obtain a license"),
        ("licensing_policy", "export controls may change"),
        ("ai_model_controls", "the AI diffusion framework"),
        ("ai_model_controls", "model weights are controlled"),
    ])
    def test_kind_keywords_match_their_evidence(self, kind, text):
        assert loaders.rule_matches_evidence(rule("r", kind=kind), text)

    @pytest.mark.parametrize("kind,text", [
        ("advanced_computing", "quarterly dividend policy"),
        ("semiconductor_equipment", "advanced computing only"),
        ("ai_model_controls", "a plain license agreement"),
        ("advanced_computing", "product A1000 launches"),  # 'a100' must not match inside a longer token
    ])
    def test_wrong_topic_evidence_does_not_match(self, kind, text):
        assert not loaders.rule_matches_evidence(rule("r", kind=kind), text)

    def test_matching_is_case_insensitive(self):
        assert loaders.rule_matches_evidence(rule("r", kind="advanced_computing"), "ADVANCED COMPUTING ITEMS")

    def test_kinds_without_evidence_keywords_never_match(self):
        for kind in ("entity_list_additions", "other", "surprise"):
            assert not loaders.rule_matches_evidence(rule("r", kind=kind), "entity list advanced computing license")

    def test_legacy_rules_match_on_title_topic_and_evidence_keywords(self):
        legacy = loaders.normalize_rule({"document_number": "1", "title": "Entity List additions",
                                         "publication_date": "2024-01-01"})
        assert loaders.rule_matches_evidence(legacy, "companies on the Entity List")
        assert not loaders.rule_matches_evidence(legacy, "advanced computing chips")  # wrong topic for this title

    def test_topic_keywords_stay_importable_with_the_notebook_values(self):
        assert loaders.TOPIC_KEYWORDS["advanced computing"] == ["advanced computing", "ai chip", "accelerator"]


class TestSelectAffectedRules:
    def chunks(self, *texts):
        return [{"chunk_id": f"c{i}", "text": t} for i, t in enumerate(texts)]

    def test_only_relevant_rules_are_linked(self):
        rules = [rule("in", relevant=True), rule("out", relevant=False)]
        picked = loaders.select_affected_rules(self.chunks("advanced computing items"), rules)
        assert [p["rule_id"] for p in picked] == ["in"]

    def test_evidence_must_match_the_rule_kind(self):
        rules = [rule("adv", kind="advanced_computing"), rule("eq", kind="semiconductor_equipment")]
        picked = loaders.select_affected_rules(self.chunks("we discuss advanced computing"), rules)
        assert [p["rule_id"] for p in picked] == ["adv"]

    def test_edge_carries_only_the_chunks_that_matched(self):
        picked = loaders.select_affected_rules(
            self.chunks("dividends", "advanced computing chips", "weather", "our GPU line"),
            [rule("adv")])
        assert picked[0]["chunks"] == ["c1", "c3"]
        assert picked[0]["date"] == "2026-01-15"

    def test_evidence_chunk_ids_are_capped(self):
        many = self.chunks(*["advanced computing"] * 25)
        assert len(loaders.select_affected_rules(many, [rule("adv")])[0]["chunks"]) == loaders.MAX_EDGE_EVIDENCE_CHUNKS

    def test_cap_keeps_the_most_recent_rules_with_a_stable_tiebreak(self):
        rules = [rule(f"d{i:02d}", published=f"2025-{(i % 12) + 1:02d}-01") for i in range(20)]
        picked = loaders.select_affected_rules(self.chunks("advanced computing"), rules)
        assert len(picked) == loaders.MAX_AFFECTED_BY_PER_COMPANY == 15
        newest_20 = sorted(rules, key=lambda r: (r["publication_date"], r["document_number"]), reverse=True)[:15]
        assert [p["rule_id"] for p in picked] == [r["document_number"] for r in newest_20]

    def test_no_evidence_means_no_edges(self):
        assert loaders.select_affected_rules([], [rule("adv")]) == []

    def test_build_rows_covers_every_exposed_company(self):
        exposures = {1: self.chunks("advanced computing"), 2: self.chunks("nothing relevant")}
        rows = loaders.build_affected_by_rows(exposures, [rule("adv")])
        assert [(r["cik"], r["rule_id"]) for r in rows] == [(1, "adv")]


# ----------------------------------------------------------------- snapshot

class TestSnapshotProps:
    def test_props_with_date_and_counts(self):
        p = loaders.snapshot_props("snap-1", date(2026, 9, 25), {"b": 2, "a": 1}, code_version="0.2.0")
        assert p == {"id": "snap-1", "as_of": "2026-09-25", "code_version": "0.2.0",
                     "counts": '{"a": 1, "b": 2}'}

    def test_as_of_may_be_a_string_or_absent(self):
        assert loaders.snapshot_props("s", "2026-09-25", {})["as_of"] == "2026-09-25"
        assert loaders.snapshot_props("s", None, {})["as_of"] is None

    def test_bad_as_of_is_an_explicit_error(self):
        with pytest.raises(ValueError):
            loaders.snapshot_props("s", "not-a-date", {})

    def test_stamp_snapshot_writes_one_merge_and_returns_the_id(self):
        drv = RecordingDriver()
        assert loaders.stamp_snapshot(drv, "snap-1", date(2026, 9, 25), {"spans": 3}) == "snap-1"
        (query, params), = drv.calls
        assert "MERGE (s:Snapshot {id: $id})" in query and "ON CREATE SET s.created_at = datetime()" in query
        assert params["as_of"] == "2026-09-25" and json.loads(params["counts"]) == {"spans": 3}


class TestResolveSnapshotId:
    def test_explicit_id_wins(self):
        assert loaders._resolve_snapshot_id(Settings(_env_file=None), "snap-x") == "snap-x"

    def test_missing_id_is_computed_from_the_data_lake(self, monkeypatch):
        monkeypatch.setattr(loaders, "compute_snapshot_id", lambda settings: "snap-computed")
        assert loaders._resolve_snapshot_id(Settings(_env_file=None), None) == "snap-computed"


# ------------------------------------------------------- driver-level glue

class Rows(list):
    """A neo4j Result stand-in: iterable rows plus ``single()``."""

    def single(self):
        return self[0] if self else None


class RecordingDriver:
    """Fake driver: records queries + params; rows come from ``responder``."""

    def __init__(self, responder=lambda q: []):
        self.calls, self.responder = [], responder

    def session(self, **config):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, parameters=None, **params):
        q = " ".join(query.split())
        self.calls.append((q, params))
        return Rows(dict(r) for r in self.responder(q))


class TestRunBatched:
    def test_rows_are_sent_in_batches_with_shared_parameters(self):
        drv = RecordingDriver()
        loaders._run_batched(drv, "UNWIND $rows AS row RETURN row", [{"i": i} for i in range(250)],
                             snapshot_id="snap-1")
        assert [len(p["rows"]) for _, p in drv.calls] == [100, 100, 50]
        assert all(p["snapshot_id"] == "snap-1" for _, p in drv.calls)

    def test_no_rows_means_no_queries(self):
        drv = RecordingDriver()
        loaders._run_batched(drv, "UNWIND $rows AS row RETURN row", [])
        assert drv.calls == []


def write_lake(tmp_path, manifest, chunk_frames=None):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    (settings.raw_dir / "edgar").mkdir(parents=True)
    (settings.raw_dir / "edgar" / "manifest_universe.json").write_text(json.dumps({"AMD": manifest}), encoding="utf-8")
    settings.chunks_dir.mkdir(parents=True)
    if chunk_frames is not None:
        chunk_frames.to_parquet(settings.chunks_dir / "AMD_chunks.parquet", index=False)
    (settings.interim_dir / "section_texts").mkdir(parents=True)
    st = pd.DataFrame([{"accession_no": r["accession_no"], "section_id": "I.1A", "section_title": "t", "n_chars": 5}
                       for r in manifest])
    st.to_parquet(settings.interim_dir / "section_texts" / "AMD_section_texts.parquet", index=False)
    return settings


class TestLoadFilingsAndSections:
    def load(self, tmp_path, parsed_frame):
        settings = write_lake(tmp_path, AMD_MANIFEST, parsed_frame)
        drv = RecordingDriver()
        counts = loaders.load_filings_and_sections(drv, settings, ["AMD"], snapshot_id="snap-1")
        return drv, counts

    def test_filings_are_stamped_with_versions_and_the_snapshot(self, tmp_path):
        parsed = frame(*[(a, "10-K" if a.startswith("a") else "10-Q", d) for a, d in
                         (("a-24", "2024-01-31"), ("a-25", "2025-02-05"), ("a-26", "2026-02-04"),
                          ("q-may", "2026-05-06"), ("q-aug", "2026-08-05"))])
        drv, counts = self.load(tmp_path, parsed)
        assert counts == (6, 6)
        filing_call = next((q, p) for q, p in drv.calls if "MERGE (f:Filing" in q)
        query, params = filing_call
        assert "f.status = row.status" in query and "f.is_current = row.is_current" in query
        assert params["snapshot_id"] == "snap-1"
        by_acc = {r["accession_no"]: r for r in params["rows"]}
        assert by_acc["a-26A"]["status"] == "amendment"  # its content was never chunked
        assert by_acc["a-26"]["status"] == "current" and by_acc["q-may"]["superseded_by"] == "q-aug"

    def test_stale_supersedes_edges_are_deleted_before_being_recreated(self, tmp_path):
        parsed = frame(("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"))
        drv, _ = self.load(tmp_path, parsed)
        queries = [q for q, _ in drv.calls]
        delete_at = next(i for i, q in enumerate(queries) if "SUPERSEDES" in q and "DELETE" in q)
        create_at = next(i for i, q in enumerate(queries) if "MERGE (newer)-[s:SUPERSEDES]->(older)" in q)
        assert delete_at < create_at
        create = next(p for q, p in drv.calls if "MERGE (newer)-[s:SUPERSEDES]->(older)" in q)
        assert {"newer": "a-26", "older": "a-25", "kind": "rolled"} in create["rows"]

    def test_missing_chunk_parquet_means_no_amendment_is_treated_as_parsed(self, tmp_path):
        drv, _ = self.load(tmp_path, None)
        rows = next(p for q, p in drv.calls if "MERGE (f:Filing" in q)["rows"]
        assert {r["accession_no"]: r["status"] for r in rows}["a-26A"] == "amendment"


# ----------------------------------------- loaders end to end (fake driver)

class ZeroEmbedder:
    def encode_chunks_cached(self, chunks_df, cache_path, batch_size=8):
        return np.zeros((len(chunks_df), 4))

    def encode_passages(self, texts, batch_size=8, show_progress=False):
        return np.zeros((len(texts), 4))


def overlay_chunks():
    """AMD as in the real lake: the 10-K owns I.1/I.1A, its 10-K/A restates only II.7."""
    return pd.concat([
        frame(("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"), sections=("I.1", "I.1A", "II.7"), n=1),
        frame(("a-26A", "10-K/A", "2026-02-04"), sections=("II.7",), n=1),
    ], ignore_index=True)


def write_extractions(settings, records):
    settings.extractions_dir.mkdir(parents=True, exist_ok=True)
    (settings.extractions_dir / "amd_extractions.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8")


def extraction(chunk_id, summary):
    return {"chunk_id": chunk_id, "relations": [], "products": [],
            "risk_factors": [{"summary": summary, "category": "Financial", "evidence_quote": "q"}]}


def risk_id(chunk_id, summary):
    import hashlib
    return hashlib.sha1(f"{chunk_id}|{summary}".encode()).hexdigest()[:16]


class TestSectionAwareLoading:
    def test_partial_amendment_becomes_an_amends_edge_and_marks_the_originals_corrected_sections(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, overlay_chunks())
        drv = RecordingDriver()
        loaders.load_filings_and_sections(drv, settings, ["AMD"], snapshot_id="s1")
        filings = {r["accession_no"]: r for r in next(p for q, p in drv.calls if "MERGE (f:Filing" in q)["rows"]}
        assert (filings["a-26"]["status"], filings["a-26"]["corrected_sections"]) == ("current", ["II.7"])
        assert (filings["a-26A"]["status"], filings["a-26A"]["corrected_sections"]) == ("current", [])
        amends = next(p for q, p in drv.calls if "MERGE (newer)-[a:AMENDS]->(older)" in q)
        assert amends["rows"] == [{"newer": "a-26A", "older": "a-26", "sections": ["II.7"]}]
        queries = [q for q, _ in drv.calls]
        assert next(i for i, q in enumerate(queries) if "AMENDS" in q and "DELETE" in q) < queries.index(
            next(q for q in queries if "MERGE (newer)-[a:AMENDS]->(older)" in q))
        supersedes = next(p for q, p in drv.calls if "MERGE (newer)-[s:SUPERSEDES]->(older)" in q)
        assert {"newer": "a-26A", "older": "a-26", "kind": "corrected"} not in supersedes["rows"]  # an overlay is not a replacement

    def test_risks_are_stamped_per_evidence_section(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, overlay_chunks())
        write_extractions(settings, [extraction("a-26:I.1A:0000", "owned risk"),
                                     extraction("a-26:II.7:0000", "restated risk"),
                                     extraction("a-26A:II.7:0000", "amendment risk")])
        drv = RecordingDriver()
        loaders.load_knowledge(drv, settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1")
        stamp = next(p for q, p in drv.calls if "rf.valid_to = date(row.valid_to)" in q)
        by_id = {r["risk_id"]: r for r in stamp["rows"]}
        owned, restated, overlay = (by_id[risk_id(c, t)] for c, t in (
            ("a-26:I.1A:0000", "owned risk"), ("a-26:II.7:0000", "restated risk"), ("a-26A:II.7:0000", "amendment risk")))
        assert (owned["is_current"], owned["valid_to"], owned["form"]) == (True, OPEN_END, "10-K")
        assert (restated["is_current"], restated["valid_to"], restated["form"]) == (False, "2026-02-04", "10-K")
        assert (overlay["is_current"], overlay["form"]) == (True, "10-K/A")
        assert stamp["snapshot_id"] == "s1"

    def test_existing_risks_are_restamped_even_though_they_are_not_re_embedded(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, overlay_chunks())
        write_extractions(settings, [extraction("a-26:I.1A:0000", "owned risk")])
        rid = risk_id("a-26:I.1A:0000", "owned risk")
        drv = RecordingDriver(lambda q: [{"id": rid}] if q.startswith("MATCH (rf:RiskFactor) RETURN rf.risk_id") else [])
        totals = loaders.load_knowledge(drv, settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1")
        assert totals["new_risks"] == 0
        assert not any("MERGE (rf:RiskFactor" in q for q, _ in drv.calls)
        assert any("rf.is_current = row.is_current" in q for q, _ in drv.calls)

    def test_spans_of_a_restated_section_are_flagged_and_left_out_of_the_current_scope(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, overlay_chunks())
        write_extractions(settings, [extraction("a-26:II.7:0000", "restated risk")])  # extracted, so it IS loaded
        drv = RecordingDriver()
        loaders.load_evidence_spans(drv, settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1")
        rows = {r["chunk_id"]: r for r in next(p for q, p in drv.calls if "MERGE (e:EvidenceSpan" in q)["rows"]}
        assert rows["a-26:II.7:0000"]["status"] == "corrected"
        assert (rows["a-26:II.7:0000"]["is_current"], rows["a-26:II.7:0000"]["retrievable"]) == (False, False)
        assert rows["a-26A:II.7:0000"]["is_current"] and rows["a-26:I.1A:0000"]["is_current"]
        assert rows["a-25:I.1A:0000"]["retrievable"] and not rows["a-25:I.1A:0000"]["is_current"]


class TestPipelineGuards:
    def test_a_filer_with_an_empty_chunk_parquet_is_skipped_not_fatal(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, pd.DataFrame())
        drv = RecordingDriver()
        assert loaders.load_evidence_spans(drv, settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1") == 0
        assert not any("MERGE (e:EvidenceSpan" in q for q, _ in drv.calls)

    def test_a_missing_chunk_parquet_still_raises_for_the_span_loader(self, tmp_path):
        settings = write_lake(tmp_path, AMD_MANIFEST, None)
        with pytest.raises(FileNotFoundError):
            loaders.load_evidence_spans(RecordingDriver(), settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1")

    def test_current_filings_without_chunks_are_reported(self, tmp_path, caplog):
        chunks = frame(("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"))  # no 10-Q was chunked yet
        settings = write_lake(tmp_path, AMD_MANIFEST, chunks)
        with caplog.at_level("WARNING", logger="semigraph.graph.loaders"):
            loaders.load_evidence_spans(RecordingDriver(), settings, ZeroEmbedder(), ["AMD"], snapshot_id="s1")
        assert any("q-aug" in r.getMessage() and "no chunks" in r.getMessage() for r in caplog.records)

    def test_an_implicit_snapshot_id_is_warned_about(self, monkeypatch, caplog):
        monkeypatch.setattr(loaders, "compute_snapshot_id", lambda settings: "snap-implicit")
        with caplog.at_level("WARNING", logger="semigraph.graph.loaders"):
            assert loaders._resolve_snapshot_id(Settings(_env_file=None), None) == "snap-implicit"
        assert any("snap-implicit" in r.getMessage() for r in caplog.records)
        caplog.clear()
        with caplog.at_level("WARNING", logger="semigraph.graph.loaders"):
            loaders._resolve_snapshot_id(Settings(_env_file=None), "snap-explicit")
        assert not caplog.records
