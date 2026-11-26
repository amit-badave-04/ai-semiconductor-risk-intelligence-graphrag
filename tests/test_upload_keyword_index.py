"""I4a / R17, S1, S10: the per-workspace keyword index of an upload workspace (src/semigraph/uploads/keyword_index.py).

The repo is faked (rows per workspace); bm25s is real (a few rows, milliseconds). No database, no network.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from semigraph.retrieval import workspace as ws_mod
from semigraph.uploads import keyword_index as ki
from semigraph.uploads import repo

A, B = "a" * 32, "b" * 32
SRC = Path(ki.__file__).parent


def _row(ws, doc, version, seq, text, title="t.txt"):
    return {"chunk_id": f"doc:{doc}:v{version}:{seq:04d}", "text": text, "document_id": doc, "version": version, "title": title}


class FakeRepo:
    def __init__(self):
        self.rows = {A: [_row(A, "aaaaaaaaaaaa", 1, 0, "Quarterly supply of advanced packaging is constrained by CoWoS capacity."),
                         _row(A, "aaaaaaaaaaaa", 1, 1, "Export controls restrict shipments of accelerators to certain countries.")],
                     B: [_row(B, "bbbbbbbbbbbb", 1, 0, "The zebra migration plan is a confidential strategy for the herd.")]}
        self.row_reads = 0

    def current_versions(self, driver, ws):
        return sorted({(r["document_id"], r["version"]) for r in self.rows.get(ws, [])})

    def current_chunk_rows(self, driver, ws):
        self.row_reads += 1
        return [dict(r) for r in self.rows.get(ws, [])]


@pytest.fixture
def fake(monkeypatch):
    fr = FakeRepo()
    monkeypatch.setattr(repo, "current_versions", fr.current_versions)
    monkeypatch.setattr(repo, "current_chunk_rows", fr.current_chunk_rows)
    monkeypatch.setattr(ki, "CACHE", ki.IndexCache())
    return fr


def test_a_query_finds_the_workspaces_own_text(fake):
    rows = ki.keyword_rows(object(), A, "CoWoS packaging capacity", 6)
    assert [r["chunk_id"] for r in rows] == ["doc:aaaaaaaaaaaa:v1:0000"]
    assert rows[0]["is_current"] is True and rows[0]["title"] == "t.txt" and "CoWoS" in rows[0]["text"]


def test_user_a_query_never_returns_user_b_text(fake):
    """S1: B's text matches A's query exactly, yet A never sees it, in keyword results or fused with dense ones."""
    assert ki.keyword_rows(object(), A, "zebra migration confidential herd", 6) == []
    assert [r["document_id"] for r in ki.keyword_rows(object(), B, "zebra migration", 6)] == ["bbbbbbbbbbbb"]
    assert ki.keyword_rows(object(), B, "CoWoS packaging capacity", 6) == []


def test_user_a_query_never_returns_user_b_text_through_workspace_retrieve(fake, monkeypatch):
    monkeypatch.setattr(repo, "search_chunks", lambda driver, ws, vec, k, cutoff: [])
    embedder = type("E", (), {"encode_query": lambda self, q: [0.0]})()
    got = ws_mod.workspace_retrieve("zebra migration confidential herd", A, object(), embedder)
    assert got["doc_chunks"] == []
    other = ws_mod.workspace_retrieve("zebra migration confidential herd", B, object(), embedder)
    assert [c["document_id"] for c in other["doc_chunks"]] == ["bbbbbbbbbbbb"]


def test_the_index_is_cached_per_workspace_and_a_second_query_reads_no_rows(fake):
    ki.keyword_rows(object(), A, "packaging", 6)
    ki.keyword_rows(object(), A, "export controls", 6)
    assert fake.row_reads == 1
    ki.keyword_rows(object(), B, "zebra", 6)
    assert fake.row_reads == 2 and len(ki.CACHE) == 2


def test_index_invalidated_on_new_version_and_delete(fake):
    ki.keyword_rows(object(), A, "packaging", 6)
    fake.rows[A] = [_row(A, "aaaaaaaaaaaa", 2, 0, "A new version talks about substrates and nothing else.")]
    assert ki.keyword_rows(object(), A, "packaging", 6) == []                  # the old text is gone from the index
    assert [r["version"] for r in ki.keyword_rows(object(), A, "substrates", 6)] == [2]
    assert fake.row_reads == 2
    fake.rows[A] = []                                                           # the document was deleted
    assert ki.keyword_rows(object(), A, "substrates", 6) == []
    assert (A in ki.CACHE) is False, "an empty workspace holds no index"


def test_invalidate_drops_a_workspaces_index(fake):
    ki.keyword_rows(object(), A, "packaging", 6)
    ki.CACHE.invalidate(A)
    assert A not in ki.CACHE
    ki.CACHE.invalidate("never-cached")                                         # idempotent


def test_an_expired_entry_of_another_workspace_is_dropped_on_any_get(fake):
    """HIGH 1: age is enforced for every entry on every get, not only for the workspace being asked about."""
    now = [0.0]
    cache = ki.IndexCache(max_age_s=10, clock=lambda: now[0])
    cache.get(object(), A)
    assert A in cache
    now[0] = 11.0
    cache.get(object(), B)
    assert A not in cache and B in cache


def test_index_cache_bounded_by_count(fake):
    cache = ki.IndexCache(max_entries=3)
    for i in range(6):
        ws = f"{i}" * 32
        fake.rows[ws] = [_row(ws, "cccccccccccc", 1, 0, f"workspace number {i} text about topic{i}")]
        cache.get(object(), ws)
    assert len(cache) == 3 and ("0" * 32) not in cache and ("5" * 32) in cache


def test_index_cache_bounded_by_bytes_least_recently_used_first(fake):
    cache = ki.IndexCache(max_entries=100, max_bytes=3_000)
    for i in range(5):
        ws = f"{i}" * 32
        fake.rows[ws] = [_row(ws, "cccccccccccc", 1, 0, "word " * 150)]         # about 750 bytes of text, more with overhead
        cache.get(object(), ws)
    assert cache.size_bytes <= 3_000 and 0 < len(cache) < 5 and ("4" * 32) in cache


def test_an_entry_too_large_for_the_byte_bound_is_served_but_not_kept(fake):
    cache = ki.IndexCache(max_bytes=100)
    fake.rows[A] = [_row(A, "aaaaaaaaaaaa", 1, 0, "packaging " * 100)]
    assert cache.get(object(), A) is not None and len(cache) == 0


def test_an_entry_older_than_the_max_age_is_rebuilt(fake):
    now = [1000.0]
    cache = ki.IndexCache(max_age_s=60, clock=lambda: now[0])
    cache.get(object(), A)
    cache.get(object(), A)
    assert fake.row_reads == 1
    now[0] += 61
    cache.get(object(), A)
    assert fake.row_reads == 2


def test_a_question_with_no_term_returns_nothing(fake):
    assert ki.keyword_rows(object(), A, "the of and", 6) == [] and ki.keyword_rows(object(), A, "", 6) == []


def test_results_are_limited_to_k_and_ordered_best_first(fake):
    fake.rows[A] = [_row(A, "aaaaaaaaaaaa", 1, i, "packaging " * (i + 1) + "filler words") for i in range(5)]
    got = ki.keyword_rows(object(), A, "packaging", 3)
    assert len(got) == 3 and "doc:aaaaaaaaaaaa:v1:0004" in [r["chunk_id"] for r in got]


def test_fusing_dense_and_keyword_rows_keeps_one_row_per_chunk_within_k():
    dense = [{"chunk_id": "x", "text": "x"}, {"chunk_id": "y", "text": "y"}]
    keyword = [{"chunk_id": "y", "text": "y"}, {"chunk_id": "z", "text": "z"}]
    fused = ki.rrf_fuse(dense, keyword, 2)
    assert [r["chunk_id"] for r in fused] == ["y", "x"]                         # y is in both lists: first
    assert len(ki.rrf_fuse(dense, keyword, 10)) == 3 and ki.rrf_fuse([], [], 6) == []


def test_search_label_names_what_the_ask_used():
    assert ki.search_label(dense=[{"chunk_id": "x"}], as_of=False) == "keyword_semantic"
    assert ki.search_label(dense=[], as_of=False) == "keyword_only"
    assert ki.search_label(dense=[{"chunk_id": "x"}], as_of=True) == "semantic_only"


def test_an_as_of_ask_never_uses_the_keyword_index(fake, monkeypatch):
    seen = []
    monkeypatch.setattr(ki, "keyword_rows", lambda *a, **kw: seen.append(a) or [])
    monkeypatch.setattr(repo, "search_chunks", lambda driver, ws, vec, k, cutoff: [])
    embedder = type("E", (), {"encode_query": lambda self, q: [0.0]})()
    got = ws_mod.workspace_retrieve("packaging", A, object(), embedder, as_of="2026-01-01")
    assert seen == [] and got["search"] == "semantic_only"


# ---- the import graph (S1): the upload path has no reranker and never touches the filings index


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {node.module or "", *(f"{node.module or ''}.{a.name}" for a in node.names)}
    return names


FORBIDDEN_NAMES = ("load_rows", "build_fusion_runtime", "hybrid_retrieve", "FusionRuntime", "hydrate")


def test_upload_path_has_no_reranker_or_filings_index_import():
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 10
    for f in files:
        imports = _imports(f)
        assert not {n for n in imports if n.split(".")[-1] in ("rerank", "fusion") or n.endswith("retrieval.rerank")}, f.name
        text = f.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not [n for n in FORBIDDEN_NAMES if n in code], f"{f.name} reaches the filings index"
    assert {n for n in _imports(SRC / "keyword_index.py") if "retrieval" in n} <= {"retrieval.keyword", "retrieval.keyword.build_index"}


def test_a_keyword_fault_leaves_the_ask_answering_from_the_vector_rows(fake, monkeypatch):
    dense = [{"chunk_id": "doc:aaaaaaaaaaaa:v1:0000", "text": "dense text", "document_id": "aaaaaaaaaaaa", "version": 1,
              "is_current": True, "title": "t"}]
    monkeypatch.setattr(repo, "search_chunks", lambda driver, ws, vec, k, cutoff: dense)

    def boom(*a, **kw):
        raise RuntimeError("bm25s is not installed")

    monkeypatch.setattr(ki, "keyword_rows", boom)
    embedder = type("E", (), {"encode_query": lambda self, q: [0.0]})()
    got = ws_mod.workspace_retrieve("packaging", A, object(), embedder)
    assert [c["chunk_id"] for c in got["doc_chunks"]] == ["doc:aaaaaaaaaaaa:v1:0000"] and got["search"] == "keyword_semantic"


def test_fused_rows_carry_the_same_keys_as_dense_rows(fake, monkeypatch):
    monkeypatch.setattr(repo, "search_chunks", lambda driver, ws, vec, k, cutoff: [])
    embedder = type("E", (), {"encode_query": lambda self, q: [0.0]})()
    got = ws_mod.workspace_retrieve("packaging", A, object(), embedder)
    assert set(got["doc_chunks"][0]) == {"chunk_id", "text", "document_id", "version", "is_current", "title"}
    assert got["search"] == "keyword_only"
