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

import json
import os
import random
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("neo4j")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import get_driver, run_cypher  # noqa: E402
from semigraph.graph.schema import apply_schema  # noqa: E402
from semigraph.retrieval import dossier  # noqa: E402
from semigraph.serve import guard  # noqa: E402
from semigraph.serve import monitor as monitor_mod  # noqa: E402
from semigraph.serve.main import graph_stats  # noqa: E402
from semigraph.uploads import repo  # noqa: E402
from semigraph.uploads.versions import as_of_cutoff  # noqa: E402

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


def test_search_chunks_as_of_an_instant_shows_the_version_created_at_that_exact_instant(driver, two_workspaces):
    """finding 10, end to end: guard.validate_as_of's normalized instant -> versions.as_of_cutoff -> the real
    Neo4j SEARCH range filter. The page's own "ask as of vN" flow uses a version's own toString(created_at)."""
    ws = two_workspaces["ws1"]
    v1_time = datetime.now(UTC)
    v1_vec = _vec(51)
    repo.put_version(driver, ws, document_id="eeeeeeeeeeee1", title="E1", version=1, content_hash="he1",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="v1", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:eeeeeeeeeeee1:v1:0000", "v1", "he1", v1_vec)],
                     change_report=_no_report(), suspicious=False, now=v1_time)
    v2_time = v1_time + timedelta(seconds=2)
    repo.put_version(driver, ws, document_id="eeeeeeeeeeee1", title=None, version=2, content_hash="he2",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="v2", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:eeeeeeeeeeee1:v2:0000", "v2", "he2", _vec(52))],
                     change_report=_no_report(), suspicious=False, now=v2_time)

    v1_created_at = run_cypher(driver, "MATCH (v:UserVersion {workspace_id: $ws, document_id: $d, version: 1}) "
                                       "RETURN toString(v.created_at) AS created_at",
                               ws=ws, d="eeeeeeeeeeee1")[0]["created_at"]
    as_of = guard.validate_as_of(v1_created_at)
    cutoff = as_of_cutoff(as_of)
    results = repo.search_chunks(driver, ws, v1_vec, k=5, cutoff=cutoff)
    assert results and results[0]["chunk_id"] == "doc:eeeeeeeeeeee1:v1:0000"

    # A cutoff one microsecond BEFORE v1's own created_at must not show it (valid_from < cutoff is strict).
    before_v1 = as_of_cutoff(guard.validate_as_of(v1_created_at)) - timedelta(microseconds=2)
    earlier_results = repo.search_chunks(driver, ws, v1_vec, k=5, cutoff=before_v1)
    assert all(r["chunk_id"] != "doc:eeeeeeeeeeee1:v1:0000" for r in earlier_results)


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


# ---------------------------------------------------------------------- findings 2/13/24: no orphan survives DELETE

def _count_user_nodes(driver, ws: str) -> dict[str, int]:
    """Every User* node still present for `ws`, by label — the reviewer's own diagnostic shape (orphan.py)."""
    counts = {}
    with driver.session() as session:
        for label in repo._USER_LABELS:
            n = session.run(f"MATCH (n:{label} {{workspace_id: $ws}}) RETURN count(n) AS n", ws=ws).single()["n"]
            if n:
                counts[label] = n
    return counts


@pytest.fixture
def cleanup_ws():
    """Registers raw workspace ids for unconditional teardown across every label — used by the RED-path tests
    below, whose whole point is that the normal repo functions (delete_workspace) refuse to touch them."""
    created: list[str] = []
    yield created


@pytest.fixture(autouse=False)
def _drop_ws(driver, cleanup_ws):
    yield
    with driver.session() as session:
        for ws in cleanup_ws:
            for label in repo._USER_LABELS:
                session.run(f"MATCH (n:{label} {{workspace_id: $ws}}) DETACH DELETE n", ws=ws).consume()


def test_put_version_after_delete_raises_workspace_gone_and_leaves_no_orphan(driver, _drop_ws, cleanup_ws):
    """Reproduces the reviewer's orphan.py exactly: create -> delete -> a job that finishes afterwards tries to
    write. Before the fix this left UserDocument/UserVersion/UserChunk/UserUnit orphans forever (findings 2/13/24);
    now put_version must raise WorkspaceGone and write NOTHING."""
    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    cleanup_ws.append(ws)
    assert repo.delete_workspace(driver, ws) is True

    with pytest.raises(repo.WorkspaceGone):
        repo.put_version(driver, ws, document_id="aaaaaaaaaaaa", title="Confidential", version=1,
                         content_hash="h1", method="text", pages=1, chars=len("CONFIDENTIAL uploaded text body"),
                         chars_per_page=5.0, text="CONFIDENTIAL uploaded text body", units=[_unit()],
                         chunks=[_chunk("doc:aaaaaaaaaaaa:v1:0000", "CONFIDENTIAL uploaded text body", "h1",
                                        _vec(99))],
                         change_report=_no_report(), suspicious=False, now=datetime.now(UTC))

    assert _count_user_nodes(driver, ws) == {}, "put_version must leave zero User* nodes for a deleted workspace"


def test_put_job_after_delete_raises_workspace_gone_and_writes_no_userjob(driver, _drop_ws, cleanup_ws):
    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    cleanup_ws.append(ws)
    assert repo.delete_workspace(driver, ws) is True

    with pytest.raises(repo.WorkspaceGone):
        repo.put_job(driver, ws, {"job_id": "j1", "state": "embedding", "document_id": "aaaaaaaaaaaa", "version": 1})

    assert _count_user_nodes(driver, ws) == {}


def test_sweep_expired_and_a_finishing_job_never_leave_an_orphan_either(driver, _drop_ws, cleanup_ws):
    """The other half of the reviewer's scenario: the 15-minute TTL sweeper, not an explicit DELETE, removes the
    workspace out from under a job still running."""
    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    cleanup_ws.append(ws)
    with driver.session() as session:
        session.run("MATCH (w:UserWorkspace {workspace_id: $ws}) SET w.expires_at = $past",
                   ws=ws, past=datetime.now(UTC) - timedelta(hours=1)).consume()
    assert repo.sweep_expired(driver, datetime.now(UTC)) >= 1

    with pytest.raises(repo.WorkspaceGone):
        repo.put_version(driver, ws, document_id="bbbbbbbbbbbb", title="T", version=1, content_hash="h2",
                         method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                         chunks=[_chunk("doc:bbbbbbbbbbbb:v1:0000", "hi", "h2", _vec(98))],
                         change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
    assert _count_user_nodes(driver, ws) == {}


def test_delete_workspace_and_put_version_serialize_instead_of_racing(driver, _drop_ws, cleanup_ws):
    """put_version's lock is taken FIRST: once its transaction has committed, a concurrent delete_workspace must
    still remove everything it wrote (no half-written state survives either order)."""
    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    cleanup_ws.append(ws)
    repo.put_version(driver, ws, document_id="cccccccccccc", title="T", version=1, content_hash="h3",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:cccccccccccc:v1:0000", "hi", "h3", _vec(97))],
                     change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
    assert repo.delete_workspace(driver, ws) is True
    assert _count_user_nodes(driver, ws) == {}


# ---------------------------------------------------------------------- uploads.jobs against the real repo (C3 item 1)


class _FakeSlots:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class _FakeEmbedder:
    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def encode_passages(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeJobSettings:
    upload_parse_timeout_s = 5
    upload_max_pages = 30
    upload_max_tokens = 16000
    upload_max_chunk_tokens = 512
    upload_max_chunks = 120
    upload_max_workspace_tokens = 48000
    upload_max_workspace_pages = 120
    upload_embed_timeout_s = 1200


class _FakeJobApp:
    def __init__(self, driver):
        self.state = type("State", (), {})()
        self.state.driver = driver
        self.state.embedder = _FakeEmbedder()
        self.state.settings = _FakeJobSettings()
        self.state.upload_slots = _FakeSlots()


def test_a_job_whose_workspace_is_already_deleted_ends_cleanly_via_the_real_repo_never_an_unhandled_exception(
        driver, _drop_ws, cleanup_ws, monkeypatch):
    """C3 item 1 (docs/v2/M4_PLAN.md 15.4): reproduces the bug against the REAL ``repo.put_job`` / ``WorkspaceGone``
    (not the unit tests' fake repo) — an EARLY, non-terminal put_job write (the job's very first "received" event)
    that raises WorkspaceGone must end the job thread cleanly with a single, local-only ``workspace_deleted``
    terminal event delivered through the JobRegistry, never an unhandled exception that kills the thread, and must
    leave zero ``User*`` nodes behind."""
    from semigraph.uploads import jobs
    from semigraph.uploads.parse import Block, ParsedDoc

    ws, _token, _exp = repo.create_workspace(driver, ttl_hours=1)
    cleanup_ws.append(ws)
    assert repo.delete_workspace(driver, ws) is True     # gone before the job even starts

    parsed = ParsedDoc(method="text", pages=1,
                       blocks=(Block(text="hello world", page=1, size=10.0, bold=False, kind_hint="paragraph"),),
                       chars_per_page=1000.0, warnings=())
    monkeypatch.setattr("semigraph.uploads.parse.parse_document", lambda *a, **kw: parsed)

    app = _FakeJobApp(driver)
    reg = jobs.registry(app)
    reg.create(ws, "job1")
    jobs._worker(app, ws, "job1", "aaaaaaaaaaaa", "T", b"hello world", "txt", "h" * 64)   # must not raise

    live_events, _ = reg.events_from(ws, "job1", 0)
    terminal = [e for e in live_events if e["state"] == "failed"]
    assert len(terminal) == 1
    assert terminal[0]["error"]["code"] == "workspace_deleted"
    assert app.state.upload_slots.released == 1
    assert _count_user_nodes(driver, ws) == {}


# ---------------------------------------------------------------------- sweep_orphans (findings 2/13/24)

def test_sweep_orphans_removes_user_nodes_seeded_without_a_userworkspace(driver, _drop_ws, cleanup_ws):
    """Orphans can only be seeded by a raw CREATE now that put_version refuses — exactly the defence-in-depth
    scenario sweep_orphans exists for (a write path the lock-and-refuse guard did not anticipate)."""
    ws = "zztest-orphan-" + "0" * 18
    cleanup_ws.append(ws)
    with driver.session() as session:
        session.run("""CREATE (:UserDocument {workspace_id: $ws, document_id: 'd1', created_at: $now,
                title: 'orphan', latest_version: 1})
            CREATE (:UserJob {workspace_id: $ws, job_id: 'j1', state: 'ready', created_at: $now, updated_at: $now,
                payload: '{}'})""", ws=ws, now=datetime.now(UTC)).consume()
    assert _count_user_nodes(driver, ws) == {"UserJob": 1, "UserDocument": 1}

    cleaned = repo.sweep_orphans(driver, datetime.now(UTC))
    assert cleaned >= 1
    assert _count_user_nodes(driver, ws) == {}


def test_sweep_orphans_never_touches_a_live_workspaces_nodes(driver, two_workspaces):
    ws1 = two_workspaces["ws1"]
    repo.put_version(driver, ws1, document_id="dddddddddddd", title="T", version=1, content_hash="h4",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:dddddddddddd:v1:0000", "hi", "h4", _vec(96))],
                     change_report=_no_report(), suspicious=False, now=datetime.now(UTC))
    repo.sweep_orphans(driver, datetime.now(UTC))
    assert repo.get_workspace(driver, ws1) is not None
    assert _count_user_nodes(driver, ws1)   # its own nodes are still there


def test_sweep_orphans_select_query_raises_no_deprecation_notification_on_real_neo4j(driver, caplog):
    """R4 (round-4 reliability review, docs/v2/M4_PLAN.md 15.4): the plain, unscoped ``CALL { ... }`` form is
    deprecated on Neo4j 2026.07.1 and used to log a DEPRECATION notification on every sweep cycle (the sweeper
    always runs this query, finding 26) — ``CALL () { ... }`` (explicit empty scope) must not."""
    with caplog.at_level("WARNING", logger="neo4j.notifications"):
        with driver.session() as session:
            session.run(repo.SWEEP_ORPHANS_SELECT_QUERY).consume()
    assert not any("deprecat" in r.getMessage().lower() for r in caplog.records), caplog.records


# ---------------------------------------------------------------------- fail_interrupted_jobs (finding 27 seam)

def test_fail_interrupted_jobs_marks_a_stale_job_failed_and_a_replay_shows_it(driver, two_workspaces):
    ws = two_workspaces["ws1"]
    stale = datetime.now(UTC) - timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S + 60)
    repo.put_job(driver, ws, {"job_id": "j-stale", "state": "embedding", "document_id": "d1", "version": 1,
                             "progress": {"done": 1, "total": 5}})
    with driver.session() as session:
        session.run("MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) SET j.updated_at = $stale",
                   ws=ws, job_id="j-stale", stale=stale).consume()

    fixed, recovered = repo.fail_interrupted_jobs(driver, datetime.now(UTC))
    assert fixed >= 1 and recovered == 0
    replayed = repo.get_job(driver, ws, "j-stale")
    assert replayed["state"] == "failed"
    assert replayed["error"] == {"code": "interrupted", "message": repo._INTERRUPTED_ERROR_MESSAGE}
    assert replayed["document_id"] == "d1" and replayed["progress"] == {"done": 1, "total": 5}


def test_fail_interrupted_jobs_leaves_a_fresh_non_terminal_job_alone(driver, two_workspaces):
    ws = two_workspaces["ws1"]
    repo.put_job(driver, ws, {"job_id": "j-fresh", "state": "embedding", "document_id": "d1", "version": 1})
    repo.fail_interrupted_jobs(driver, datetime.now(UTC))
    assert repo.get_job(driver, ws, "j-fresh")["state"] == "embedding"


def test_exclusion_aware_sweep_never_fails_a_registered_live_job_but_does_fail_an_equally_stale_dead_one(
        driver, two_workspaces):
    """docs/v2/M4_PLAN.md 15.4: the upload sweeper's interrupted-job pass (``repo.fail_interrupted_jobs`` with the
    registry's open jobs as ``exclude``), run against the REAL database — a job present in THIS process's JobRegistry
    must survive even though its persisted ``updated_at`` looks exactly as stale as a job that is genuinely dead (not in
    the registry), which IS marked ``interrupted``."""
    from semigraph.uploads import jobs

    ws = two_workspaces["ws1"]
    older_than_s = 1800
    stale = datetime.now(UTC) - timedelta(seconds=older_than_s + 60)
    repo.put_job(driver, ws, {"job_id": "j-live", "state": "embedding", "document_id": "d1", "version": 1})
    repo.put_job(driver, ws, {"job_id": "j-dead", "state": "embedding", "document_id": "d1", "version": 1})
    with driver.session() as session:
        session.run("MATCH (j:UserJob {workspace_id: $ws}) WHERE j.job_id IN $ids SET j.updated_at = $stale",
                   ws=ws, ids=["j-live", "j-dead"], stale=stale).consume()

    reg = jobs.JobRegistry()
    reg.create(ws, "j-live")      # still "running" in THIS process, per the registry — j-dead is not tracked at all

    fixed, recovered = repo.fail_interrupted_jobs(driver, older_than_s=older_than_s, exclude=reg.keys())
    assert fixed >= 1 and recovered == 0
    assert repo.get_job(driver, ws, "j-live")["state"] == "embedding"     # excluded: never touched
    assert repo.get_job(driver, ws, "j-dead")["state"] == "failed"        # not registered here: marked interrupted


def test_fail_interrupted_jobs_recovers_a_job_to_ready_when_its_own_version_already_committed(driver, two_workspaces):
    """Round-4 review, finding 27 residual 1, against the REAL database: a job's terminal write can keep failing
    even though ``put_version`` already committed the version — ``fail_interrupted_jobs`` must recover it to
    ``ready``, never mark it ``failed``/``interrupted`` over a version that actually succeeded. Round-4 review 3,
    finding 27 residual 2: the committed ``UserVersion`` must carry THIS job's own ``job_id`` (``put_version``'s new
    keyword) for the recovery to fire at all."""
    ws = two_workspaces["ws1"]
    now = datetime.now(UTC)
    repo.put_version(driver, ws, document_id="ffffffffffff", title="T", version=1, content_hash="hf",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                     chunks=[_chunk("doc:ffffffffffff:v1:0000", "hi", "hf", _vec(77))],
                     change_report=_no_report(), suspicious=False, now=now, job_id="j-stuck")
    # The terminal "ready" write itself never landed (simulated directly): the persisted job is stuck non-terminal.
    repo.put_job(driver, ws, {"job_id": "j-stuck", "state": "indexing", "document_id": "ffffffffffff", "version": 1})
    stale = datetime.now(UTC) - timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S + 60)
    with driver.session() as session:
        session.run("MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) SET j.updated_at = $stale",
                   ws=ws, job_id="j-stuck", stale=stale).consume()

    fixed, recovered = repo.fail_interrupted_jobs(driver, datetime.now(UTC))
    assert recovered >= 1 and fixed == 0
    replayed = repo.get_job(driver, ws, "j-stuck")
    assert replayed["state"] == "ready"
    assert replayed["chunks"] == 1 and replayed["units"] == 1
    assert "error" not in replayed


def test_fail_interrupted_jobs_never_recovers_a_failed_job_from_a_different_jobs_commit_live(driver, two_workspaces):
    """Round-4 review 3, finding 27 residual 2 (secRel LOW repo.py:635 / corUi LOW repo.py:130), against the REAL
    database — reviewer scenario 1: job A (doc D, v2) fails, and its own terminal 'failed' write also exhausts its
    retries, so its persisted state stays non-terminal. The user re-uploads D; job B independently computes the SAME
    next_version (2) and commits it. Before this fix, the periodic sweep would find UserVersion(D, v2) by
    (document_id, version) alone and rewrite job A to 'ready' with job B's stats — job A's own replay would then
    contradict what its live watchers actually saw (failed). It must instead stay interrupted."""
    ws = two_workspaces["ws1"]
    document_id = "aaaaaaaaaaab"
    # Job A: a stale, non-terminal job for (document_id, version=2) whose OWN put_version never ran (it "failed" for
    # real, but even that terminal write never landed) — job_id is never associated with any UserVersion.
    repo.put_job(driver, ws, {"job_id": "job-a", "state": "embedding", "document_id": document_id, "version": 2})
    # Job B: a later, independent upload that computes and commits the SAME (document_id, version) pair.
    repo.put_version(driver, ws, document_id=document_id, title="T", version=2, content_hash="hb",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                     chunks=[_chunk(f"doc:{document_id}:v2:0000", "hi", "hb", _vec(78))],
                     change_report=_no_report(), suspicious=False, now=datetime.now(UTC), job_id="job-b")
    stale = datetime.now(UTC) - timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S + 60)
    with driver.session() as session:
        session.run("MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) SET j.updated_at = $stale",
                   ws=ws, job_id="job-a", stale=stale).consume()

    fixed, recovered = repo.fail_interrupted_jobs(driver, datetime.now(UTC))
    assert fixed >= 1 and recovered == 0, "job A must fall through to interrupted, never recover off job B's commit"
    replayed = repo.get_job(driver, ws, "job-a")
    assert replayed["state"] == "failed"
    assert replayed["error"] == {"code": "interrupted", "message": repo._INTERRUPTED_ERROR_MESSAGE}


def test_fail_interrupted_jobs_never_recovers_a_job_whose_own_version_never_committed_live(driver, two_workspaces):
    """Reviewer scenario 2, against the REAL database: job A's own version NEVER committed at all (no UserVersion
    ever carries job_id='job-a'), while a LATER job (job B) committed the same (document_id, version). Mechanically
    the same guard as the test above, phrased the other way: mere existence of a version for that (document_id,
    version) pair must never be conflated with THIS job having produced it."""
    ws = two_workspaces["ws1"]
    document_id = "aaaaaaaaaaac"
    repo.put_job(driver, ws, {"job_id": "job-a2", "state": "indexing", "document_id": document_id, "version": 5})
    repo.put_version(driver, ws, document_id=document_id, title="T", version=5, content_hash="hc",
                     method="text", pages=1, chars=2, chars_per_page=2.0, text="hi", units=[_unit(char_end=2)],
                     chunks=[_chunk(f"doc:{document_id}:v5:0000", "hi", "hc", _vec(79))],
                     change_report=_no_report(), suspicious=False, now=datetime.now(UTC), job_id="job-b2")
    stale = datetime.now(UTC) - timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S + 60)
    with driver.session() as session:
        session.run("MATCH (j:UserJob {workspace_id: $ws, job_id: $job_id}) SET j.updated_at = $stale",
                   ws=ws, job_id="job-a2", stale=stale).consume()

    fixed, recovered = repo.fail_interrupted_jobs(driver, datetime.now(UTC))
    assert fixed >= 1 and recovered == 0
    assert repo.get_job(driver, ws, "job-a2")["state"] == "failed"


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


def test_release_lease_lets_a_different_holder_acquire_immediately_without_waiting_out_the_lease(
        driver, clean_lease_and_freshness):
    """C1/R1 (M4 review round 2): a machine that finishes (or is killed and restarts) must not leave the next
    holder's due boot check waiting out the full LEASE_MINUTES for a lease nobody needs any more."""
    assert monitor_mod._acquire_lease(driver, "holder-1") is True
    monitor_mod._release_lease(driver, "holder-1")
    assert monitor_mod._acquire_lease(driver, "holder-2") is True   # no wait for `until` to lapse


def test_release_lease_from_a_stale_holder_never_clobbers_the_new_holders_live_lease(driver, clean_lease_and_freshness):
    """The release query's `WHERE l.holder = $me` guard: a release that arrives late — this holder's OWN lease
    already expired and a different machine has since taken it over — must never clear the NEW holder's lease."""
    assert monitor_mod._acquire_lease(driver, "holder-1") is True
    with driver.session() as session:
        session.run("MATCH (l:SvcLease {key: 'freshness'}) SET l.until = $past",
                   past=datetime.now(UTC) - timedelta(minutes=1)).consume()
    assert monitor_mod._acquire_lease(driver, "holder-2") is True   # holder-2 now legitimately owns the lease

    monitor_mod._release_lease(driver, "holder-1")   # a late, stale release from the OLD holder

    assert monitor_mod._acquire_lease(driver, "holder-3") is False   # holder-2's lease must still be live


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
