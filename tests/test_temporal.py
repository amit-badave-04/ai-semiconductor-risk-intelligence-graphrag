"""graph/temporal.py after M1b step 4: category spelling fix, the Active/Historical status of risk disclosures, as-of queries.

The greedy embedding clustering of LLM risk summaries and its 'Deleted' closure are gone (they reported risks as dropped
although their text was still in the newer filing); what changed between two filings is read from the text-grounded item layer
(``graph/items.py``, ``graph/item_loader.py``). No Neo4j here: the Cypher is checked through a recording fake driver.
"""

import pytest

from semigraph.graph import temporal
from semigraph.graph.temporal import CANONICAL_CATEGORIES, category_mapping


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


class RecordingDriver:
    """Fake driver capturing the Cypher and parameters of every call."""

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


class TestTheLineageClosureIsGone:
    @pytest.mark.parametrize("name", ["cluster_lineages", "compute_temporal_states", "fetch_annual_risks",
                                      "write_temporal_states", "apply_closure", "SAME_RISK_SIM"])
    def test_the_retired_names_no_longer_exist(self, name):
        assert not hasattr(temporal, name)

    def test_no_query_of_the_module_can_write_the_status_deleted(self):
        assert "Deleted" not in temporal._STATUS_CYPHER and "Deleted" not in temporal._AS_OF_CYPHER
        assert {temporal.STATUS_ACTIVE, temporal.STATUS_HISTORICAL} == {"Active", "Historical"}


class TestApplyCurrentStatus:
    def test_active_means_the_risk_is_in_the_current_annual_filing_and_nothing_else(self):
        drv = RecordingDriver()
        temporal.apply_current_status(drv)
        query, params = drv.calls[0]
        assert "'Active'" in query and "'Historical'" in query and "Deleted" not in query
        assert "rf.is_current = true" in query and "f.is_current = true" in query and "f.form IN $annual" in query
        assert set(params["annual"]) == {"10-K", "10-K/A", "20-F", "20-F/A"} == set(temporal.ANNUAL_FORMS)

    def test_it_touches_every_disclosure_edge_so_no_stale_status_survives(self):
        drv = RecordingDriver()
        temporal.apply_current_status(drv)
        query, _ = drv.calls[0]
        assert "MATCH (:Company)-[d:DISCLOSES_RISK]->(rf:RiskFactor)" in query and "WHERE" not in query.split("SET")[0]

    def test_the_retired_closure_properties_are_removed(self):
        drv = RecordingDriver()
        temporal.apply_current_status(drv)
        query, _ = drv.calls[0]
        assert "REMOVE d.end_date" in query
        assert "rf.lineage_id" in query and "rf.first_seen" in query and "rf.last_seen" in query

    def test_it_returns_the_edge_counts_by_status(self):
        drv = RecordingDriver([{"status": "Active", "n": 5}, {"status": "Historical", "n": 9}])
        assert temporal.apply_current_status(drv) == {"Active": 5, "Historical": 9}


class TestRisksActiveAsOf:
    def test_the_annual_filing_current_at_the_date_is_the_latest_effective_one_filed_on_or_before_it(self):
        drv = RecordingDriver()
        temporal.risks_active_as_of(drv, "2025-06-01")
        query, params = drv.calls[0]
        assert params["asof"] == "2025-06-01" and set(params["forms"]) == set(temporal.ANNUAL_FORMS)
        assert set(params["statuses"]) == {"current", "superseded"} == set(temporal.EFFECTIVE_FILING_STATUSES)
        assert "f.filing_date <= date($asof)" in query and "ORDER BY f.filing_date DESC" in query and "head(collect(f))" in query

    def test_an_overlay_amendment_is_never_the_annual_itself_but_its_owned_sections_count(self):
        drv = RecordingDriver()
        temporal.risks_active_as_of(drv, "2026-06-01")
        query, _ = drv.calls[0]
        assert "NOT (f)-[:AMENDS]->(:Filing)" in query and "(amend:Filing)-[:AMENDS]->(annual)" in query
        assert "amend.filing_date <= date($asof)" in query and "e.status IN $statuses" in query

    def test_it_can_be_limited_to_one_ticker(self):
        drv = RecordingDriver()
        temporal.risks_active_as_of(drv, "2025-06-01", "NVDA")
        query, params = drv.calls[0]
        assert "c.ticker = $ticker" in query and params["ticker"] == "NVDA"
        temporal.risks_active_as_of(drv, "2025-06-01")
        assert "$ticker" not in drv.calls[1][0] and "ticker" not in drv.calls[1][1]

    def test_the_rows_are_the_risk_factors_of_that_filing(self):
        drv = RecordingDriver([{"company": "Nvidia", "accession_no": "a", "risk_id": "r1", "summary": "s"}])
        assert temporal.risks_active_as_of(drv, "2025-06-01") == [{"company": "Nvidia", "accession_no": "a", "risk_id": "r1", "summary": "s"}]
        assert "RETURN DISTINCT" in drv.calls[0][0] and "rf.risk_id AS risk_id" in drv.calls[0][0]
