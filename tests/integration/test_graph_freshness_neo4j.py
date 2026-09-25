"""Graph freshness against a real Neo4j — opt-in (RUN_NEO4J_TESTS=1).

Loads a tiny synthetic corpus through the REAL loader functions into a scratch
database (``sgtest``; created and dropped by the fixtures, the ``neo4j``
database is never touched) and checks the version properties, SUPERSEDES
edges, filtered vector search, retrievability, AFFECTED_BY selection, schema
staleness detection and reset_graph.

Corpus
    AMD  a-25  10-K   2025-02-05  superseded annual (historical risk text)
         a-26  10-K   2026-02-04  current annual {I.1, I.1A, II.7}
         a-26x 10-K/A 2026-03-01  parsed, restates ONLY II.7 -> overlay (the real AMD shape)
         a-26y 10-K/A 2026-03-15  never segmented -> inert amendment
         q-may 10-Q   2026-05-06  superseded quarterly (2 chunks were extracted)
         q-aug 10-Q   2026-08-05  current quarterly
    AVGO b-26  10-K   2026-01-15  corrected original
         b-26A 10-K/A 2026-01-20  parsed amendment, the effective annual
         b-q1  10-Q   2026-03-10  current quarterly
"""

import json
import zlib
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from semigraph.graph import loaders, schema, temporal
from semigraph.hashing import content_hash

EMBED_DIM = 1024
AMD_CIK, AVGO_CIK, TSMC_CIK, MICRON_CIK = 2488, 1730168, 1046179, 723125
OPEN_END = date(9999, 12, 31)

AMD_MANIFEST = [
    ("a-25", "10-K", "2025-02-05"), ("a-26", "10-K", "2026-02-04"), ("a-26x", "10-K/A", "2026-03-01"),
    ("a-26y", "10-K/A", "2026-03-15"), ("q-may", "10-Q", "2026-05-06"), ("q-aug", "10-Q", "2026-08-05"),
]
AVGO_MANIFEST = [("b-26", "10-K", "2026-01-15"), ("b-26A", "10-K/A", "2026-01-20"), ("b-q1", "10-Q", "2026-03-10")]

# accession -> {section: chunk count}; a-26y is never segmented, so it has none
AMD_CHUNKS = {"a-25": {"I.1A": 4}, "a-26": {"I.1": 4, "I.1A": 4, "II.7": 3}, "a-26x": {"II.7": 2},
              "q-may": {"II.1A": 4}, "q-aug": {"II.1A": 6}}
AVGO_CHUNKS = {"b-26": {"I.1A": 2}, "b-26A": {"I.1": 2, "I.1A": 2}, "b-q1": {"II.1A": 3}}

TEXT_OVERRIDES = {
    "a-26:I.1A:0000": "Export controls restrict our sales of advanced computing products such as the H200 accelerator.",
    "a-25:I.1A:0000": "Restrictions on semiconductor manufacturing equipment and lithography could hurt our foundry partners.",
    "b-26:I.1A:0000": "Controls on lithography and semiconductor manufacturing equipment may affect our suppliers.",
    "b-26A:I.1A:0000": "We could be added to the Entity List and our affiliates could be restricted.",
}


def embedding_for(key: str) -> np.ndarray:
    """A deterministic random unit vector per chunk id / text."""
    rng = np.random.default_rng(zlib.crc32(key.encode()))
    v = rng.standard_normal(EMBED_DIM)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """Stand-in for Embedder: random unit vectors, never touches the real embedding caches."""

    def encode_chunks_cached(self, chunks_df, cache_path, batch_size=8):
        return np.vstack([embedding_for(cid) for cid in chunks_df["chunk_id"]])

    def encode_passages(self, texts, batch_size=8, show_progress=False):
        return np.vstack([embedding_for(t) for t in texts])


# ------------------------------------------------------------- lake builder

def chunk_frame(ticker, cik, manifest, layout):
    forms = {acc: (form, day) for acc, form, day in manifest}
    rows = []
    for acc, sections in layout.items():
        form, day = forms[acc]
        for section, n in sections.items():
            for k in range(n):
                cid = f"{acc}:{section}:{k:04d}"
                rows.append({
                    "chunk_id": cid, "ticker": ticker, "cik": cik, "form": form, "filing_date": day,
                    "accession_no": acc, "section_id": section, "section_title": section, "sub_heading": None,
                    "kind": "prose", "text": TEXT_OVERRIDES.get(cid, f"{acc} {section} filler text number {k}"),
                    "char_start": 0, "char_end": 50, "n_tokens": 12, "source_url": f"https://sec.example/{acc}",
                })
    return pd.DataFrame(rows)


def risk(summary, category, quote="quote"):
    return {"summary": summary, "category": category, "evidence_quote": quote}


def relation(src, rel, tgt):
    return {"source_entity": src, "relation": rel, "target_entity": tgt, "evidence_quote": "q"}


def record(chunk_id, *, risks=(), relations=(), products=()):
    acc, section, _ = chunk_id.split(":")
    return {"chunk_id": chunk_id, "ticker": "X", "accession_no": acc, "section_id": section,
            "relations": list(relations), "risk_factors": list(risks), "products": list(products)}


AMD_RECORDS = [
    record("a-26:I.1A:0000", risks=[risk("Export controls limit advanced computing sales", "Export Controls")]),
    record("a-26:I.1:0000", relations=[relation("TSMC", "SUPPLIES_TO", "AMD")],
           products=[{"name": "MI300X", "type": "GPU"}]),
    record("a-26:II.7:0000", risks=[risk("Restated MD&A liquidity risk", "Financial")]),
    record("a-26x:II.7:0000", risks=[risk("Amended MD&A liquidity risk", "Financial")]),
    record("a-25:I.1A:0000", risks=[risk("Equipment restrictions hurt foundry partners", "Export Controls")]),
    record("q-may:II.1A:0000", risks=[risk("Supply constraints in Q1", "Supply Chain")],
           relations=[relation("Micron", "SUPPLIES_TO", "AMD")]),
    record("q-may:II.1A:0001"),
    record("q-aug:II.1A:0000", risks=[risk("Weak PC demand", "Demand")], relations=[relation("TSMC", "SUPPLIES_TO", "AMD")]),
]
AVGO_RECORDS = [
    record("b-26:I.1A:0000", risks=[risk("Equipment controls affect suppliers", "Export Controls")]),
    record("b-26A:I.1A:0000", risks=[risk("We may be added to the Entity List", "Regulatory / Export Controls")]),
    record("b-q1:II.1A:0000", risks=[risk("Customer concentration", "Supply Chain")]),
]

RULES = [
    {"document_number": "R1", "title": "Advanced computing rule", "publication_date": "2026-01-15",
     "html_url": "u1", "abstract": "a", "kind": "advanced_computing", "topics": ["advanced computing"], "relevant": True},
    {"document_number": "R2", "title": "Equipment rule", "publication_date": "2025-06-01",
     "html_url": "u2", "abstract": "a", "kind": "semiconductor_equipment", "topics": [], "relevant": True},
    {"document_number": "R3", "title": "Entity List additions", "publication_date": "2026-02-01",
     "html_url": "u3", "abstract": "a", "kind": "entity_list_additions", "topics": ["entity list"], "relevant": False},
    {"document_number": "R4", "title": "Irrelevant computing notice", "publication_date": "2026-03-01",
     "html_url": "u4", "abstract": "a", "kind": "advanced_computing", "topics": [], "relevant": False},
    {"document_number": "R5", "title": "Legacy advanced computing rule", "publication_date": "2024-05-05",
     "html_url": "u5", "abstract": "a"},
    {"document_number": "R6", "title": "Affiliates rule", "publication_date": "2025-09-29",
     "html_url": "u6", "abstract": "a", "kind": "affiliates_rule", "topics": ["affiliates rule"], "relevant": True},
]


def build_lake(root: Path, settings, *, overlay_parsed=True):
    """Write the synthetic data lake under ``root`` and return Settings pointing at it.
    ``overlay_parsed=False`` leaves AMD's 10-K/A a-26x unsegmented (no chunks)."""
    lake = settings.model_copy(update={"data_dir": root / "data"})
    (lake.raw_dir / "edgar").mkdir(parents=True)
    (lake.interim_dir / "section_texts").mkdir(parents=True)
    lake.chunks_dir.mkdir(parents=True)
    lake.extractions_dir.mkdir(parents=True)

    tickers = {"AMD": AMD_CIK, "AVGO": AVGO_CIK, "TSM": TSMC_CIK, "MU": MICRON_CIK}
    (lake.raw_dir / "edgar" / "company_tickers.json").write_text(json.dumps(
        {str(i): {"cik_str": cik, "ticker": t, "title": t} for i, (t, cik) in enumerate(tickers.items())}), encoding="utf-8")
    manifests = {"AMD": AMD_MANIFEST, "AVGO": AVGO_MANIFEST}
    (lake.raw_dir / "edgar" / "manifest_universe.json").write_text(json.dumps({
        t: [{"ticker": t, "cik": tickers[t], "accession_no": a, "form": f, "filing_date": d,
             "source_url": f"https://sec.example/{a}"} for a, f, d in rows]
        for t, rows in manifests.items()}), encoding="utf-8")

    amd_layout = dict(AMD_CHUNKS)
    if not overlay_parsed:
        del amd_layout["a-26x"]
    for ticker, cik, layout in (("AMD", AMD_CIK, amd_layout), ("AVGO", AVGO_CIK, AVGO_CHUNKS)):
        ch = chunk_frame(ticker, cik, manifests[ticker], layout)
        ch.to_parquet(lake.chunks_dir / f"{ticker}_chunks.parquet", index=False)
        keys = {(a, "I.1A") for a, _, _ in manifests[ticker]} | set(zip(ch["accession_no"], ch["section_id"]))
        sections = pd.DataFrame([{"accession_no": a, "section_id": sec, "section_title": sec, "n_chars": 100}
                                 for a, sec in sorted(keys)])
        sections.to_parquet(lake.interim_dir / "section_texts" / f"{ticker}_section_texts.parquet", index=False)
    for name, records in (("amd", AMD_RECORDS), ("avgo", AVGO_RECORDS)):
        (lake.extractions_dir / f"{name}_extractions.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8")
    (lake.raw_dir / "federal_register_bis_rules.json").write_text(json.dumps({"results": RULES}), encoding="utf-8")
    (lake.processed_dir / "xbrl").mkdir(parents=True)
    pd.DataFrame([{"cik": AMD_CIK, "metric": "revenue", "concept": "Revenues", "val": 25.8e9, "unit": "USD",
                   "start": "2025-01-01", "end": "2025-12-31", "accn": "a-26"}]
                 ).to_parquet(lake.processed_dir / "xbrl" / "AMD_key_metrics.parquet", index=False)
    return lake


# ------------------------------------------------------------------- helpers

def rows(driver, query, **params):
    with driver.session() as session:
        return [dict(r) for r in session.run(query, **params)]


def one(driver, query, **params):
    return rows(driver, query, **params)[0]


def search(driver, vector, where, limit):
    return [r["id"] for r in rows(driver, f"""
        MATCH (e:EvidenceSpan) SEARCH e IN (VECTOR INDEX evidence_embedding FOR $v WHERE {where} LIMIT {limit})
        SCORE AS score RETURN e.chunk_id AS id ORDER BY score DESC""", v=[float(x) for x in vector])]


def node_and_edge_counts(driver):
    return (one(driver, "MATCH (n) RETURN count(n) AS n")["n"], one(driver, "MATCH ()-[r]->() RETURN count(r) AS n")["n"])


# ------------------------------------------------------------- main scenario

@pytest.fixture(scope="module")
def graph(scratch_database, tmp_path_factory):
    """The synthetic corpus loaded through the real loaders into database ``sgtest``."""
    with scratch_database("sgtest") as (driver, settings):
        lake = build_lake(tmp_path_factory.mktemp("lake"), settings)
        schema.apply_schema(driver)
        driver.execute_query("CALL db.awaitIndexes(120)")
        snapshot = "snap-20260925-abcdef0123"
        embedder = FakeEmbedder()
        counts = {}
        counts["companies"] = loaders.load_companies(driver, lake, snapshot_id=snapshot)
        counts["filings"], counts["sections"] = loaders.load_filings_and_sections(
            driver, lake, ["AMD", "AVGO"], snapshot_id=snapshot)
        counts["metrics"] = loaders.load_metrics(driver, lake, ["AMD"], snapshot_id=snapshot)
        counts["spans"] = loaders.load_evidence_spans(driver, lake, embedder, ["AMD", "AVGO"], snapshot_id=snapshot)
        counts["knowledge"] = loaders.load_knowledge(driver, lake, embedder, ["AMD", "AVGO"], snapshot_id=snapshot)
        counts["export_controls"] = loaders.load_export_controls(driver, lake, snapshot_id=snapshot)
        loaders.stamp_snapshot(driver, snapshot, date(2026, 9, 25), counts)
        driver.execute_query("CALL db.awaitIndexes(120)")
        yield {"driver": driver, "settings": lake, "embedder": embedder, "snapshot": snapshot, "counts": counts}


class TestFilingVersions:
    def test_version_properties(self, graph):
        by_acc = {r["acc"]: r for r in rows(graph["driver"], """
            MATCH (f:Filing) RETURN f.accession_no AS acc, f.status AS status, f.supersede_kind AS kind,
                   f.superseded_by AS by, f.is_current AS cur, f.snapshot_id AS snap""")}
        expect = {
            "a-25": ("superseded", "rolled", "a-26", False), "a-26": ("current", None, None, True),
            "a-26x": ("current", None, None, True),   # overlay: shares its period's status
            "a-26y": ("amendment", None, None, False), "q-may": ("superseded", "rolled", "q-aug", False),
            "q-aug": ("current", None, None, True), "b-26": ("corrected", "corrected", "b-26A", False),
            "b-26A": ("current", None, None, True), "b-q1": ("current", None, None, True),
        }
        assert {a: (r["status"], r["kind"], r["by"], r["cur"]) for a, r in by_acc.items()} == expect
        assert {r["snap"] for r in by_acc.values()} == {graph["snapshot"]}

    def test_a_partial_amendment_is_an_overlay_not_a_replacement(self, graph):
        by_acc = {r["acc"]: r for r in rows(graph["driver"], """
            MATCH (f:Filing) WHERE f.form IN ['10-K', '10-K/A']
            RETURN f.accession_no AS acc, f.corrected_sections AS corrected""")}
        assert by_acc["a-26"]["corrected"] == ["II.7"]               # present in the 10-K, restated by the 10-K/A
        assert by_acc["a-26x"]["corrected"] == [] and by_acc["a-25"]["corrected"] == []
        assert by_acc["b-26"]["corrected"] == ["I.1A"]               # a full replacement retires every section
        amends = rows(graph["driver"], """
            MATCH (n:Filing)-[a:AMENDS]->(o:Filing)
            RETURN n.accession_no AS newer, o.accession_no AS older, a.sections AS sections""")
        assert amends == [{"newer": "a-26x", "older": "a-26", "sections": ["II.7"]}]

    def test_supersedes_edges_run_newer_to_older(self, graph):
        edges = {(r["newer"], r["older"], r["kind"]) for r in rows(graph["driver"], """
            MATCH (n:Filing)-[s:SUPERSEDES]->(o:Filing)
            RETURN n.accession_no AS newer, o.accession_no AS older, s.kind AS kind""")}
        assert edges == {("a-26", "a-25", "rolled"), ("q-aug", "q-may", "rolled"), ("b-26A", "b-26", "corrected")}

    def test_at_most_one_current_filing_per_company_and_family(self, graph):
        groups = rows(graph["driver"], """
            MATCH (c:Company)-[:FILED]->(f:Filing) WHERE f.is_current = true AND NOT (f)-[:AMENDS]->()
            RETURN c.ticker AS ticker,
                   CASE WHEN f.form IN ['10-K', '10-K/A', '20-F'] THEN 'annual' ELSE 'quarterly' END AS family,
                   count(f) AS n""")
        assert {(g["ticker"], g["family"]) for g in groups} == {
            ("AMD", "annual"), ("AMD", "quarterly"), ("AVGO", "annual"), ("AVGO", "quarterly")}
        assert all(g["n"] == 1 for g in groups)

    def test_every_node_the_loaders_wrote_carries_the_snapshot(self, graph):
        labels = {r["label"] for r in rows(graph["driver"], "MATCH (n) RETURN DISTINCT labels(n)[0] AS label")}
        assert labels >= {"Company", "Filing", "FilingSection", "Metric", "EvidenceSpan", "RiskFactor",
                          "Product", "ExportControl", "Snapshot"}
        bare = rows(graph["driver"], """
            MATCH (n) WHERE NOT n:Snapshot AND (n.snapshot_id IS NULL OR n.snapshot_id <> $snap)
            RETURN labels(n)[0] AS label, count(*) AS n""", snap=graph["snapshot"])
        assert bare == []


class TestEvidenceSpans:
    def test_history_with_extraction_records_is_loaded_and_the_rest_of_a_retired_quarterly_is_not(self, graph):
        per_filing = {r["acc"]: r["n"] for r in rows(graph["driver"], """
            MATCH (e:EvidenceSpan) RETURN e.accession_no AS acc, count(*) AS n""")}
        assert per_filing == {"a-25": 4, "a-26": 9, "a-26x": 2, "q-may": 2, "q-aug": 6,
                              "b-26": 1, "b-26A": 4, "b-q1": 3}

    def test_span_contract_properties(self, graph):
        r = one(graph["driver"], """
            MATCH (e:EvidenceSpan {chunk_id: 'a-26:I.1A:0000'})
            RETURN e.text AS text, e.content_hash AS h, e.filer_cik AS cik, e.form AS form,
                   e.accession_no AS acc, e.section_id AS sec, e.filing_date AS fd, e.valid_from AS vf,
                   e.valid_to AS vt, e.is_current AS cur, e.retrievable AS ret, e.status AS status,
                   e.source_type AS st, e.snapshot_id AS snap, size(e.embedding) AS dim""")
        assert r["h"] == content_hash(r["text"])
        assert (r["cik"], r["form"], r["acc"], r["sec"]) == (AMD_CIK, "10-K", "a-26", "I.1A")
        assert r["fd"].to_native() == r["vf"].to_native() == date(2026, 2, 4)
        assert r["vt"].to_native() == OPEN_END
        assert (r["cur"], r["ret"], r["status"], r["st"], r["snap"], r["dim"]) == (
            True, True, "current", "sec_filing", graph["snapshot"], EMBED_DIM)

    def test_retrievable_semantics(self, graph):
        by_section = {(r["acc"], r["sec"]): (r["status"], r["cur"], r["ret"], r["vt"].to_native())
                      for r in rows(graph["driver"], """
            MATCH (e:EvidenceSpan) RETURN DISTINCT e.accession_no AS acc, e.section_id AS sec, e.status AS status,
                   e.is_current AS cur, e.retrievable AS ret, e.valid_to AS vt""")}
        assert by_section == {
            ("a-25", "I.1A"): ("superseded", False, True, date(2026, 2, 4)),  # superseded ANNUAL: stays retrievable
            ("a-26", "I.1"): ("current", True, True, OPEN_END),
            ("a-26", "I.1A"): ("current", True, True, OPEN_END),
            # the original's Item 7 was restated by the 10-K/A: neither current nor retrievable, valid until then
            ("a-26", "II.7"): ("corrected", False, False, date(2026, 3, 1)),
            ("a-26x", "II.7"): ("current", True, True, OPEN_END),
            ("q-may", "II.1A"): ("superseded", False, False, date(2026, 8, 5)),  # superseded quarterly
            ("q-aug", "II.1A"): ("current", True, True, OPEN_END),
            ("b-26", "I.1A"): ("corrected", False, False, date(2026, 1, 20)),    # fully corrected original
            ("b-26A", "I.1"): ("current", True, True, OPEN_END),
            ("b-26A", "I.1A"): ("current", True, True, OPEN_END),
            ("b-q1", "II.1A"): ("current", True, True, OPEN_END),
        }

    def test_loading_twice_changes_nothing(self, graph):
        before = node_and_edge_counts(graph["driver"])
        loaders.load_companies(graph["driver"], graph["settings"], snapshot_id=graph["snapshot"])
        loaders.load_metrics(graph["driver"], graph["settings"], ["AMD"], snapshot_id=graph["snapshot"])
        loaders.load_filings_and_sections(graph["driver"], graph["settings"], ["AMD", "AVGO"], snapshot_id=graph["snapshot"])
        loaders.load_evidence_spans(graph["driver"], graph["settings"], graph["embedder"], ["AMD", "AVGO"],
                                    snapshot_id=graph["snapshot"])
        loaders.load_knowledge(graph["driver"], graph["settings"], graph["embedder"], ["AMD", "AVGO"],
                               snapshot_id=graph["snapshot"])
        loaders.load_export_controls(graph["driver"], graph["settings"], snapshot_id=graph["snapshot"])
        assert node_and_edge_counts(graph["driver"]) == before


class TestFilteredSearch:
    def query_vector(self, key="a-25:I.1A:0000"):
        return embedding_for(key)  # a NON-current span: an unfiltered search would rank it first

    def test_without_the_currency_filter_a_non_current_span_ranks_first(self, graph):
        assert search(graph["driver"], self.query_vector(), "e.filer_cik = 2488", 5)[0] == "a-25:I.1A:0000"

    def test_current_filter_returns_only_current_spans_of_that_company(self, graph):
        ids = search(graph["driver"], self.query_vector(), f"e.filer_cik = {AMD_CIK} AND e.is_current = true", 5)
        assert len(ids) == 5
        assert all(i.startswith(("a-26:", "a-26x:", "q-aug:")) for i in ids) and "a-26:II.7:0000" not in ids

    def test_company_filter_excludes_other_companies(self, graph):
        ids = search(graph["driver"], self.query_vector("b-26A:I.1A:0001"), f"e.filer_cik = {AMD_CIK} AND e.is_current = true", 5)
        assert ids and not any(i.startswith("b-") for i in ids)

    def test_retrievable_filter_keeps_historical_annual_text(self, graph):
        ids = search(graph["driver"], self.query_vector(), f"e.filer_cik = {AMD_CIK} AND e.retrievable = true", 12)
        assert any(i.startswith("a-25:") for i in ids)
        assert not any(i.startswith("q-may:") for i in ids)

    def test_valid_date_range_filter(self, graph):
        ids = search(graph["driver"], self.query_vector("q-may:II.1A:0000"),
                     "e.valid_from <= date('2026-06-01') AND e.valid_to > date('2026-06-01')", 30)
        assert ids and {i.split(":")[0] for i in ids} <= {"a-26", "a-26x", "q-may", "b-26A", "b-q1"}
        assert not any(i.startswith("a-26:II.7") for i in ids)  # restated on 2026-03-01

    def test_a_restated_section_is_not_retrievable_but_its_amendment_is(self, graph):
        ids = search(graph["driver"], embedding_for("a-26:II.7:0000"),
                     f"e.filer_cik = {AMD_CIK} AND e.retrievable = true", 30)
        assert "a-26:II.7:0000" not in ids
        assert {"a-26x:II.7:0000", "a-26x:II.7:0001"} <= set(ids)

    def test_demoted_spans_disappear_without_diluting_top_k(self, graph):
        driver = graph["driver"]
        demote = [f"a-26:I.1A:{k:04d}" for k in range(4)]  # 16 current AMD spans -> 12 left
        try:
            with driver.session() as session:
                session.run("MATCH (e:EvidenceSpan) WHERE e.chunk_id IN $ids SET e.is_current = false", ids=demote).consume()
            where = f"e.filer_cik = {AMD_CIK} AND e.is_current = true"
            for probe in (demote[0], "q-aug:II.1A:0003"):
                top5 = search(driver, embedding_for(probe), where, 5)
                assert len(top5) == 5 and not set(top5) & set(demote)
            everything = search(driver, embedding_for(demote[0]), where, 14)
            assert len(everything) == 12 and not set(everything) & set(demote)
        finally:
            with driver.session() as session:
                session.run("MATCH (e:EvidenceSpan) WHERE e.chunk_id IN $ids SET e.is_current = true", ids=demote).consume()
        assert len(search(driver, embedding_for(demote[0]), f"e.filer_cik = {AMD_CIK} AND e.is_current = true", 18)) == 16

    def test_fulltext_index_finds_exact_tokens(self, graph):
        hits = rows(graph["driver"], "CALL db.index.fulltext.queryNodes('evidence_text_ft', 'H200') "
                                     "YIELD node RETURN node.chunk_id AS id")
        assert [h["id"] for h in hits] == ["a-26:I.1A:0000"]

    def test_risk_index_filters_on_currency(self, graph):
        ids = rows(graph["driver"], f"""
            MATCH (rf:RiskFactor) SEARCH rf IN (VECTOR INDEX risk_embedding FOR $v
                WHERE rf.filer_cik = {AVGO_CIK} AND rf.is_current = true LIMIT 5) SCORE AS s
            RETURN rf.summary AS summary""", v=[float(x) for x in embedding_for("Equipment controls affect suppliers")])
        assert {r["summary"] for r in ids} == {"We may be added to the Entity List", "Customer concentration"}


class TestRisksAndRelations:
    def test_risk_factors_inherit_their_filings_validity(self, graph):
        by_summary = {r["s"]: r for r in rows(graph["driver"], """
            MATCH (rf:RiskFactor) RETURN rf.summary AS s, rf.is_current AS cur, rf.form AS form,
                   rf.filer_cik AS cik, rf.valid_from AS vf, rf.valid_to AS vt, rf.snapshot_id AS snap""")}
        old = by_summary["Equipment restrictions hurt foundry partners"]
        assert (old["cur"], old["form"], old["cik"], old["vt"].to_native()) == (False, "10-K", AMD_CIK, date(2026, 2, 4))
        new = by_summary["Export controls limit advanced computing sales"]
        assert (new["cur"], new["vt"].to_native(), new["vf"].to_native()) == (True, OPEN_END, date(2026, 2, 4))
        assert {r["snap"] for r in by_summary.values()} == {graph["snapshot"]}
        restated = by_summary["Restated MD&A liquidity risk"]      # evidenced in the 10-K's restated Item 7
        assert (restated["cur"], restated["vt"].to_native()) == (False, date(2026, 3, 1))
        amended = by_summary["Amended MD&A liquidity risk"]        # ... and its replacement in the 10-K/A
        assert (amended["cur"], amended["form"]) == (True, "10-K/A")

    def test_relation_edges_know_whether_current_evidence_backs_them(self, graph):
        edges = {r["src"]: r for r in rows(graph["driver"], """
            MATCH (s:Company)-[r:SUPPLIES_TO]->(:Company {cik: 2488})
            RETURN s.name AS src, r.has_current_evidence AS cur, r.last_evidenced_at AS last""")}
        assert edges["TSMC"]["cur"] is True and edges["TSMC"]["last"].to_native() == date(2026, 8, 5)
        assert edges["Micron"]["cur"] is False and edges["Micron"]["last"].to_native() == date(2026, 5, 6)

    def test_lineage_clustering_reads_only_effective_annual_filings(self, graph):
        frame = temporal.fetch_annual_risks(graph["driver"])
        assert set(frame["accession_no"]) == {"a-25", "a-26", "b-26A"}  # not the corrected b-26, no quarterlies
        # the restated Item-7 risk never feeds a lineage; the overlay's replacement does, dated by the 10-K it amends
        assert "Restated MD&A liquidity risk" not in set(frame["summary"])
        amended = frame[frame["summary"] == "Amended MD&A liquidity risk"].iloc[0]
        assert (amended["accession_no"], amended["filing_date"]) == ("a-26", "2026-02-04")
        result = temporal.apply_closure(graph["driver"], graph["settings"])
        assert result["states_written"] == len(frame) == 4


class TestExportControls:
    def test_export_control_nodes_carry_classification(self, graph):
        by_rule = {r["id"]: r for r in rows(graph["driver"], """
            MATCH (x:ExportControl) RETURN x.rule_id AS id, x.kind AS kind, x.topics AS topics,
                   x.relevant AS relevant, x.snapshot_id AS snap""")}
        assert set(by_rule) == {"R1", "R2", "R3", "R4", "R5", "R6"}
        assert (by_rule["R1"]["kind"], by_rule["R1"]["relevant"], by_rule["R1"]["topics"]) == (
            "advanced_computing", True, ["advanced computing"])
        assert (by_rule["R5"]["kind"], by_rule["R5"]["relevant"], by_rule["R5"]["topics"]) == ("legacy", True, [])
        assert by_rule["R3"]["relevant"] is False and by_rule["R4"]["relevant"] is False

    def test_affected_by_links_only_relevant_rules_backed_by_current_evidence(self, graph):
        linked = {}
        for r in rows(graph["driver"], """
                MATCH (c:Company)-[:AFFECTED_BY]->(x:ExportControl) RETURN c.ticker AS t, x.rule_id AS rule"""):
            linked.setdefault(r["t"], set()).add(r["rule"])
        # AMD: its CURRENT risk names advanced computing (R1, and legacy R5 via its title);
        #      its equipment risk is historical (R2 not linked); R3/R4 are irrelevant.
        # AVGO: its current risk names the Entity List / affiliates (R6); the lithography text is in a corrected original.
        assert linked == {"AMD": {"R1", "R5"}, "AVGO": {"R6"}}

    def test_affected_by_edges_carry_the_matching_evidence_chunks(self, graph):
        edge = one(graph["driver"], """
            MATCH (:Company {ticker: 'AMD'})-[r:AFFECTED_BY]->(:ExportControl {rule_id: 'R1'})
            RETURN r.evidence_chunk_ids AS chunks, r.status AS status, r.start_date AS start""")
        assert edge["chunks"] == ["a-26:I.1A:0000"] and edge["status"] == "Active"
        assert edge["start"].to_native() == date(2026, 1, 15)


class TestSnapshotNode:
    def test_snapshot_node_and_uniqueness(self, graph):
        driver = graph["driver"]
        node = one(driver, "MATCH (s:Snapshot) RETURN s.id AS id, s.as_of AS as_of, s.created_at AS created, "
                           "s.code_version AS version, s.counts AS counts")
        assert node["id"] == graph["snapshot"] and node["as_of"].to_native() == date(2026, 9, 25)
        assert node["created"] is not None and node["version"]
        assert json.loads(node["counts"])["spans"] == graph["counts"]["spans"]
        loaders.stamp_snapshot(driver, graph["snapshot"], date(2026, 9, 25), {"spans": 1})
        assert one(driver, "MATCH (s:Snapshot) RETURN count(s) AS n")["n"] == 1
        assert one(driver, "MATCH (s:Snapshot) RETURN s.created_at AS c")["c"] == node["created"]  # set once
        assert json.loads(one(driver, "MATCH (s:Snapshot) RETURN s.counts AS c")["c"]) == {"spans": 1}


# --------------------------------------------------- separate scratch scenarios

def test_apply_schema_refuses_a_stale_unfiltered_vector_index(scratch_database):
    with scratch_database("sgteststale") as (driver, _):
        with driver.session() as session:  # the v1 definition: no filter properties
            session.run("""CREATE VECTOR INDEX evidence_embedding IF NOT EXISTS FOR (e:EvidenceSpan) ON (e.embedding)
                OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}""").consume()
        with pytest.raises(RuntimeError, match=r"evidence_embedding.*build-graph --rebuild"):
            schema.apply_schema(driver)
        names = {r["name"] for r in rows(driver, "SHOW INDEXES YIELD name")}
        assert "evidence_text_ft" not in names and "risk_embedding" not in names  # nothing half-applied

        # the documented remedy: reset drops the stale index, then the schema applies cleanly
        schema.reset_graph(driver)
        assert schema.apply_schema(driver) > 0
        info = one(driver, "SHOW INDEXES YIELD name, properties WHERE name = 'evidence_embedding' RETURN properties")
        assert info["properties"] == ["embedding", "is_current", "retrievable", "filer_cik", "form", "valid_from", "valid_to"]


def test_reset_graph_keeps_service_state_and_lets_the_schema_be_recreated(scratch_database):
    with scratch_database("sgtestreset") as (driver, _):
        schema.apply_schema(driver)
        with driver.session() as session:
            session.run("""CREATE (:SvcAnswer {key: 'a'}), (:SvcPolicy {key: 'kill'}), (:Company {cik: 1, name: 'X'})
                           -[:FILED]->(:Filing {accession_no: 'f'})""").consume()
            session.run("UNWIND range(1, 2500) AS i CREATE (:EvidenceSpan {chunk_id: 'c' + i})").consume()
        result = schema.reset_graph(driver)
        assert result == {"deleted_nodes": 2502}
        labels = {r["label"] for r in rows(driver, "MATCH (n) RETURN DISTINCT labels(n)[0] AS label")}
        assert labels == {"SvcAnswer", "SvcPolicy"}
        names = {r["name"] for r in rows(driver, "SHOW INDEXES YIELD name")}
        assert not names & {"evidence_embedding", "risk_embedding", "evidence_text_ft", "risk_summary_ft"}
        assert "chunk_id" in names  # constraints survive
        schema.apply_schema(driver)
        assert {"evidence_embedding", "risk_embedding", "evidence_text_ft", "risk_summary_ft"} <= {
            r["name"] for r in rows(driver, "SHOW INDEXES YIELD name")}
        schema.reset_graph(driver, keep_service_state=False)
        assert rows(driver, "MATCH (n) RETURN n") == []


def test_a_late_parsed_amendment_replaces_stale_edges(scratch_database, tmp_path):
    with scratch_database("sgtesttransition") as (driver, settings):
        schema.apply_schema(driver)
        lake = build_lake(tmp_path, settings, overlay_parsed=False)
        loaders.load_companies(driver, lake, snapshot_id="s1")

        def reload(layout, snapshot):
            chunk_frame("AMD", AMD_CIK, AMD_MANIFEST, layout).to_parquet(lake.chunks_dir / "AMD_chunks.parquet", index=False)
            loaders.load_filings_and_sections(driver, lake, ["AMD"], snapshot_id=snapshot)

        def edges(kind):
            return {(r["n"], r["o"]) for r in rows(driver, f"""
                MATCH (n:Filing)-[:{kind}]->(o:Filing) RETURN n.accession_no AS n, o.accession_no AS o""")}

        def status(acc):
            return one(driver, "MATCH (f:Filing {accession_no: $a}) RETURN f.status AS s, f.is_current AS c", a=acc)

        base = {k: v for k, v in AMD_CHUNKS.items() if k != "a-26x"}
        reload(base, "s1")                                    # 1. the 10-K/A is not segmented yet
        assert status("a-26x") == {"s": "amendment", "c": False}
        assert edges("SUPERSEDES") == {("a-26", "a-25"), ("q-aug", "q-may")} and edges("AMENDS") == set()

        reload({**base, "a-26x": {"I.1A": 2}}, "s2")          # 2. segmented: restates I.1A only -> overlay
        assert status("a-26") == {"s": "current", "c": True} and status("a-26x") == {"s": "current", "c": True}
        assert edges("AMENDS") == {("a-26x", "a-26")}
        assert one(driver, "MATCH (f:Filing {accession_no: 'a-26'}) RETURN f.corrected_sections AS c")["c"] == ["I.1A"]

        reload({**base, "a-26x": {"I.1": 4, "I.1A": 4, "II.7": 3}}, "s3")   # 3. re-segmented: restates everything
        assert status("a-26") == {"s": "corrected", "c": False} and status("a-26x") == {"s": "current", "c": True}
        assert edges("AMENDS") == set()                       # the overlay edge from step 2 is gone
        assert edges("SUPERSEDES") == {("a-26x", "a-25"), ("a-26x", "a-26"), ("q-aug", "q-may")}  # ... and so is a-26 -> a-25
