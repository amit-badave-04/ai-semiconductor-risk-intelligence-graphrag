"""The ``checks`` object of the terminal ``done`` event: reported for EVERY answer, including the ones that cannot
escalate (routed straight to the strong model, or streamed live with no escalation model). A strong answer that fails a
check is reported, never silently passed; a cheap draft that fails one is escalated. No network, no model."""

import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval.answerer import answer, answer_stream

CID = "0001045810-24-000029:I.1A:0001"
REV = "xbrl:1045810:revenue:2026-01-25"
CLEAN = {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [], "pseudo_citations": []}
RETRIEVAL = {
    "anchors": {"Nvidia": 1045810}, "edges": [], "risks": [], "temporal": [], "temporal_pairs": [],
    "metrics": [{"company": "Nvidia", "cik": 1045810, "metric": "revenue", "value": 215938000000.0,
                 "period_start": "2025-01-27", "period_end": "2026-01-25"},
                {"company": "Nvidia", "cik": 1045810, "metric": "revenue", "value": 130497000000.0,
                 "period_start": "2024-01-29", "period_end": "2025-01-26"}],
    "chunks": [{"chunk_id": CID, "score": 0.9, "text": "One customer accounted for 19% of our revenue.",
                "source_url": "u"}],
}


class Stream:
    def __init__(self, text, model, finish="stop"):
        self.text, self.model, self.finish_reason = text, model, finish
        self.usage = {"prompt_tokens": 100, "completion_tokens": 10}
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        yield self.text


@pytest.fixture(autouse=True)
def retrieval(monkeypatch):
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: RETRIEVAL)


def run(question, draft, strong=None, *, escalation_model="strong/m"):
    return list(answer_stream(question, None, None, llm_stream=lambda p: draft, escalation_model=escalation_model,
                              escalation_stream=(lambda p: strong) if strong is not None else None))


def done_of(events):
    return events[-1]


def test_a_released_cheap_draft_reports_clean_checks():
    events = run("Who supplies HBM?", Stream(f"Revenue was $215.9 billion [{REV}], up 65.5% from the prior year [{REV}].", "cheap/m"))
    assert done_of(events)["escalated"] is False and done_of(events)["checks"] == CLEAN


def test_a_strong_routed_answer_cannot_escalate_so_it_reports_what_it_fails():
    cheap, strong = Stream("unused", "cheap/m"), Stream(f"Revenue was $190 billion [{CID}] [Reported Metrics].", "strong/m")
    events = run("How have Nvidia's risk disclosures changed?", cheap, strong)
    done = done_of(events)
    assert done["routed"] == "strong" and done["escalated"] is False and not cheap.iterated
    assert done["checks"] == {"citations_retrieved": True, "numbers_grounded": False,
                              "unmatched_numbers": ["$190 billion"], "pseudo_citations": ["Reported Metrics"]}


def test_a_strong_answer_citing_something_never_retrieved_reports_it():
    bogus = "0009999999-99-999999:I.1A:0001"
    events = run("How have Nvidia's risk disclosures changed?", Stream("x", "cheap/m"), Stream(f"Something [{bogus}].", "strong/m"))
    done = done_of(events)
    assert done["checks"]["citations_retrieved"] is False and done["hallucinated"] == [bogus]


def test_a_draft_with_a_pseudo_citation_is_escalated_and_the_strong_answers_checks_are_reported():
    draft = Stream(f"Revenue was $215.9 billion [{REV}] [Reported Metrics].", "cheap/m")
    events = run("Who supplies HBM?", draft, Stream(f"Revenue was $215.9 billion [{REV}].", "strong/m"))
    escalated = next(e for e in events if e["event"] == "escalated")
    assert escalated["reasons"] == ["pseudo_citation"]
    assert done_of(events)["escalated"] is True and done_of(events)["checks"] == CLEAN


def test_a_draft_with_an_ungrounded_percentage_is_escalated():
    events = run("Who supplies HBM?", Stream(f"Revenue grew 70.1% [{REV}].", "cheap/m"), Stream(f"Revenue grew 65.5% [{REV}].", "strong/m"))
    assert next(e for e in events if e["event"] == "escalated")["reasons"] == ["ungrounded_number"]
    assert done_of(events)["checks"] == CLEAN


def test_a_percentage_in_a_chunk_the_draft_cites_is_grounded_through_the_sources_map():
    events = run("Who supplies HBM?", Stream(f"One customer was 19% of revenue [{CID}].", "cheap/m"))
    assert done_of(events)["escalated"] is False and done_of(events)["checks"] == CLEAN


def test_a_percentage_in_a_chunk_the_draft_does_not_cite_is_not_grounded():
    text = f"One customer was 19% of revenue [{REV}]."
    events = run("Who supplies HBM?", Stream(text, "cheap/m"), Stream(f"One customer was 19% of revenue [{CID}].", "strong/m"))
    assert next(e for e in events if e["event"] == "escalated")["reasons"] == ["ungrounded_number"]


def test_a_dollar_value_echoed_from_the_question_is_not_flagged():
    events = run("Did revenue exceed $100 billion?", Stream(f"Yes: $215.9 billion [{REV}], above $100 billion.", "cheap/m"))
    assert done_of(events)["escalated"] is False and done_of(events)["checks"] == CLEAN


def test_without_an_escalation_model_the_live_answer_still_reports_checks():
    events = list(answer_stream("q", None, None, llm_stream=lambda p: Stream(f"Revenue was $190 billion [{REV}].", "m")))
    assert [e["event"] for e in events] == ["retrieval", "delta", "done"]
    assert done_of(events)["checks"]["numbers_grounded"] is False


def test_the_computed_year_over_year_line_grounds_the_percentage_and_the_dollar_change():
    text = f"Revenue rose 65.5%, or $85.441 billion, to $215.938 billion [{REV}]."
    assert done_of(run("Who supplies HBM?", Stream(text, "cheap/m")))["checks"] == CLEAN


def test_the_non_streaming_answer_reports_checks_too():
    out = answer("q", None, None, llm=lambda p: f"Revenue was $190 billion [{REV}].")
    assert out["checks"] == {"citations_retrieved": True, "numbers_grounded": False,
                             "unmatched_numbers": ["$190 billion"], "pseudo_citations": []}
    assert out["hallucinated"] == set() and out["cited"] == {REV}


def test_a_percentage_in_a_cited_temporal_headline_or_rule_title_is_grounded_for_a_strong_routed_answer(monkeypatch):
    """Temporal questions cannot escalate: a correct answer quoting a headline's figure must not report failed checks."""
    new_chunk = "0001045810-26-000021:I.1A:0350"
    retrieval_with = {
        **RETRIEVAL,
        "edges": [{"source": "Nvidia", "relation": "AFFECTED_BY", "target": "Rule adding a 50% licensing fee",
                   "status": "Active", "quote": None, "chunk_ids": None, "rule_id": "2026-19537", "date": "2026-03-12"}],
        "temporal": [{"company": "Nvidia", "cik": 1045810, "change": "new", "headline": "One customer reached 25% of revenue",
                      "older_headline": None, "unit_kind": "headline", "section_id": "I.1A", "decided_by": None,
                      "older_chunk_ids": [], "newer_chunk_ids": [new_chunk]}],
        "temporal_pairs": [{"company": "Nvidia", "cik": 1045810, "older_accession": "A", "older_form": "10-K",
                            "older_date": "2025-02-26", "newer_accession": "B", "newer_form": "10-K",
                            "newer_date": "2026-02-25", "totals": {"removed": 0, "new": 1, "reworded": 0}}],
    }
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval_with)
    text = (f"A new risk says one customer reached 25% of revenue [{new_chunk}]. Separately, a Federal Register rule "
            f"adds a 50% licensing fee [fr:2026-19537].")
    events = run("How have Nvidia's risk disclosures changed?", Stream("unused", "cheap/m"), Stream(text, "strong/m"))
    assert done_of(events)["routed"] == "strong" and done_of(events)["checks"] == CLEAN
