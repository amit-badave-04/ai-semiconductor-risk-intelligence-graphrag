"""Spec S1 (docs/specs/m2-search.md): the upload keyword index never crosses workspaces, proved against a REAL Neo4j.

Opt-in (``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1``) and hard-pinned to the THROWAWAY instance only
(``bolt://localhost:7898``), like ``test_workspace_repo_neo4j.py``; skipped cleanly when either flag is missing or the instance
is unreachable. Synthetic data only. Two workspaces upload a chunk containing the same rare keyword; each workspace's keyword
search must return only its own chunk, whatever the other holds.
"""

import os
from datetime import UTC, datetime

import pytest

pytest.importorskip("neo4j")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import get_driver  # noqa: E402
from semigraph.graph.schema import apply_schema  # noqa: E402
from semigraph.uploads import keyword_index as ki  # noqa: E402
from semigraph.uploads import repo  # noqa: E402

THROWAWAY_URI = "bolt://localhost:7898"
THROWAWAY_USER = "neo4j"
THROWAWAY_PASSWORD = "itest-throwaway-only"
KEYWORD = "zzquillfeather"                       # synthetic, occurs in no real text
DOC_A, DOC_B = "aaaaaaaaaaa1", "bbbbbbbbbbb2"


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
    yield d
    d.close()


@pytest.fixture
def two_workspaces(driver):
    ws1, _, _ = repo.create_workspace(driver, ttl_hours=1)
    ws2, _, _ = repo.create_workspace(driver, ttl_hours=1)
    yield ws1, ws2
    repo.delete_workspace(driver, ws1)
    repo.delete_workspace(driver, ws2)


def _put(driver, ws, doc, text):
    chunk = {"chunk_id": f"doc:{doc}:v1:0000", "seq": 0, "text": text, "text_hash": f"h-{doc}", "char_start": 0,
             "char_end": len(text), "tokens": 5, "embedding": None, "embedded": False}
    repo.put_version(driver, ws, document_id=doc, title=f"{doc}.txt", version=1, content_hash=f"c-{doc}", method="text",
                     pages=1, chars=len(text), chars_per_page=float(len(text)), text=text,
                     units=[{"unit_id": "u0", "kind": "paragraph", "headline": None, "char_start": 0, "char_end": len(text)}],
                     chunks=[chunk], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))


def test_user_a_keyword_search_never_returns_user_b_chunk(driver, two_workspaces, monkeypatch):
    ws_a, ws_b = two_workspaces
    _put(driver, ws_a, DOC_A, f"Workspace A note: the {KEYWORD} shipment slipped by one quarter.")
    _put(driver, ws_b, DOC_B, f"Workspace B note: the {KEYWORD} contract is confidential to B.")
    monkeypatch.setattr(ki, "CACHE", ki.IndexCache())

    rows_a = ki.keyword_rows(driver, ws_a, KEYWORD, 10)
    rows_b = ki.keyword_rows(driver, ws_b, KEYWORD, 10)

    assert [r["document_id"] for r in rows_a] == [DOC_A] and all("confidential to B" not in r["text"] for r in rows_a)
    assert [r["document_id"] for r in rows_b] == [DOC_B] and all("slipped by one quarter" not in r["text"] for r in rows_b)
    assert {r["chunk_id"] for r in rows_a}.isdisjoint({r["chunk_id"] for r in rows_b})
