"""R8a: a deployed row carries the context the model saw; scoring claim support without one raises, never passes."""

import pytest

from semigraph.eval import bakeoff as bo
from semigraph.retrieval.answerer import MAX_CHUNK_IDS_PER_ITEM, build_blocks

IDS = [f"0001045810-26-000021:I.1:{n:04d}" for n in range(1, MAX_CHUNK_IDS_PER_ITEM + 2)]   # one more than is printed
BENCH = [{"id": "D1", "type": "dependency", "q": "Who supplies Nvidia?", "expect": {"any_of": ["TSMC"]}}]


def retrieval():
    return {"edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active", "chunk_ids": IDS}],
            "metrics": [], "risks": [], "temporal": [], "chunks": []}


def done(text, cited):
    return {"event": "done", "answer": text, "citations": cited, "hallucinated": [], "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "cost_usd": 0.001, "routed": "cheap"}


def test_a_deployed_row_carries_the_context_and_the_ids_printed_in_it(tmp_path, monkeypatch):
    monkeypatch.setattr(bo, "hybrid_retrieve", lambda *a, **kw: retrieval())
    monkeypatch.setattr(bo, "stream_answer_for_context", lambda q, r, strategy, **kw: iter([done("TSMC.", [IDS[0]])]))
    [row] = bo.run_deployed(BENCH, None, None, tmp_path / "d.jsonl", model="c/m", escalation_model="s/m", max_usd=None)
    assert row["context"] == build_blocks(retrieval())[1]
    # build_blocks' valid ids hold all four evidence ids, but only the first MAX_CHUNK_IDS_PER_ITEM are printed: R8a is
    # checked against the PRINTED ids
    assert row["context_ids"] == sorted(IDS[:MAX_CHUNK_IDS_PER_ITEM])
    assert all(i in row["context"] for i in row["context_ids"]) and IDS[-1] not in row["context"]


def test_a_row_without_context_makes_the_deployed_score_raise_not_pass():
    row = {"id": "D1", "answer": f"TSMC [{IDS[0]}].", "cited": [IDS[0]], "hallucinated": [], "valid_ids": [IDS[0]],
           "finish_reason": "stop", "usage": None, "cost_usd": 0.0, "latency_s": 1.0, "routed": "cheap", "escalated": False}
    with pytest.raises(ValueError, match="no context"):
        bo.score_deployed([row], BENCH, lambda *a, **kw: {}, votes=1)
