"""M2 dependencies (docs/specs/m2-search.md OD2, OD14): bm25s serves retrieval; ir-measures is benchmark-only."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bm25s_imports_and_scores_a_tiny_corpus():
    import bm25s

    retriever = bm25s.BM25()
    retriever.index(bm25s.tokenize(["export controls on chips", "foundry supply in taiwan", "memory pricing"],
                                   stopwords=None, show_progress=False), show_progress=False)
    docs, scores = retriever.retrieve(bm25s.tokenize(["taiwan foundry"], stopwords=None, show_progress=False), k=1,
                                      show_progress=False)
    assert int(docs[0][0]) == 1 and float(scores[0][0]) > 0


def test_bm25s_is_in_the_serve_extra_and_the_hash_locked_serve_requirements():
    assert re.search(r'"bm25s>=0\.3\.16"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert re.search(r"^bm25s>=0\.3\.16$", (ROOT / "deploy/requirements-serve.in").read_text(encoding="utf-8"), re.M)
    locked = (ROOT / "deploy/requirements-serve.txt").read_text(encoding="utf-8")
    assert re.search(r"^bm25s==0\.3\.\d+ \\$", locked, re.M) and "--hash=sha256:" in locked.split("bm25s==", 1)[1][:400]


def test_ir_measures_is_a_bench_group_dependency_and_never_imported_by_the_service():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^bench = \[\s*"ir-measures>=0\.4\.3"', pyproject, re.M)
    assert "ir-measures" not in (ROOT / "deploy/requirements-serve.in").read_text(encoding="utf-8")
    for sub in ("serve", "uploads", "retrieval"):
        for f in (ROOT / "src" / "semigraph" / sub).rglob("*.py"):
            assert not re.search(r"^\s*(import|from)\s+ir_measures", f.read_text(encoding="utf-8"), re.M), f
