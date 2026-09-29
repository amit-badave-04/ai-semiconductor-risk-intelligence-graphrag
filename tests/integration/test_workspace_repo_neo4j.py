"""Integration proof of ``uploads.repo`` and ``serve.monitor``'s Cypher against a REAL Neo4j instance (M4,
docs/v2/M4_PLAN.md 4.1, 4.2, 4.4, 5, 14.4, 14.8).

Opt-in (``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1``) and hard-pinned to the THROWAWAY test instance ONLY
(``bolt://localhost:7898``, ``neo4j`` / ``itest-throwaway-only``) — never 7699 (the real local dev graph), never
7687 (production), whatever ``NEO4J_URI`` happens to be set to in the environment; the connection settings below are
hard-coded, not read from ``.env``. Seeds a tiny, clearly-synthetic public fixture (two Company nodes, one Filing,
three EvidenceSpan nodes with real 1024-d vectors, negative/``ZZTEST``-prefixed ids that cannot collide with real SEC
data) and cleans up only what it created — never a full-database wipe. ``tests/integration/conftest.py``'s
``scratch_database`` is deliberately NOT used here: its Community-edition path resets the WHOLE server database,
which would destroy a concurrent worker's fixtures on the same shared throwaway instance.

The last section runs ``serve.monitor``'s ``SvcLease`` / ``SvcFreshness`` Cypher and ``retrieval.dossier``'s two
hand-written queries (``FILINGS_QUERY``, ``DOSSIER_ACTIVE_RISKS_QUERY``) for real — every other test of those modules
uses a fake ``run_cypher`` and had never actually been sent to a Neo4j server before.
"""

import os
import random
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("neo4j")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import get_driver, run_cypher  # noqa: E402
from semigraph.graph.schema import apply_schema  # noqa: E402
from semigraph.retrieval import dossier  # noqa: E402
from semigraph.serve import monitor as monitor_mod  # noqa: E402
from semigraph.serve.main import graph_stats  # noqa: E402
from semigraph.uploads import repo  # noqa: E402

THROWAWAY_URI = "bolt://localhost:7898"
THROWAWAY_USER = "neo4j"
THROWAWAY_PASSWORD = "itest-throwaway-only"
SYNTHETIC_CIK_A = -900001
SYNTHETIC_CIK_B = -900002
SYNTHETIC_ACCESSION = "9999999999-99-999999"

EVIDENCE_SEARCH_QUERY = """MATCH (node:EvidenceSpan)
SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec
                WHERE node.retrievable = true AND node.filer_cik = $cik LIMIT $k) SCORE AS score
RETURN node.chunk_id AS chunk_id, score ORDER BY score DESC"""


def _vec(seed: int, n: int = 1024) -> list[float]:
    rng = random.Random(seed)
    return [rng.random() for _ in range(n)]


def _chunk(chunk_id: str, text: str, text_hash: str, embedding: list[float], *, tokens: int = 5,
          embedded: bool = True, seq: int = 0) -> dict:
    return {"chunk_id": chunk_id, "seq": seq, "text": text, "text_hash": text_hash, "char_start": 0,
           "char_end": len(text), "tokens": tokens, "embedding": embedding, "embedded": embedded}


def _unit(unit_id: str = "u0", char_end: int = 5) -> dict:
    return {"unit_id": unit_id, "kind": "paragraph", "headline": None, "char_start": 0, "char_end": char_end}


def _no_report() -> dict:
    return {"items_compared": True, "not_compared_reason": None}


@pytest.fixture(scope="module")
def driver():
    if os.environ.get("RUN_NEO4J_TESTS") != "1":
        pytest.skip("Neo4j integration tests are opt-in: set RUN_NEO4J_TESTS=1")
    if os.environ.get("SEMIGRAPH_ALLOW_WIPE") != "1":
        pytest.skip("this suite creates and deletes graph data: set SEMIGRAPH_ALLOW_WIPE=1 too")
    settings = Settings(neo4j_uri=THROWAWAY_URI, neo4j_user=THROWAWAY_USER, neo4j_password=THROWAWAY_PASSWORD,
                       neo4j_database="")
    try:
        d = get_driver(settings)
    except RuntimeError as exc:
        pytest.skip(f"the throwaway Neo4j instance ({THROWAWAY_URI}) is not reachable: {exc}")
    apply_schema(d)
    with d.session() as session:
        session.run("CALL db.awaitIndexes(300)").consume()
    yield d
    d.close()


@pytest.fixture
def public_fixture(driver):
    """Two synthetic Companies, one Filing, three EvidenceSpan nodes (1024-d vectors, the evidence_embedding index
    shape). Cleaned up afterwards regardless of test outcome."""
    with driver.session() as session:
        session.run("""CREATE (a:Company {cik: $cik_a, name: 'Synthetic Test Co A', ticker: 'ZZTESTA'})
            CREATE (b:Company {cik: $cik_b, name: 'Synthetic Test Co B', ticker: 'ZZTESTB'})
            CREATE (f:Filing {accession_no: $acc, form: '10-K', filing_date: date('2026-01-01'),
                is_current: true, status: 'current'})
            CREATE (a)-[:FILED]->(f)
            CREATE (:EvidenceSpan {chunk_id: $acc + ':I.1A:0001', text: 'synthetic evidence one', is_current: true,
                retrievable: true, filer_cik: $cik_a, form: '10-K',
                valid_from: datetime('2026-01-01T00:00:00Z'), valid_to: datetime('9999-12-31T00:00:00Z'),
                embedding: $vec1})
            CREATE (:EvidenceSpan {chunk_id: $acc + ':I.1A:0002', text: 'synthetic evidence two', is_current: true,
                retrievable: true, filer_cik: $cik_a, form: '10-K',
                valid_from: datetime('2026-01-01T00:00:00Z'), valid_to: datetime('9999-12-31T00:00:00Z'),
                embedding: $vec2})
            CREATE (:EvidenceSpan {chunk_id: $acc + ':I.1A:0003', text: 'synthetic evidence three', is_current: true,
                retrievable: true, filer_cik: $cik_a, form: '10-K',
                valid_from: datetime('2026-01-01T00:00:00Z'), valid_to: datetime('9999-12-31T00:00:00Z'),
                embedding: $vec3})""",
                   cik_a=SYNTHETIC_CIK_A, cik_b=SYNTHETIC_CIK_B, acc=SYNTHETIC_ACCESSION,
                   vec1=_vec(1), vec2=_vec(2), vec3=_vec(3)).consume()
    yield {"cik_a": SYNTHETIC_CIK_A, "cik_b": SYNTHETIC_CIK_B, "accession_no": SYNTHETIC_ACCESSION}
    with driver.session() as session:
        session.run("""MATCH (n) WHERE (n:Company AND n.cik IN [$cik_a, $cik_b])
               OR (n:Filing AND n.accession_no = $acc) OR (n:EvidenceSpan AND n.filer_cik = $cik_a)
            DETACH DELETE n""", cik_a=SYNTHETIC_CIK_A, cik_b=SYNTHETIC_CIK_B, acc=SYNTHETIC_ACCESSION).consume()


@pytest.fixture
def two_workspaces(driver):
    ws1, token1, _ = repo.create_workspace(driver, ttl_hours=1)
    ws2, token2, _ = repo.create_workspace(driver, ttl_hours=1)
    yield {"ws1": ws1, "token1": token1, "ws2": ws2, "token2": token2}
    repo.delete_workspace(driver, ws1)
    repo.delete_workspace(driver, ws2)


# ---------------------------------------------------------------------- create / authenticate

def test_authenticate_true_for_the_right_token_false_for_wrong_or_unknown(driver, two_workspaces):
    ws1, token1 = two_workspaces["ws1"], two_workspaces["token1"]
    assert repo.authenticate(driver, ws1, token1) is True
    assert repo.authenticate(driver, ws1, "wrong-token") is False
    assert repo.authenticate(driver, "0" * 32, token1) is False


def test_authenticate_false_for_an_expired_workspace(driver):
    ws, token, _ = repo.create_workspace(driver, ttl_hours=24)
    try:
        with driver.session() as session:
            session.run("MATCH (w:UserWorkspace {workspace_id: $ws}) SET w.expires_at = $past",
                       ws=ws, past=datetime.now(UTC) - timedelta(hours=1)).consume()
        assert repo.authenticate(driver, ws, token) is False
    finally:
        repo.delete_workspace(driver, ws)


# ---------------------------------------------------------------------- put_version / currency flip

def test_put_version_flips_currency_and_evidence_reports_superseded_by(driver, two_workspaces):
    ws = two_workspaces["ws1"]
    now = datetime.now(UTC)
    repo.put_version(driver, ws, document_id="aaaaaaaaaaaa", title="Doc", version=1, content_hash="h1",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk("doc:aaaaaaaaaaaa:v1:0000", "hello", "h1", _vec(10))],
                     change_report=_no_report(), suspicious=False, now=now)
    assert repo.evidence(driver, ws, "doc:aaaaaaaaaaaa:v1:0000")["is_current"] is True

    now2 = now + timedelta(seconds=1)
    repo.put_version(driver, ws, document_id="aaaaaaaaaaaa", title=None, version=2, content_hash="h2",
                     method="text", pages=1, chars=6, chars_per_page=6.0, text="hello2", units=[_unit()],
                     chunks=[_chunk("doc:aaaaaaaaaaaa:v2:0000", "hello2", "h2", _vec(11))],
                     change_report=_no_report(), suspicious=False, now=now2)
    ev1_after = repo.evidence(driver, ws, "doc:aaaaaaaaaaaa:v1:0000")
    assert ev1_after["is_current"] is False and ev1_after["status"] == "superseded"
    assert ev1_after["superseded_by_version"] == 2
    assert repo.evidence(driver, ws, "doc:aaaaaaaaaaaa:v2:0000")["is_current"] is True
    assert repo.latest_version(driver, ws, "aaaaaaaaaaaa")["version"] == 2


# ---------------------------------------------------------------------- search_chunks: isolation + as-of

def test_search_chunks_never_returns_another_workspaces_chunk_even_with_an_identical_vector(driver, two_workspaces):
    ws1, ws2 = two_workspaces["ws1"], two_workspaces["ws2"]
    now = datetime.now(UTC)
    shared_vec = _vec(42)
    repo.put_version(driver, ws1, document_id="aaaaaaaaaaaa", title="A", version=1, content_hash="ha",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk("doc:aaaaaaaaaaaa:v1:0000", "hello", "ha", shared_vec)],
                     change_report=_no_report(), suspicious=False, now=now)
    repo.put_version(driver, ws2, document_id="bbbbbbbbbbbb", title="B", version=1, content_hash="hb",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk("doc:bbbbbbbbbbbb:v1:0000", "hello", "hb", shared_vec)],
                     change_report=_no_report(), suspicious=False, now=now)
    results = repo.search_chunks(driver, ws1, shared_vec, k=10, cutoff=None)
    assert results and all(r["document_id"] != "bbbbbbbbbbbb" for r in results)
    assert any(r["document_id"] == "aaaaaaaaaaaa" for r in results)


def test_search_chunks_as_of_cutoff_returns_the_version_visible_at_that_instant(driver, two_workspaces):
    ws = two_workspaces["ws1"]
    v1_time = datetime.now(UTC)
    v1_vec = _vec(21)
    repo.put_version(driver, ws, document_id="cccccccccccc", title="C", version=1, content_hash="hc1",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="v1", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:cccccccccccc:v1:0000", "v1", "hc1", v1_vec)],
                     change_report=_no_report(), suspicious=False, now=v1_time)
    v2_time = v1_time + timedelta(seconds=2)
    repo.put_version(driver, ws, document_id="cccccccccccc", title=None, version=2, content_hash="hc2",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="v2", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:cccccccccccc:v2:0000", "v2", "hc2", _vec(22))],
                     change_report=_no_report(), suspicious=False, now=v2_time)

    cutoff_before_v2 = v2_time - timedelta(seconds=1)
    as_of_results = repo.search_chunks(driver, ws, v1_vec, k=5, cutoff=cutoff_before_v2)
    assert as_of_results and as_of_results[0]["chunk_id"] == "doc:cccccccccccc:v1:0000"

    current_results = repo.search_chunks(driver, ws, v1_vec, k=5, cutoff=None)
    assert all(r["chunk_id"] != "doc:cccccccccccc:v1:0000" for r in current_results)


# ---------------------------------------------------------------------- embedded_chunks reuse

def test_embedded_chunks_hands_back_the_stored_embedding_by_text_hash(driver, two_workspaces):
    ws = two_workspaces["ws1"]
    vec1 = _vec(31)
    repo.put_version(driver, ws, document_id="dddddddddddd", title="D", version=1, content_hash="hd1",
                     method="text", pages=1, chars=9, chars_per_page=9.0, text="same text", units=[_unit(char_end=9)],
                     chunks=[_chunk("doc:dddddddddddd:v1:0000", "same text", "same-hash", vec1)],
                     change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
    reuse = repo.embedded_chunks(driver, ws, "dddddddddddd")
    assert "same-hash" in reuse
    assert reuse["same-hash"] == pytest.approx(vec1)


# ---------------------------------------------------------------------- delete_workspace / sweep_expired

def test_delete_workspace_leaves_the_other_workspace_intact(driver, two_workspaces):
    ws1, ws2 = two_workspaces["ws1"], two_workspaces["ws2"]
    assert repo.delete_workspace(driver, ws1) is True
    assert repo.get_workspace(driver, ws1) is None
    assert repo.get_workspace(driver, ws2) is not None


def test_sweep_expired_deletes_only_the_expired_workspace(driver, two_workspaces):
    ws1, ws2 = two_workspaces["ws1"], two_workspaces["ws2"]
    with driver.session() as session:
        session.run("MATCH (w:UserWorkspace {workspace_id: $ws}) SET w.expires_at = $past",
                   ws=ws1, past=datetime.now(UTC) - timedelta(hours=1)).consume()
    swept = repo.sweep_expired(driver, datetime.now(UTC))
    assert swept >= 1
    assert repo.get_workspace(driver, ws1) is None
    assert repo.get_workspace(driver, ws2) is not None


# ---------------------------------------------------------------------- leak proofs against the public graph

def test_graph_stats_excludes_user_nodes_and_the_relationship_count_is_unchanged(driver, public_fixture):
    before = graph_stats(driver)
    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    try:
        repo.put_version(driver, ws, document_id="eeeeeeeeeeee", title="E", version=1, content_hash="he",
                        method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                        chunks=[_chunk("doc:eeeeeeeeeeee:v1:0000", "hi", "he", _vec(41))],
                        change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
        after = graph_stats(driver)
        assert "UserWorkspace" not in after["nodes"] and "UserChunk" not in after["nodes"]
        assert "UserDocument" not in after["nodes"] and "UserVersion" not in after["nodes"]
        assert after["relationships"] == before["relationships"]
    finally:
        repo.delete_workspace(driver, ws)


def test_sec_evidence_search_is_identical_before_and_after_workspace_writes(driver, public_fixture):
    cik = public_fixture["cik_a"]
    query_vec = _vec(1)   # identical to the public fixture's own e1 embedding: ranks first either way
    before = run_cypher(driver, EVIDENCE_SEARCH_QUERY, vec=query_vec, cik=cik, k=10)

    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    try:
        repo.put_version(driver, ws, document_id="ffffffffffff", title="F", version=1, content_hash="hf",
                        method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                        chunks=[_chunk("doc:ffffffffffff:v1:0000", "hi", "hf", query_vec)],
                        change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
        after = run_cypher(driver, EVIDENCE_SEARCH_QUERY, vec=query_vec, cik=cik, k=10)
        assert before == after
    finally:
        repo.delete_workspace(driver, ws)


# ---------------------------------------------------------------------- serve.monitor: SvcLease / SvcFreshness (live)

@pytest.fixture
def clean_lease_and_freshness(driver):
    """Removes any ``SvcLease {key:'freshness'}`` / ``SvcFreshness {key:'latest'}`` this test leaves behind — before
    AND after, so a previous failed run never poisons this one."""
    def _clean():
        with driver.session() as session:
            session.run("MATCH (l:SvcLease {key: 'freshness'}) DETACH DELETE l").consume()
            session.run("MATCH (f:SvcFreshness {key: 'latest'}) DETACH DELETE f").consume()

    _clean()
    yield
    _clean()


def test_acquire_lease_admits_one_holder_readmits_it_then_refuses_a_different_holder(driver, clean_lease_and_freshness):
    assert monitor_mod._acquire_lease(driver, "holder-1") is True
    assert monitor_mod._acquire_lease(driver, "holder-1") is True     # the SAME holder is re-admitted (renewal)
    assert monitor_mod._acquire_lease(driver, "holder-2") is False    # a DIFFERENT holder, lease still live


def test_acquire_lease_admits_a_new_holder_once_the_old_one_expires(driver, clean_lease_and_freshness):
    assert monitor_mod._acquire_lease(driver, "holder-1") is True
    with driver.session() as session:
        session.run("MATCH (l:SvcLease {key: 'freshness'}) SET l.until = $past",
                   past=datetime.now(UTC) - timedelta(minutes=1)).consume()
    assert monitor_mod._acquire_lease(driver, "holder-2") is True


def test_persist_and_load_freshness_state_round_trips_live(driver, clean_lease_and_freshness):
    assert monitor_mod._load_persisted(driver) is None
    result = {"checked_at": datetime.now(UTC).isoformat(), "as_of": "2026-09-24",
             "snapshot_id": "snap-20260920-aaaaaaaaaa", "snapshot_as_of": "2026-09-20", "status": "ok",
             "error": None, "pending_filings": [{"ticker": "NVDA", "accession_no": "acc1"}],
             "federal_register": {"graph_count": 1, "live_count": 2, "new_since": 1}, "unresolved": ["TSM"],
             "duration_s": 1.23, "pending_count": 1}
    monitor_mod._persist(driver, result)
    loaded = monitor_mod._load_persisted(driver)
    assert loaded["snapshot_as_of"] == "2026-09-20" and loaded["snapshot_id"] == "snap-20260920-aaaaaaaaaa"
    assert loaded["pending_filings"] == [{"ticker": "NVDA", "accession_no": "acc1"}]
    assert loaded["federal_register"]["live_count"] == 2
    assert loaded["unresolved"] == ["TSM"]


def test_check_once_runs_its_graph_queries_cleanly_against_a_real_empty_result(driver, clean_lease_and_freshness):
    """No Company/Filing/ExportControl fixture is seeded for this one: the point is that every Cypher statement
    check_once issues is syntactically valid and returns cleanly on a real server, not that the numbers are
    interesting (the fixture-backed pending-list logic is already proven purely in test_serve_monitor.py)."""
    result = monitor_mod.check_once(driver, Settings(sec_user_agent="test"),
                                    fetch=lambda url: {"count": 0}, today="2026-09-24")
    assert result["as_of"] == "2026-09-24"
    assert isinstance(result["pending_filings"], list)
    assert result["federal_register"]["graph_count"] == 0


# ---------------------------------------------------------------------- retrieval.dossier: hand-written queries (live)

def test_dossier_filings_and_active_risks_queries_run_cleanly_against_real_neo4j(driver, public_fixture):
    """The two queries dossier.py wrote itself (not imported from retriever.py) — EXPLAIN needs no data, only valid
    Cypher and an existing label/property vocabulary, and proves these were never actually sent to a server before."""
    with driver.session() as session:
        session.run(f"EXPLAIN {dossier.FILINGS_QUERY}", cik=public_fixture["cik_a"]).consume()
        session.run(f"EXPLAIN {dossier.DOSSIER_ACTIVE_RISKS_QUERY}", cik=public_fixture["cik_a"]).consume()
    # And run FILINGS_QUERY for real: the public fixture's one Filing must come back.
    rows = run_cypher(driver, dossier.FILINGS_QUERY, cik=public_fixture["cik_a"])
    assert rows and rows[0]["accession_no"] == public_fixture["accession_no"]
