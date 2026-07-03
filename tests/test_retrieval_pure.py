"""Pure-logic tests for semigraph.retrieval — no Neo4j, no network, no real LLM.

Covers context-block assembly (notebook 14 build_blocks), anchor detection
from the packaged canonical dictionary, citation extraction/validation, the
answer() plumbing with a mocked retriever + llm, and the local hardened
plain-text call's battle scars (litellm fully mocked).
"""

import json
from types import SimpleNamespace

import litellm
import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval import (
    CITE_RE,
    answer,
    build_blocks,
    detect_anchors,
    llm_text,
)

CID1 = "0001045810-26-000021:I.1:0320"
CID2 = "0001045810-26-000021:I.1A:0345"
CID3 = "0001045810-24-000029:I.1A:0152"


def synthetic_retrieval():
    return {
        "anchors": {"Nvidia": 1045810},
        "edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC",
                   "status": "Active", "quote": "We utilize foundries",
                   "chunk_ids": [CID1]}],
        "metrics": [{"company": "Nvidia", "metric": "revenue", "value": 60922000000.0,
                     "period_start": "2023-01-30", "period_end": "2024-01-28"}],
        "risks": [{"company": "Nvidia", "category": "supply_chain",
                   "summary": "Geographic concentration of suppliers",
                   "chunk_id": CID2, "score": 0.9}],
        "temporal": [{"company": "Nvidia", "lineage": "1045810:3",
                      "first_seen": "2023-02-24", "last_seen": "2025-02-26",
                      "example": "COVID-related supply disruption risk"}],
        "chunks": [{"chunk_id": CID3, "score": 0.88,
                    "text": "Export controls affect our China sales.",
                    "source_url": "https://www.sec.gov/x"}],
    }


# --- build_blocks (context assembly) ---

def test_build_blocks_collects_valid_ids_from_all_layers():
    blocks, full_context, valid_ids = build_blocks(synthetic_retrieval())
    assert valid_ids == {CID1, CID2, CID3}


def test_build_blocks_full_context_has_all_five_sections():
    _, full_context, _ = build_blocks(synthetic_retrieval())
    for header in ("RELATIONSHIPS:", "METRICS:", "ACTIVE RISKS:",
                   "DROPPED RISK LINEAGES:", "EXCERPTS:"):
        assert header in full_context
    # the bitemporal layer is surfaced in the context the model (and judge) sees
    assert "disclosed 2023-02-24 through 2025-02-26, then dropped" in full_context
    assert "60,922,000,000 USD" in full_context
    assert "Export controls affect our China sales." in full_context


def test_build_blocks_empty_layers_render_none_placeholders():
    empty = {"anchors": {}, "edges": [], "metrics": [], "risks": [],
             "temporal": [], "chunks": []}
    (e_b, m_b, k_b, t_b, c_b), full_context, valid_ids = build_blocks(empty)
    assert (e_b, m_b, k_b, t_b, c_b) == ("(none)",) * 5
    assert valid_ids == set()


def test_build_blocks_edge_without_chunk_ids_key():
    r = synthetic_retrieval()
    r["edges"] = [{"source": "A", "relation": "COMPETES_WITH", "target": "B",
                   "status": None, "quote": None, "chunk_ids": None}]
    blocks, _, valid_ids = build_blocks(r)
    assert "- A COMPETES_WITH B (status=None)" in blocks[0]
    assert CID1 not in valid_ids


# --- anchor detection (packaged canonical dictionary; deterministic, no LLM) ---

def test_detect_anchors_finds_canonical_entities():
    anchors = detect_anchors("How does Nvidia depend on TSMC?")
    assert anchors.get("Nvidia") == 1045810
    assert "TSMC" in anchors


def test_detect_anchors_is_word_bounded_and_case_insensitive():
    assert "Nvidia" in detect_anchors("what does NVDA disclose?") or \
           "Nvidia" in detect_anchors("what does nvidia disclose?")
    assert detect_anchors("nothing about semiconductors here") == {}


# --- citation grammar ---

def test_cite_re_extracts_valid_ids_only():
    text = (f"Revenue grew [{CID1}]. Risks remain [{CID2}]."
            " Bogus [not-a-chunk] and [12345] ignored.")
    assert set(CITE_RE.findall(text)) == {CID1, CID2}


# --- answer() plumbing with mocked retriever + llm ---

def test_answer_returns_full_context_and_flags_hallucinations(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve",
                        lambda q, d, e, k_chunks=8, hops=2: synthetic_retrieval())
    fake_answer_text = (f"Nvidia depends on TSMC [{CID1}]."
                        f" Made-up claim [0009999999-99-999999:I.1A:0001].")
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        return fake_answer_text

    out = answer("How does Nvidia depend on TSMC?", driver=None, embedder=None,
                 strategy="hybrid", llm=fake_llm)
    assert out["answer"] == fake_answer_text
    assert out["citations"] == sorted([CID1, "0009999999-99-999999:I.1A:0001"])
    assert out["hallucinated"] == {"0009999999-99-999999:I.1A:0001"}
    assert out["valid_ids"] == {CID1, CID2, CID3}
    assert out["chunk_ids"] == [CID3]
    # the full context string is what the answering model saw
    assert "DROPPED RISK LINEAGES:" in out["context"]
    # and the prompt embedded the question + every block
    assert "How does Nvidia depend on TSMC?" in prompts[0]
    assert "=== RISK LINEAGES DROPPED FROM THE LATEST ANNUAL REPORT (bitemporal layer) ===" in prompts[0]


def test_answer_vector_strategy_dispatch(monkeypatch):
    calls = []

    def fake_vector(q, d, e, k=8):
        calls.append(k)
        return {"anchors": {}, "edges": [], "metrics": [], "risks": [],
                "temporal": [], "chunks": []}

    monkeypatch.setattr(answerer_mod, "vector_retrieve", fake_vector)
    out = answer("q", None, None, strategy="vector", llm=lambda p: "No context.", k_chunks=5)
    assert calls == [5]
    assert out["cited"] == set() and out["hallucinated"] == set()


def test_answer_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown strategy"):
        answer("q", None, None, strategy="cypher", llm=lambda p: "x")


# --- llm_text battle scars (litellm mocked) ---

def make_resp(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])


class FakeCompletion:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_llm_text_never_sends_sampling_params_and_disables_thinking(monkeypatch):
    fake = FakeCompletion([make_resp("answer text")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="anthropic/claude-sonnet-5") == "answer text"
    call = fake.calls[0]
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in call
    assert call["thinking"] == {"type": "disabled"}
    assert call["num_retries"] == 2


def test_llm_text_truncation_regenerates_with_doubled_budget(monkeypatch):
    fake = FakeCompletion([make_resp("partial", finish_reason="length"),
                           make_resp("full answer")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="m", max_tokens=1200) == "full answer"
    assert [c["max_tokens"] for c in fake.calls] == [1200, 2400]


def test_llm_text_transient_backs_off_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(answerer_mod.time, "sleep", sleeps.append)
    err = litellm.RateLimitError(message="429", llm_provider="anthropic", model="m")
    fake = FakeCompletion([err, make_resp("ok")])
    monkeypatch.setattr(answerer_mod, "completion", fake)
    assert llm_text("p", model="m") == "ok"
    assert sleeps == [15]


def test_llm_text_empty_content_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(answerer_mod, "completion", FakeCompletion([make_resp(None)] * 4))
    with pytest.raises(RuntimeError, match="llm_text failed after 4 attempts"):
        llm_text("p", model="m")
