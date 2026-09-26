"""Cheap draft -> verify -> release, escalating to the stronger model when the verifier objects.

The invariant that matters: a draft the verifier rejects is NEVER shown to the client.
"""

import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval.answerer import answer_stream

CID = "0001045810-24-000029:I.1:0001"
BOGUS = "0000000000-00-000000:I.1:9999"
RETRIEVAL = {"anchors": {"Nvidia": 1045810}, "edges": [], "metrics": [], "risks": [], "temporal": [],
             "chunks": [{"chunk_id": CID, "score": 0.9, "text": "HBM text", "source_url": "u"}]}
GOOD = f"Nvidia depends on SK hynix for HBM [{CID}]."


class Stream:
    """Stream double exposing what TextStream exposes: deltas, usage, finish_reason and model."""

    def __init__(self, parts, usage=(100, 10), finish="stop", model=None, boom=None):
        self.parts, self.finish_reason, self.model, self.boom = list(parts), finish, model, boom
        self.usage = {"prompt_tokens": usage[0], "completion_tokens": usage[1]} if usage else None
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        yield from self.parts
        if self.boom:
            raise self.boom


@pytest.fixture(autouse=True)
def retrieval(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: RETRIEVAL)


def run(draft, escalation=None, escalation_model="strong/m"):
    return list(answer_stream("q", None, None, llm_stream=lambda p: draft, escalation_model=escalation_model,
                              escalation_stream=(lambda p: escalation) if escalation is not None else None))


def kinds(events):
    return [e["event"] for e in events]


def test_a_clean_draft_is_released_after_verification_and_the_strong_model_is_never_called():
    draft, strong = Stream([GOOD[:20], GOOD[20:]], model="cheap/m"), Stream([GOOD], model="strong/m")
    events = run(draft, strong)
    assert kinds(events) == ["retrieval", "delta", "done"]      # buffered, then released in one piece
    assert events[1]["text"] == GOOD and not strong.iterated
    done = events[-1]
    assert done["escalated"] is False and done["answered_by"] == "cheap/m" and done["answer"] == GOOD
    assert done["cost_usd"] == pytest.approx(0.0003)


def test_a_draft_citing_something_that_was_not_retrieved_is_escalated_and_never_shown():
    bad = f"Nvidia depends on SK hynix [{BOGUS}]."
    events = run(Stream([bad], usage=(100, 10), model="cheap/m"), Stream([GOOD[:15], GOOD[15:]], usage=(200, 20), model="strong/m"))
    assert kinds(events) == ["retrieval", "escalated", "delta", "delta", "done"]
    assert events[1] == {"event": "escalated", "reasons": ["invalid_citation"], "from": "cheap/m", "to": "strong/m"}
    assert all(BOGUS not in e.get("text", "") for e in events if e["event"] == "delta")
    done = events[-1]
    assert done["escalated"] is True and done["answered_by"] == "strong/m" and done["answer"] == GOOD
    assert done["escalation_reasons"] == ["invalid_citation"] and done["hallucinated"] == []
    assert done["usage"] == {"prompt_tokens": 300, "completion_tokens": 30}                 # both attempts are paid for
    assert done["cost_usd"] == pytest.approx(300 * 2 / 1e6 + 30 * 10 / 1e6)


@pytest.mark.parametrize("draft,reason", [
    (Stream([""], model="cheap/m"), "empty"),
    (Stream(["Revenue was $215.9 billion."], model="cheap/m"), "no_citation"),
    (Stream([GOOD], finish="length", model="cheap/m"), "truncated"),
    (Stream(["partial"], model="cheap/m", boom=RuntimeError("stream interrupted")), "draft_error"),
])
def test_every_verifier_failure_escalates(draft, reason):
    events = run(draft, Stream([GOOD], model="strong/m"))
    assert events[1]["event"] == "escalated" and events[1]["reasons"][0] == reason
    assert "partial" not in [e.get("text") for e in events if e["event"] == "delta"]


def test_an_explicit_refusal_without_citations_is_a_legitimate_draft():
    refusal = "The context does not contain Samsung's revenue."
    events = run(Stream([refusal], model="cheap/m"), Stream([GOOD]))
    assert kinds(events) == ["retrieval", "delta", "done"] and events[-1]["escalated"] is False


def test_when_the_strong_model_also_fails_the_error_carries_both_attempts_spend():
    strong = Stream(["part"], usage=(200, 5), model="strong/m", boom=RuntimeError("stream interrupted mid-answer"))
    events = run(Stream([f"x [{BOGUS}]"], usage=(100, 10), model="cheap/m"), strong)
    assert kinds(events) == ["retrieval", "escalated", "delta", "error"]
    assert events[-1]["usage"] == {"prompt_tokens": 300, "completion_tokens": 15}
    assert events[-1]["cost_usd"] == pytest.approx(300 * 2 / 1e6 + 15 * 10 / 1e6)


def test_without_an_escalation_model_the_answer_streams_live_exactly_as_before():
    events = list(answer_stream("q", None, None, llm_stream=lambda p: Stream(["Nvidia ", f"[{CID}]"], model="cheap/m")))
    assert kinds(events) == ["retrieval", "delta", "delta", "done"]
    assert "escalated" not in events[-1] and "answered_by" not in events[-1]


def test_an_uncited_dollar_figure_found_in_the_retrieved_metrics_is_released_not_escalated(monkeypatch):
    retrieval = {**RETRIEVAL, "metrics": [{"company": "Nvidia", "metric": "revenue", "period_start": "2025-01-27",
                                           "period_end": "2026-01-25", "value": 215938000000.0}]}
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval)
    strong = Stream([GOOD], model="strong/m")
    events = run(Stream(["Nvidia's revenue for that year was $215.9 billion."], model="cheap/m"), strong)
    assert kinds(events) == ["retrieval", "delta", "done"] and not strong.iterated
    assert events[-1]["escalated"] is False


def test_an_uncited_dollar_figure_that_is_not_in_the_retrieved_context_is_still_escalated(monkeypatch):
    retrieval = {**RETRIEVAL, "metrics": [{"company": "Nvidia", "metric": "revenue", "period_start": "2025-01-27",
                                           "period_end": "2026-01-25", "value": 215938000000.0}]}
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval)
    events = run(Stream(["Nvidia's revenue for that year was $190 billion."], model="cheap/m"), Stream([GOOD], model="strong/m"))
    assert events[1]["event"] == "escalated" and events[1]["reasons"] == ["no_citation"]
