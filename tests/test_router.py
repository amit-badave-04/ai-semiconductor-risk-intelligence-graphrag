"""Deterministic routing: questions about how disclosures CHANGED go straight to the strong model.

Evidence (docs/v2 bake-off): every cheaper model failed the 'evolved across annual reports' question that
Sonnet answered, while matching Sonnet on the numeric, dependency, regulatory and risk questions.
"""

import json
from pathlib import Path

import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval.answerer import answer_stream
from semigraph.retrieval.router import needs_strong_model

BENCH = json.loads((Path(__file__).resolve().parents[1] / "src/semigraph/artifacts/benchmark.json").read_text(encoding="utf-8"))


def test_every_temporal_benchmark_question_is_routed_to_the_strong_model():
    temporal = [b for b in BENCH if b["type"] == "temporal"]
    assert len(temporal) >= 3       # T1-T3 plus the source-text temporal questions merged by scripts/build_temporal_questions.py
    assert all(needs_strong_model(b["q"]) for b in temporal), [b["id"] for b in temporal if not needs_strong_model(b["q"])]


def test_no_other_benchmark_question_is_routed_to_the_strong_model():
    over_routed = [b["id"] for b in BENCH if b["type"] != "temporal" and needs_strong_model(b["q"])]
    assert over_routed == []


@pytest.mark.parametrize("q", [
    "How has Nvidia's risk profile changed over time?",
    "Which risks did Intel stop disclosing after 2024?",
    "What is new in AMD's latest risk factors compared to the prior year?",
    "Show the trend in Broadcom's export-control disclosures.",
    "Were any Micron risks removed from the latest 10-K?",
])
def test_other_phrasings_of_change_over_time_are_routed_to_the_strong_model(q):
    assert needs_strong_model(q)


@pytest.mark.parametrize("q", ["What is Nvidia's revenue?", "Who manufactures AMD's chips?", "Which BIS rules apply to Nvidia?", ""])
def test_ordinary_questions_are_not(q):
    assert not needs_strong_model(q)


# --- wiring into answer_stream ---

CID = "0001045810-24-000029:I.1:0001"
RETRIEVAL = {"anchors": {"Nvidia": 1045810}, "edges": [], "metrics": [], "risks": [], "temporal": [],
             "chunks": [{"chunk_id": CID, "score": 0.9, "text": "HBM text", "source_url": "u"}]}


class Stream:
    def __init__(self, parts, model):
        self.parts, self.model, self.usage, self.finish_reason, self.iterated = parts, model, {"prompt_tokens": 100, "completion_tokens": 10}, "stop", False

    def __iter__(self):
        self.iterated = True
        yield from self.parts


def test_a_temporal_question_skips_the_cheap_draft_and_streams_the_strong_model_live(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: RETRIEVAL)
    cheap, strong = Stream([f"draft [{CID}]"], "cheap/m"), Stream(["Risks ", f"evolved [{CID}]."], "strong/m")
    events = list(answer_stream("How has Nvidia's risk profile evolved?", None, None, llm_stream=lambda p: cheap,
                                escalation_model="strong/m", escalation_stream=lambda p: strong))
    assert [e["event"] for e in events] == ["retrieval", "delta", "delta", "done"] and not cheap.iterated
    done = events[-1]
    assert done["routed"] == "strong" and done["escalated"] is False and done["answered_by"] == "strong/m"


def test_an_ordinary_question_still_takes_the_cheap_path(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: RETRIEVAL)
    cheap, strong = Stream([f"HBM [{CID}]."], "cheap/m"), Stream(["x"], "strong/m")
    events = list(answer_stream("Who supplies HBM?", None, None, llm_stream=lambda p: cheap,
                                escalation_model="strong/m", escalation_stream=lambda p: strong))
    assert not strong.iterated and events[-1]["answered_by"] == "cheap/m" and events[-1]["routed"] == "cheap"


# --- review finding C2: the router must catch natural phrasings, not just the benchmark's own words ---

@pytest.mark.parametrize("q", [
    "How have Nvidia's risk disclosures changed?",
    "How did Intel's risk factors change between 2022 and 2024?",
    "What risks did AMD add in its latest 10-K?",
    "Which risk factors has Nvidia stopped mentioning?",
    "Compare TSMC's 2023 and 2025 risk factors",
    "What is different in Nvidia's latest 10-K risk section versus 2023?",
    "Has Nvidia's export-control risk grown since 2022?",
    "Did Broadcom's disclosures about China shift after 2023?",
    "What was removed from Micron's latest filing?",
])
def test_natural_phrasings_of_a_change_in_disclosures_are_routed_to_the_strong_model(q):
    assert needs_strong_model(q)


@pytest.mark.parametrize("q", [
    "Compare the competition risks disclosed by AMD and Nvidia.",                       # cross-company, not over time
    "By how much did Nvidia's annual revenue grow from fiscal 2024 to fiscal 2026?",    # numeric growth, no disclosure noun
    "What was Microsoft's total revenue for the fiscal year ended June 30, 2025?",
    "What new products does Nvidia sell?",
    "Which BIS rules were issued in 2026 that affect AMD?",
    "What geopolitical risks does ASML disclose?",
])
def test_comparison_growth_and_ordinary_disclosure_questions_stay_on_the_cheap_path(q):
    assert not needs_strong_model(q)
