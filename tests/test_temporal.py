"""Pure tests for the bitemporal closure core (notebook 13 semantics).

No Neo4j: cluster_lineages / compute_temporal_states / category_mapping
operate on plain arrays and DataFrames.
"""

import numpy as np
import pandas as pd
import pytest

from semigraph.graph.temporal import (
    CANONICAL_CATEGORIES,
    category_mapping,
    cluster_lineages,
    compute_temporal_states,
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
