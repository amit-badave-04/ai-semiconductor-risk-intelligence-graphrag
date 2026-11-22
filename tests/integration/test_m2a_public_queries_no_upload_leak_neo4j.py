"""M2a S5: the public retrieval queries that M2a changed return no workspace-scoped evidence (spec section "Security
requirements", S5). Opt-in (``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1``) and hard-pinned to the THROWAWAY instance
(``bolt://localhost:7898``), never 7699 and never read from ``.env``.

The changed queries are ``COMPANY_EDGES_QUERY`` (now also reads each evidence id's EvidenceSpan metadata) and
``RULE_EDGES_QUERY`` (now also returns the rule abstract). One upload fixture sits beside a small public fixture: a workspace
with a chunk whose id COLLIDES with the id a public edge cites and whose text carries a marker. The marker must appear in
nothing the queries return and nothing ``build_blocks`` renders from them. The test never wipes: every node it creates carries
unique ids and is deleted in ``finally`` (the instance is shared with other suites).
"""

import json
import os
import secrets

import pytest

pytest.importorskip("neo4j")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import get_driver, run_cypher  # noqa: E402
from semigraph.graph.schema import apply_schema  # noqa: E402
from semigraph.retrieval import retriever as R  # noqa: E402
from semigraph.retrieval.answerer import build_blocks  # noqa: E402

THROWAWAY = {"neo4j_uri": "bolt://localhost:7898", "neo4j_user": "neo4j", "neo4j_password": "itest-throwaway-only",
             "neo4j_database": ""}
RUN = secrets.token_hex(4)
CIK_A, CIK_B = 9_200_000 + secrets.randbelow(40_000), 9_250_000 + secrets.randbelow(40_000)
PUBLIC_CHUNK = f"zzs5{RUN}:I.1A:0001"
RULE_ID = f"2099-s5{RUN}"
MARKER = f"WORKSPACE-ONLY-MARKER-{RUN}"
WS = f"zzs5-ws-{RUN}"


@pytest.fixture(scope="module")
def driver():
    if os.environ.get("RUN_NEO4J_TESTS") != "1":
        pytest.skip("Neo4j integration tests are opt-in: set RUN_NEO4J_TESTS=1")
    if os.environ.get("SEMIGRAPH_ALLOW_WIPE") != "1":
        pytest.skip("this test creates and deletes graph data: set SEMIGRAPH_ALLOW_WIPE=1 too")
    try:
        d = get_driver(Settings(_env_file=None, **THROWAWAY))
    except RuntimeError as exc:
        pytest.skip(f"the throwaway Neo4j instance is not reachable: {exc}")
    apply_schema(d)
    yield d
    d.close()


@pytest.fixture(scope="module")
def seeded(driver):
    run_cypher(driver, """
        CREATE (a:Company {cik: $a, name: $an}) CREATE (b:Company {cik: $b, name: $bn})
        CREATE (a)-[:SUPPLIES_TO {status: 'Active', evidence_quote: 'Public quote about the supply line.',
                                  evidence_chunk_ids: [$chunk]}]->(b)
        CREATE (e:EvidenceSpan {chunk_id: $chunk, text: 'Public filing text.', is_current: true, retrievable: true,
                                filer_cik: $a, form: '10-K', filing_date: date('2026-02-01')})
        CREATE (x:ExportControl {rule_id: $rule, title: 'Public rule title', date: date('2099-01-01'),
                                 abstract: 'Public abstract text.', url: 'https://example.test/r', kind: 'rule'})
        CREATE (a)-[:AFFECTED_BY {status: 'Active', link_method: 'keyword'}]->(x)
        CREATE (w:UserWorkspace {workspace_id: $ws, token_hash: 'x'})
        CREATE (c:UserChunk {workspace_id: $ws, chunk_id: $chunk, text: $marker, document_id: 'd', version: 1, seq: 1})
        CREATE (q:UserChunk {workspace_id: $ws, chunk_id: $uchunk, text: $marker, document_id: 'd', version: 1, seq: 2})""",
              a=CIK_A, an=f"S5 Co A {RUN}", b=CIK_B, bn=f"S5 Co B {RUN}", chunk=PUBLIC_CHUNK, rule=RULE_ID, ws=WS,
              marker=MARKER, uchunk=f"doc:{RUN}:v1:0002")
    yield
    run_cypher(driver, """
        OPTIONAL MATCH (n) WHERE (n:Company AND n.cik IN [$a, $b]) OR (n:EvidenceSpan AND n.chunk_id = $chunk)
           OR (n:ExportControl AND n.rule_id = $rule) OR (n.workspace_id = $ws)
        DETACH DELETE n""", a=CIK_A, b=CIK_B, chunk=PUBLIC_CHUNK, rule=RULE_ID, ws=WS)


def test_the_changed_queries_name_no_upload_label():
    for query in (R.COMPANY_EDGES_QUERY, R.RULE_EDGES_QUERY):
        assert "User" not in query and "workspace" not in query.lower()


def test_the_changed_public_queries_return_no_workspace_scoped_evidence(driver, seeded):
    edges = run_cypher(driver, R.company_edges_query(1), ids=[CIK_A])
    rules = run_cypher(driver, R.RULE_EDGES_QUERY, ids=[CIK_A], include_neighbours=False, per_company=R.RULES_PER_COMPANY)
    assert len(edges) == 1 and len(rules) == 1, (edges, rules)
    assert edges[0]["evidence"] == [{"id": PUBLIC_CHUNK, "is_current": True, "filing_date": "2026-02-01"}]
    assert rules[0]["abstract"] == "Public abstract text."
    assert MARKER not in json.dumps([edges, rules], default=str)


def test_the_context_built_from_those_rows_carries_no_workspace_text(driver, seeded):
    edges = run_cypher(driver, R.company_edges_query(1), ids=[CIK_A])
    rules = run_cypher(driver, R.RULE_EDGES_QUERY, ids=[CIK_A], include_neighbours=False, per_company=R.RULES_PER_COMPANY)
    r = {"anchors": {"S5 Co A": CIK_A}, "edges": [*edges, *rules], "metrics": [], "risks": [], "temporal": [],
         "temporal_pairs": [], "chunks": []}
    _, context, valid = build_blocks(r, detail_lines=True)
    assert MARKER not in context
    assert "Public quote about the supply line." in context and "abstract:" in context
    assert not [i for i in valid if i.startswith("doc:")]
