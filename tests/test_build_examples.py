"""scripts/build_examples.py: the seeded example answers are the benchmark's hybrid runs of ONE snapshot."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_examples.py"
spec = importlib.util.spec_from_file_location("build_examples", SCRIPT)
be = importlib.util.module_from_spec(spec)
sys.modules["build_examples"] = be
spec.loader.exec_module(be)

SNAP = "snap-20260924-7feaaf9bfe"
BENCH = [{"id": "N1", "type": "numeric", "q": "Revenue?"}, {"id": "U1", "type": "refusal", "q": "Samsung?"}]


def run(id_, system="hybrid", answer="A [c1].", cited=("c1",), hallucinated=()):
    return {"id": id_, "system": system, "type": "x", "q": "q", "answer": answer,
            "cited": list(cited), "hallucinated": list(hallucinated)}


def runs(**over):
    return [run("N1"), run("U1", answer="Not in the corpus.", cited=()), run("N1", system="vector", answer="v")]


def test_examples_are_the_hybrid_runs_in_benchmark_order_and_carry_the_snapshot():
    doc = be.build_examples(list(reversed(runs())), BENCH, SNAP, source="test run")
    assert doc["snapshot_id"] == SNAP and doc["source"] == "test run"
    assert [e["id"] for e in doc["examples"]] == ["N1", "U1"]
    n1 = doc["examples"][0]
    assert n1 == {"id": "N1", "type": "numeric", "question": "Revenue?", "answer": "A [c1].",
                  "citations": ["c1"], "hallucinated": []}


def test_a_benchmark_question_without_a_hybrid_run_is_refused():
    with pytest.raises(ValueError, match="U1"):
        be.build_examples([run("N1")], BENCH, SNAP, source="s")


def test_an_example_with_hallucinated_citations_is_refused():
    with pytest.raises(ValueError, match="hallucinated"):
        be.build_examples([run("N1", hallucinated=("bogus",)), run("U1")], BENCH, SNAP, source="s")


def test_an_empty_answer_is_refused():
    with pytest.raises(ValueError, match="empty"):
        be.build_examples([run("N1", answer="  "), run("U1")], BENCH, SNAP, source="s")


def test_snapshot_must_look_like_a_real_snapshot_id():
    with pytest.raises(ValueError, match="snapshot"):
        be.build_examples(runs(), BENCH, "latest", source="s")
