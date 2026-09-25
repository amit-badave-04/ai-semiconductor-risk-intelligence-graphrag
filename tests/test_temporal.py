"""Pure tests for the bitemporal closure core (notebook 13 semantics).

No Neo4j: cluster_lineages / compute_temporal_states / category_mapping
operate on plain arrays and DataFrames.
"""

import re

import numpy as np
import pandas as pd
import pytest

from semigraph.graph.temporal import (
    ANNUAL_FORMS,
    CANONICAL_CATEGORIES,
    EFFECTIVE_FILING_STATUSES,
    category_mapping,
    cluster_lineages,
    compute_temporal_states,
    fetch_annual_risks,
)


def unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


# orthogonal unit vectors = clearly distinct risks; identical = same risk
E_SUPPLY = unit([1, 0, 0, 0])
E_EXPORT = unit([0, 1, 0, 0])
E_PANDEMIC = unit([0, 0, 1, 0])


def risk_row(risk_id, company, filing_date, accession, emb):
    return {
        "cik": 1045810 if company == "Nvidia" else 50863,
        "company": company, "risk_id": risk_id, "summary": risk_id,
        "category": "Supply Chain", "embedding": emb,
        "filing_date": filing_date, "accession_no": accession,
    }


class TestClusterLineages:
    def test_identical_embeddings_form_one_lineage(self):
        embs = np.vstack([E_SUPPLY, E_SUPPLY, E_EXPORT])
        assert cluster_lineages(embs) == [[0, 1], [2]]

    def test_orthogonal_embeddings_stay_separate(self):
        embs = np.vstack([E_SUPPLY, E_EXPORT, E_PANDEMIC])
        assert cluster_lineages(embs) == [[0], [1], [2]]

    def test_threshold_boundary_joins_at_exact_similarity(self):
        # cos = 0.75 exactly -> joins (notebook uses >=)
        a = unit([1, 0])
        b = np.array([0.75, np.sqrt(1 - 0.75**2)])
        assert cluster_lineages(np.vstack([a, b])) == [[0, 1]]


class TestComputeTemporalStates:
    def test_omitted_risk_closed_with_latest_filing_date(self):
        # pandemic risk only in the 2023 10-K; supply risk recurs
        risks = pd.DataFrame([
            risk_row("r_supply_23", "Nvidia", "2023-02-24", "acc-23", E_SUPPLY),
            risk_row("r_pandemic_23", "Nvidia", "2023-02-24", "acc-23", E_PANDEMIC),
            risk_row("r_supply_26", "Nvidia", "2026-02-25", "acc-26", E_SUPPLY),
        ])
        updates, stats = compute_temporal_states(risks)
        by_id = updates.set_index("risk_id")
        assert by_id.loc["r_pandemic_23", "status"] == "Deleted"
        assert by_id.loc["r_pandemic_23", "end_date"] == "2026-02-25"
        assert stats[0]["closed_lineages"] == 1

    def test_recurring_risk_backdated_and_active(self):
        risks = pd.DataFrame([
            risk_row("r_supply_23", "Nvidia", "2023-02-24", "acc-23", E_SUPPLY),
            risk_row("r_supply_26", "Nvidia", "2026-02-25", "acc-26", E_SUPPLY),
        ])
        updates, _ = compute_temporal_states(risks)
        assert set(updates["status"]) == {"Active"}
        assert set(updates["lineage_id"]) == {updates["lineage_id"].iloc[0]}
        # both members carry the lineage's first disclosure date
        assert set(updates["first_seen"]) == {"2023-02-24"}
        assert set(updates["last_seen"]) == {"2026-02-25"}
        assert updates["end_date"].isna().all()

    def test_single_annual_filing_never_closes(self):
        risks = pd.DataFrame([
            risk_row("r1", "Intel", "2024-01-26", "acc-24", E_SUPPLY),
            risk_row("r2", "Intel", "2024-01-26", "acc-24", E_EXPORT),
        ])
        updates, stats = compute_temporal_states(risks)
        assert set(updates["status"]) == {"Active"}
        assert stats[0]["closed_lineages"] == 0

    def test_every_risk_node_gets_a_state(self):
        risks = pd.DataFrame([
            risk_row("a", "Nvidia", "2023-02-24", "acc-23", E_SUPPLY),
            risk_row("b", "Nvidia", "2026-02-25", "acc-26", E_EXPORT),
            risk_row("c", "Intel", "2024-01-26", "acc-24", E_PANDEMIC),
        ])
        updates, _ = compute_temporal_states(risks)
        assert len(updates) == len(risks)  # M5 assertion from notebook 13

    def test_companies_partitioned_independently(self):
        # same embedding at two companies must NOT share a lineage
        risks = pd.DataFrame([
            risk_row("nv", "Nvidia", "2023-02-24", "acc-23", E_SUPPLY),
            risk_row("in", "Intel", "2024-01-26", "acc-24", E_SUPPLY),
        ])
        updates, stats = compute_temporal_states(risks)
        assert updates["lineage_id"].nunique() == 2
        assert len(stats) == 2


class TestCategoryMapping:
    def test_case_variants_map_to_canonical(self):
        changes = category_mapping(["supply chain", "Supply Chain", "cybersecurity"])
        assert {c["old"]: c["new"] for c in changes} == {
            "supply chain": "Supply Chain", "cybersecurity": "Cybersecurity",
        }

    def test_unknown_categories_title_cased(self):
        changes = category_mapping(["intellectual property"])
        assert changes == [{"old": "intellectual property",
                            "new": "Intellectual Property"}]

    def test_canonical_spellings_untouched(self):
        assert category_mapping(list(CANONICAL_CATEGORIES)) == []


class TestLineageDeterminism:
    """Lineage ids must not depend on the row order the graph happens to return."""

    @staticmethod
    def frame():
        vecs = [E_SUPPLY, E_EXPORT, E_PANDEMIC]
        rows = []
        for year, acc in ((2023, "acc-23"), (2024, "acc-24"), (2025, "acc-25"), (2026, "acc-26")):
            for i, emb in enumerate(vecs):
                if year == 2026 and i == 2:
                    continue  # the pandemic risk is dropped in 2026
                # every risk of a filing shares its filing date: order among them is the tie-break
                rows.append(risk_row(f"r{i}_{year}", "Nvidia", f"{year}-02-25", acc, emb))
        return pd.DataFrame(rows)

    @staticmethod
    def states(updates):
        return updates.set_index("risk_id")[["lineage_id", "first_seen", "last_seen", "status", "end_date"]]

    @pytest.mark.parametrize("seed", range(8))
    def test_shuffling_the_input_rows_changes_nothing(self, seed):
        base = self.frame()
        expected, _ = compute_temporal_states(base)
        shuffled = base.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        actual, _ = compute_temporal_states(shuffled)
        pd.testing.assert_frame_equal(self.states(actual).sort_index(), self.states(expected).sort_index())

    def test_output_order_is_deterministic_too(self):
        base = self.frame()
        a, _ = compute_temporal_states(base)
        b, _ = compute_temporal_states(base.iloc[::-1].reset_index(drop=True))
        assert a["risk_id"].tolist() == b["risk_id"].tolist()

    def test_risks_with_the_same_date_are_ordered_by_risk_id(self):
        # both risks are first seen on the same day: lineage ids must follow risk_id order
        risks = pd.DataFrame([
            risk_row("zz", "Intel", "2024-01-26", "acc-24", E_EXPORT),
            risk_row("aa", "Intel", "2024-01-26", "acc-24", E_SUPPLY),
        ])
        updates, _ = compute_temporal_states(risks)
        by_id = updates.set_index("risk_id")["lineage_id"]
        assert by_id["aa"] == "50863:0" and by_id["zz"] == "50863:1"


class RecordingDriver:
    """Fake driver capturing the Cypher and parameters of run_cypher calls."""

    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or []

    def session(self, **config):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, parameters=None, **params):
        self.calls.append((" ".join(query.split()), params))
        return [dict(r) for r in self.rows]


class TestFetchAnnualRisks:
    def test_only_effective_annual_filings_of_every_annual_form_are_read(self):
        drv = RecordingDriver()
        fetch_annual_risks(drv)
        query, params = drv.calls[0]
        assert set(params["forms"]) == {"10-K", "10-K/A", "20-F"} == set(ANNUAL_FORMS)
        # corrected originals and inert amendments must not feed lineage clustering
        assert set(params["statuses"]) == {"current", "superseded"} == set(EFFECTIVE_FILING_STATUSES)
        assert "f.form IN $forms" in query and "f.status IN $statuses" in query

    def test_only_risks_whose_evidence_section_is_owned_feed_lineages(self):
        # a section restated by a later amendment is 'corrected' on the span: it must not feed a lineage
        drv = RecordingDriver()
        fetch_annual_risks(drv)
        query, params = drv.calls[0]
        assert "e.status IN $statuses" in query

    def test_risks_from_an_overlay_amendment_are_dated_by_the_filing_it_amends(self):
        # an Item-7-only 10-K/A filed months later must not advance the company's "latest annual"
        drv = RecordingDriver()
        fetch_annual_risks(drv)
        query, _ = drv.calls[0]
        assert "OPTIONAL MATCH (f)-[:AMENDS]->(base:Filing)" in query
        assert "coalesce(base.accession_no, f.accession_no) AS accession_no" in query
        assert "toString(coalesce(base.filing_date, f.filing_date)) AS filing_date" in query

    def test_rows_come_back_in_a_stable_order(self):
        drv = RecordingDriver()
        fetch_annual_risks(drv)
        query, _ = drv.calls[0]
        assert re.search(r"ORDER BY cik, filing_date, risk_id, accession_no\s*$", query)

    def test_no_rows_gives_an_empty_frame(self):
        assert fetch_annual_risks(RecordingDriver()).empty
