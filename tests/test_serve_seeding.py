"""M5 of the review: the answer cache and the seeded examples cannot replay an answer written under another prompt, or an
answer whose checks failed or were never computed. Pure: the store is faked at ``put_answer``, no Neo4j, no network."""

import hashlib
import logging
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import semigraph.retrieval.answerer as answerer_mod
import semigraph.serve.routes as routes
from semigraph.artifacts import load_examples
from semigraph.retrieval.answerer import template_fingerprint
from semigraph.serve import main as serve_main
from semigraph.serve import store
from semigraph.serve.guard import RateLimiter

CLEAN = {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1, "unmatched_numbers": [],
         "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
         "unsupported_removal_claim": False, "unsupported_removal_sentences": []}
REFUSAL = {**CLEAN, "numbers_checked": 0, "has_citation": False, "is_refusal": True}
SNAP = "snap-20260924-7feaaf9bfe"


def example(id_="N1", **over):
    return {"id": id_, "type": "numeric", "question": f"Question {id_}?", "answer": "A [c1].", "citations": ["c1"],
            "hallucinated": [], "checks": CLEAN, **over}


@pytest.fixture
def puts(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: seen.append(kw))
    return seen


# ---------------------------------------------------------------------------------- the cache key carries the template

def test_the_cache_key_carries_the_prompt_and_context_header_fingerprint():
    fingerprint = template_fingerprint()
    expected = hashlib.sha256(f"{fingerprint}|hybrid|what about nvidia".encode()).hexdigest()[:32]
    assert store.cache_key("What about Nvidia?", "hybrid") == expected
    with_snapshot = hashlib.sha256(f"{fingerprint}|{SNAP}|hybrid|what about nvidia".encode()).hexdigest()[:32]
    assert store.cache_key("What about Nvidia?", "hybrid", SNAP) == with_snapshot


def test_changing_the_answer_prompt_or_a_context_header_changes_every_cache_key(monkeypatch):
    """Rewritten by the review: ``test_cache_key_without_a_snapshot_is_the_legacy_key`` pinned the pre-review key, which
    is exactly what let a prompt change replay old answers."""
    before = store.cache_key("What about Nvidia?", "hybrid", SNAP)
    monkeypatch.setattr(answerer_mod, "ANSWER_PROMPT", answerer_mod.ANSWER_PROMPT + "\n- a new rule")
    after_prompt = store.cache_key("What about Nvidia?", "hybrid", SNAP)
    monkeypatch.undo()
    monkeypatch.setattr(answerer_mod, "CONTEXT_HEADERS", (*answerer_mod.CONTEXT_HEADERS[:-1], "\n\nEXCERPTS v2:\n"))
    after_header = store.cache_key("What about Nvidia?", "hybrid", SNAP)
    assert len({before, after_prompt, after_header}) == 3


def test_the_key_still_ignores_case_spacing_and_trailing_punctuation_and_still_separates_strategies():
    a = store.cache_key("What about  Nvidia?", "hybrid")
    assert a == store.cache_key("what about nvidia", "hybrid") and a != store.cache_key("what about nvidia", "vector")


def test_put_answer_and_the_route_compute_the_same_key(monkeypatch):
    captured = {}
    monkeypatch.setattr(store, "run_cypher", lambda driver, query, **p: captured.update(p))
    store.put_answer(object(), question="What about Nvidia?", strategy="hybrid", answer="A", citations=[], hallucinated=[],
                     snapshot_id=SNAP)
    assert captured["key"] == store.cache_key("What about Nvidia?", "hybrid", SNAP)


# ---------------------------------------------------------------------------------- seeding refuses what it cannot vouch for

def test_a_clean_example_and_a_zero_citation_refusal_are_seeded(puts):
    result = store.seed_examples(object(), [example("N1"), example("U1", answer="Not in the corpus.", citations=[],
                                                                   checks=REFUSAL)], SNAP)
    assert result.seeded_ids == ("N1", "U1") and result.refused == ()
    assert [p["question"] for p in puts] == ["Question N1?", "Question U1?"] and puts[0]["source"] == "benchmark"
    assert all(p["strategy"] == "hybrid" and p["snapshot_id"] == SNAP for p in puts)


@pytest.mark.parametrize("record,reason", [
    ({k: v for k, v in example().items() if k != "checks"}, "no stored checks"),
    (example(checks=None), "no stored checks"),
    (example(checks={}), "no stored checks"),
    (example(checks="ok"), "no stored checks"),
    (example(checks={"numbers_grounded": True}), "incomplete"),
    (example(checks={**CLEAN, "numbers_grounded": False, "unmatched_numbers": ["$9"]}), "ungrounded_number"),
    (example(checks={**CLEAN, "numbers_grounded": False, "echoed_numbers": ["$5"]}), "ungrounded_number"),
    (example(checks={**CLEAN, "citations_retrieved": False}), "citations_not_retrieved"),
    (example(checks={**CLEAN, "pseudo_citations": ["Excerpts"]}), "pseudo_citation"),
    (example(checks={**CLEAN, "unsupported_removal_claim": True}), "unsupported_removal_claim"),
    (example(checks={**CLEAN, "has_citation": False}), "no_citation"),
])
def test_an_example_whose_checks_are_missing_incomplete_or_failed_is_refused_with_the_reason(puts, record, reason):
    result = store.seed_examples(object(), [record, example("OK")], SNAP)
    assert result.seeded_ids == ("OK",) and [p["question"] for p in puts] == ["Question OK?"]
    ((refused_id, why),) = result.refused
    assert refused_id == record["id"] and reason in why


def test_one_refused_example_does_not_stop_the_others_and_every_reason_is_reported(puts):
    result = store.seed_examples(object(), [example("A", checks=None), example("B"), example("C", checks={**CLEAN, "has_citation": False})])
    assert result.seeded_ids == ("B",) and [r[0] for r in result.refused] == ["A", "C"]


def test_the_examples_document_must_match_the_running_prompt_template():
    assert store.examples_match_template({"template_fingerprint": template_fingerprint()}) is True
    assert store.examples_match_template({"template_fingerprint": "0000000000"}) is False
    assert store.examples_match_template({}) is False                              # a file that predates the fingerprint


# ---------------------------------------------------------------------------------- the service starts and says why

class Boot:
    """Fakes for everything ``bootstrap`` touches except its own example handling."""

    def __init__(self, monkeypatch, doc):
        self.puts = []
        monkeypatch.setattr(serve_main, "connect_with_retry", lambda settings: object())
        monkeypatch.setattr(serve_main, "apply_schema", lambda driver: None)
        monkeypatch.setattr(serve_main, "graph_stats", lambda driver: {"nodes": {}})
        monkeypatch.setattr(serve_main, "load_examples", lambda: doc)
        monkeypatch.setattr(serve_main, "Embedder", lambda: type("E", (), {"encode_query": lambda self, q: [0.0]})())
        monkeypatch.setattr(store, "ensure_indexes", lambda driver: None)
        monkeypatch.setattr(store, "current_snapshot", lambda driver: {"id": SNAP, "as_of": "2026-09-24"})
        monkeypatch.setattr(store, "put_answer", lambda driver, **kw: self.puts.append(kw))

    def run(self):
        return serve_main.bootstrap(object())


def test_the_committed_examples_match_the_current_template_and_seed_exactly_the_ones_whose_checks_pass(monkeypatch, caplog):
    """examples.json is regenerated from the deployed-path benchmark run (scripts/build_examples.py --deployed): its template
    fingerprint is the running build's, every example carries the service's own checks, the ones that fail them are refused (and
    named in the log), and the questions the correctness judge rejected are recorded under excluded and are not in the file.
    If this fails after you changed answer.txt or CONTEXT_HEADERS: regenerate the file (it needs a paid deployed run)."""
    doc = load_examples()
    assert store.examples_match_template(doc), "answer.txt / CONTEXT_HEADERS changed since examples.json was generated"
    boot = Boot(monkeypatch, {**doc, "snapshot_id": SNAP})
    caplog.set_level(logging.INFO, logger="semigraph.serve.main")
    driver, embedder, stats, snapshot, example_ids = boot.run()
    refused = {e["id"] for e in doc["examples"] if store.refusal_reason(e)}
    assert example_ids == frozenset(e["id"] for e in doc["examples"]) - refused and len(boot.puts) == len(example_ids)
    assert example_ids and all(e["checks"] for e in doc["examples"])
    assert not ({x["id"] for x in doc.get("excluded", [])} & {e["id"] for e in doc["examples"]})
    messages = " | ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "template" not in messages and all(f"example {i} refused" in messages for i in refused)


def test_examples_of_the_current_template_are_seeded_and_only_the_valid_ones_are_listed(monkeypatch, caplog):
    doc = {"source": "s", "snapshot_id": SNAP, "template_fingerprint": template_fingerprint(),
           "examples": [example("N1"), example("N2", checks=None), example("N3", checks={**CLEAN, "has_citation": False})]}
    boot = Boot(monkeypatch, doc)
    caplog.set_level(logging.INFO, logger="semigraph.serve.main")
    *_, example_ids = boot.run()
    assert [p["question"] for p in boot.puts] == ["Question N1?"] and example_ids == frozenset({"N1"})
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("N2" in w and "no stored checks" in w for w in warnings)
    assert any("N3" in w and "no_citation" in w for w in warnings)
    assert any("seeded 1 of 3" in r.getMessage() for r in caplog.records)


def test_examples_from_another_snapshot_are_still_not_seeded(monkeypatch, caplog):
    doc = {"source": "s", "snapshot_id": "snap-20200101-0000000000", "template_fingerprint": template_fingerprint(),
           "examples": [example("N1")]}
    boot = Boot(monkeypatch, doc)
    *_, example_ids = boot.run()
    assert boot.puts == [] and example_ids == frozenset()


# ---------------------------------------------------------------------------------- /api/examples lists only what is cached

class Settings:
    read_rate_limit_per_minute = 50
    stats_cache_seconds = 0
    client_ip_header = ""


def make_client(example_ids=None):
    app = FastAPI()
    app.include_router(routes.router)
    app.state.settings = Settings()
    app.state.driver = object()
    app.state.read_rate_limiter = RateLimiter(50, 60)
    app.state.answer_slots = threading.BoundedSemaphore(1)
    if example_ids is not None:
        app.state.example_ids = example_ids
    return TestClient(app)


def test_the_examples_endpoint_lists_only_the_accepted_examples():
    every = load_examples()["examples"]
    accepted = frozenset({every[0]["id"], every[2]["id"]})
    body = make_client(accepted).get("/api/examples").json()
    assert [e["id"] for e in body["examples"]] == [every[0]["id"], every[2]["id"]]
    assert set(body["examples"][0]) == {"id", "type", "question"}


def test_with_no_accepted_example_the_endpoint_lists_none_so_no_click_becomes_a_paid_call():
    body = make_client(frozenset()).get("/api/examples").json()
    assert body["examples"] == [] and "source" in body


def test_without_bootstrap_state_the_endpoint_lists_every_packaged_example():
    assert len(make_client().get("/api/examples").json()["examples"]) == len(load_examples()["examples"])
