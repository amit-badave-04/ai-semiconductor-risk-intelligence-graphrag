"""M2a R1 (docs/specs/m2a-answer-integrity.md): a saved example answer served from the cache carries the checks it was
seeded with, so the page's number badge says what was checked ("numbers matched the retrieved context" or "no figures to
check") instead of "numbers not checked". Only an example the service seeded gets them; a live cached answer and a row that
matches no seeded example are unchanged. Nothing is written: the checks come from the packaged examples file.
"""

import pytest
from test_serve_api import CACHED_DONE_BYTES, Q, client, fakes, parse_sse  # noqa: F401 - fixtures

from semigraph.artifacts import load_examples
from semigraph.serve import store

EXAMPLES = load_examples()["examples"]
WITH_FIGURES = next(e for e in EXAMPLES if e["checks"].get("numbers_checked", 0) >= 1)
WITHOUT_FIGURES = next(e for e in EXAMPLES if e["checks"].get("numbers_checked", 0) == 0)


def seed_row(fakes, example: dict, *, source: str = "benchmark", question: str | None = None) -> None:
    """A cached row as ``seed_examples`` writes it (store.put_answer through the backend's answer cache)."""
    q = question if question is not None else example["question"]
    fakes.backend.cache[store.cache_key(example["question"], "hybrid", "")] = {
        "question": q, "strategy": "hybrid", "answer": example["answer"], "citations": example["citations"],
        "hallucinated": example["hallucinated"], "source": source, "created_at": "2026-11-21T00:00:00+00:00"}


def ask(client, question: str) -> dict:
    events = parse_sse(client.post("/api/ask", json={"question": question}).text)
    assert len(events) == 1 and events[0]["event"] == "done" and events[0]["cached"] is True
    return events[0]


@pytest.mark.parametrize("example", [WITH_FIGURES, WITHOUT_FIGURES], ids=["with figures", "without figures"])
def test_a_seeded_example_served_from_the_cache_carries_its_stored_checks(client, fakes, example):
    client.app.state.example_ids = (example["id"],)
    seed_row(fakes, example)
    done = ask(client, example["question"])
    assert done["source"] == "benchmark"
    assert done["checks"] == example["checks"]


def test_an_example_the_service_did_not_seed_gets_no_checks(client, fakes):
    client.app.state.example_ids = ("SOME-OTHER-ID",)
    seed_row(fakes, WITH_FIGURES)
    assert "checks" not in ask(client, WITH_FIGURES["question"])


def test_a_benchmark_row_whose_question_matches_no_example_gets_no_checks(client, fakes):
    client.app.state.example_ids = (WITH_FIGURES["id"],)
    seed_row(fakes, WITH_FIGURES, question=WITH_FIGURES["question"] + " (edited)")
    assert "checks" not in ask(client, WITH_FIGURES["question"])


def test_a_live_answer_served_from_the_cache_is_unchanged(client, fakes):
    client.app.state.example_ids = tuple(e["id"] for e in EXAMPLES)
    client.post("/api/ask", json={"question": Q})                      # paid, populates the cache as a live answer
    assert client.post("/api/ask", json={"question": Q}).content == CACHED_DONE_BYTES


def test_without_a_seeded_list_every_packaged_example_counts_as_in_the_examples_listing(client, fakes):
    """No bootstrap state (``example_ids`` unset): the same rule as ``/api/examples``, which then lists every example."""
    assert getattr(client.app.state, "example_ids", None) is None
    seed_row(fakes, WITH_FIGURES)
    assert ask(client, WITH_FIGURES["question"])["checks"] == WITH_FIGURES["checks"]


@pytest.mark.parametrize("accepted, attached", [(frozenset(), False), (frozenset({WITH_FIGURES["id"]}), True)],
                         ids=["nothing seeded", "seeded, as bootstrap passes it"])
def test_the_seeded_list_is_honoured_as_the_frozenset_bootstrap_passes(client, fakes, accepted, attached):
    client.app.state.example_ids = accepted                     # main.bootstrap gives a frozenset; empty = nothing seeded
    seed_row(fakes, WITH_FIGURES)
    assert ("checks" in ask(client, WITH_FIGURES["question"])) is attached


def test_a_row_whose_answer_is_not_the_examples_answer_gets_no_checks(client, fakes):
    """Security review of R1, LOW 1: two releases with different examples files can share a snapshot and template, so the
    row's answer may come from the other release; the badge must describe the answer actually shown."""
    client.app.state.example_ids = (WITH_FIGURES["id"],)
    seed_row(fakes, WITH_FIGURES)
    fakes.backend.cache[store.cache_key(WITH_FIGURES["question"], "hybrid", "")]["answer"] = "A different answer [x]."
    assert "checks" not in ask(client, WITH_FIGURES["question"])


def test_a_request_worded_slightly_differently_is_served_the_seeded_row_with_its_checks(client, fakes):
    """The cache key normalises case, spacing and trailing punctuation; the checks follow the stored row, not the text."""
    client.app.state.example_ids = (WITH_FIGURES["id"],)
    seed_row(fakes, WITH_FIGURES)
    variant = "  " + WITH_FIGURES["question"].upper().rstrip("?.!") + "  "
    assert ask(client, variant)["checks"] == WITH_FIGURES["checks"]


def test_the_checks_sent_are_a_copy_not_the_shared_cached_object(client, fakes):
    from semigraph.serve import routes
    client.app.state.example_ids = (WITH_FIGURES["id"],)
    seed_row(fakes, WITH_FIGURES)
    ask(client, WITH_FIGURES["question"])
    shared = routes._example_checks()[WITH_FIGURES["question"]]
    sent = routes._seeded_checks(client.app.state, dict(fakes.backend.cache[store.cache_key(WITH_FIGURES["question"], "hybrid", "")]))
    assert sent["checks"] == shared[-1] and sent["checks"] is not shared[-1]
