"""scripts/build_examples.py: the seeded example answers are the benchmark's hybrid runs of ONE snapshot, each with the
``checks`` the service would report for it (computed with the same ``answer_checks``), stamped with the prompt template."""

import importlib.util
import sys
from pathlib import Path

import pytest

from semigraph.retrieval.answerer import build_blocks, template_fingerprint
from semigraph.retrieval.verify import failed_check_names
from semigraph.serve import store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_examples.py"
spec = importlib.util.spec_from_file_location("build_examples", SCRIPT)
be = importlib.util.module_from_spec(spec)
sys.modules["build_examples"] = be
spec.loader.exec_module(be)

SNAP = "snap-20260924-7feaaf9bfe"
BENCH = [{"id": "N1", "type": "numeric", "q": "Revenue?"}, {"id": "U1", "type": "refusal", "q": "Samsung?"}]
CID = "0001045810-26-000021:I.1:0320"
REV = "xbrl:1045810:revenue:2026-01-25"
RETRIEVAL = {"edges": [], "risks": [], "temporal": [], "temporal_pairs": [],
             "metrics": [{"company": "Nvidia", "cik": 1045810, "metric": "revenue", "value": 215938000000.0,
                          "period_start": "2025-01-27", "period_end": "2026-01-25"}],
             "chunks": [{"chunk_id": CID, "text": "Nvidia depends on TSMC."}]}
_, CONTEXT, VALID_IDS = build_blocks(RETRIEVAL)
OK_ANSWER = f"Nvidia's revenue was $215.9 billion [{REV}]."


def run(id_, system="hybrid", answer=OK_ANSWER, cited=(REV,), hallucinated=(), **over):
    return {"id": id_, "system": system, "type": "x", "q": "q", "answer": answer, "cited": list(cited),
            "hallucinated": list(hallucinated), "context": CONTEXT, "valid_ids": sorted(VALID_IDS), **over}


def runs(**over):
    return [run("N1"), run("U1", answer="The context does not contain Samsung's revenue.", cited=()),
            run("N1", system="vector", answer="v")]


def test_examples_are_the_hybrid_runs_in_benchmark_order_and_carry_the_snapshot_and_the_template():
    doc = be.build_examples(list(reversed(runs())), BENCH, SNAP, source="test run")
    assert doc["snapshot_id"] == SNAP and doc["source"] == "test run" and doc["template_fingerprint"] == template_fingerprint()
    assert [e["id"] for e in doc["examples"]] == ["N1", "U1"]
    n1 = doc["examples"][0]
    assert {k: v for k, v in n1.items() if k != "checks"} == {
        "id": "N1", "type": "numeric", "question": "Revenue?", "answer": OK_ANSWER, "citations": [REV], "hallucinated": []}


def test_each_example_stores_the_checks_the_service_computes_for_that_answer():
    doc = be.build_examples(runs(), BENCH, SNAP, source="s")
    n1, u1 = doc["examples"]
    assert n1["checks"] == {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1,
                            "unmatched_numbers": [], "echoed_numbers": [], "pseudo_citations": [], "has_citation": True,
                            "is_refusal": False, "unsupported_removal_claim": False, "unsupported_removal_sentences": []}
    assert u1["checks"]["has_citation"] is False and u1["checks"]["is_refusal"] is True        # a refusal is not a failure
    assert failed_check_names(n1["checks"]) == [] == failed_check_names(u1["checks"])


def test_the_stored_checks_are_computed_with_the_same_function_and_the_question_of_the_benchmark():
    """A figure the QUESTION states is echoed, not grounded: the benchmark question is passed to answer_checks."""
    bench = [{"id": "N1", "type": "numeric", "q": "Did revenue reach $500 billion?"}]
    doc = be.build_examples([run("N1", answer=f"Yes, revenue reached $500 billion [{REV}].")], bench, SNAP, source="s")
    checks = doc["examples"][0]["checks"]
    assert checks["echoed_numbers"] == ["$500 billion"] and checks["numbers_grounded"] is False
    with pytest.raises(ValueError, match=r"N1: failed checks: ungrounded_number"):
        be.build_examples([run("N1", answer=f"Yes, revenue reached $500 billion [{REV}].")], bench, SNAP, source="s",
                          strict=True)


FAILING = [
    (f"Revenue was $190 billion [{REV}].", "ungrounded_number"),
    ("Nvidia depends on TSMC for advanced packaging.", "no_citation"),
    (f"Revenue was $215.9 billion [{REV}] [Reported Metrics].", "pseudo_citation"),
    (f"Nvidia dropped its export-control risk factor [{CID}].", "unsupported_removal_claim"),
]


@pytest.mark.parametrize("answer,name", FAILING)
def test_an_example_that_fails_its_own_checks_is_still_written_with_them_and_listed_as_refused(answer, name):
    """The spec: persist the checks, let seeding refuse. One failing temporal example must not abort the regeneration of
    the other nineteen."""
    doc = be.build_examples([run("N1", answer=answer, cited=())], BENCH[:1], SNAP, source="s")
    assert name in failed_check_names(doc["examples"][0]["checks"])
    ((refused_id, reason),) = be.refused_examples(doc)
    assert refused_id == "N1" and name in reason


@pytest.mark.parametrize("answer,name", FAILING)
def test_strict_mode_raises_on_a_failing_example_and_names_it(answer, name):
    with pytest.raises(ValueError, match=f"N1: failed checks: .*{name}"):
        be.build_examples([run("N1", answer=answer, cited=())], BENCH[:1], SNAP, source="s", strict=True)


def test_every_failing_example_is_listed_in_one_strict_error_and_the_clean_ones_are_not():
    bad = [run("N1", answer=f"Revenue was $190 billion [{REV}]."), run("U1", answer="Nvidia designs GPUs.", cited=())]
    with pytest.raises(ValueError) as e:
        be.build_examples(bad, BENCH, SNAP, source="s", strict=True)
    assert "N1: failed checks" in str(e.value) and "U1: failed checks" in str(e.value)
    ok = be.build_examples(runs(), BENCH, SNAP, source="s", strict=True)
    assert be.refused_examples(ok) == []


def test_the_command_line_writes_everything_lists_the_refusals_and_strict_writes_nothing(tmp_path, monkeypatch, capsys):
    import json

    jsonl = tmp_path / "runs.jsonl"
    jsonl.write_text("\n".join(json.dumps(r) for r in [run("N1", answer=f"Revenue was $190 billion [{REV}]."),
                                                      run("U1", answer="The context does not contain that.", cited=())]),
                     encoding="utf-8")
    bench = tmp_path / "benchmark.json"
    bench.write_text(json.dumps(BENCH), encoding="utf-8")
    monkeypatch.setattr(be, "BENCHMARK_PATH", bench)
    out = tmp_path / "examples.json"
    monkeypatch.setattr(sys, "argv", ["build_examples", "--runs", str(jsonl), "--snapshot", SNAP, "--out", str(out)])
    assert be.main() == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert [e["id"] for e in written["examples"]] == ["N1", "U1"] and written["template_fingerprint"] == template_fingerprint()
    assert "REFUSE to seed N1" in capsys.readouterr().err
    out.unlink()
    monkeypatch.setattr(sys, "argv", ["build_examples", "--runs", str(jsonl), "--snapshot", SNAP, "--out", str(out), "--strict"])
    assert be.main() == 1 and not out.exists() and "N1: failed checks" in capsys.readouterr().err


def test_a_run_without_its_context_or_valid_ids_is_refused_because_its_checks_cannot_be_computed():
    no_context = {k: v for k, v in run("N1").items() if k != "context"}
    with pytest.raises(ValueError, match="N1: the run has no context"):
        be.build_examples([no_context, run("U1", answer="The context does not contain that.", cited=())], BENCH, SNAP, source="s")
    no_ids = {k: v for k, v in run("N1").items() if k != "valid_ids"}
    with pytest.raises(ValueError, match="N1: the run has no context"):
        be.build_examples([no_ids, run("U1", answer="The context does not contain that.", cited=())], BENCH, SNAP, source="s")


def test_a_run_answered_under_the_legacy_context_template_is_refused():
    legacy = ("RELATIONSHIPS:\n(none)\n\nMETRICS:\n(none)\n\nACTIVE RISKS:\n(none)\n\nDROPPED RISK LINEAGES:\n(none)\n\n"
              "EXCERPTS:\n(none)")
    with pytest.raises(ValueError, match="N1: the run's context was not built with the current context template"):
        be.build_examples([run("N1", context=legacy)], BENCH[:1], SNAP, source="s")


def test_what_the_builder_writes_is_exactly_what_the_service_accepts_when_seeding(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: seen.append(kw["question"]))
    doc = be.build_examples(runs(), BENCH, SNAP, source="s")
    result = store.seed_examples(object(), doc["examples"], SNAP)
    assert result.seeded_ids == ("N1", "U1") and result.refused == () and seen == ["Revenue?", "Samsung?"]
    assert store.examples_match_template(doc) and store.examples_match_snapshot(doc, SNAP)


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


# --- examples from a DEPLOYED-path run (`semigraph eval-deployed`): the rows carry the service's own ``checks`` ---

CLEAN = {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1, "unmatched_numbers": [],
         "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
         "unsupported_removal_claim": False, "unsupported_removal_sentences": []}
REFUSAL = {**CLEAN, "numbers_checked": 0, "has_citation": False, "is_refusal": True}


def deployed(id_, answer=OK_ANSWER, cited=(REV,), checks=CLEAN, **over):
    return {"id": id_, "type": "x", "q": "q", "answer": answer, "cited": list(cited), "hallucinated": [],
            "checks": checks, "routed": "cheap", "escalated": False, **over}


def deployed_runs(**over):
    return [deployed("N1", **over), deployed("U1", answer="The context does not contain Samsung's revenue.", cited=(),
                                              checks=REFUSAL)]


def test_deployed_rows_become_examples_with_the_checks_the_service_reported():
    doc = be.build_examples(deployed_runs(), BENCH, SNAP, source="deployed run", deployed=True)
    assert [e["id"] for e in doc["examples"]] == ["N1", "U1"] and doc["examples"][0]["checks"] == CLEAN
    assert be.refused_examples(doc) == [] and doc["template_fingerprint"] == template_fingerprint()


def test_deployed_examples_are_exactly_what_the_service_accepts_when_seeding(monkeypatch):
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: None)
    doc = be.build_examples(deployed_runs(), BENCH, SNAP, source="s", deployed=True)
    assert store.seed_examples(object(), doc["examples"], SNAP).seeded_ids == ("N1", "U1")


def test_a_deployed_row_without_checks_cannot_become_an_example():
    with pytest.raises(ValueError, match="no checks"):
        be.build_examples([deployed("N1", checks=None), deployed("U1", checks=REFUSAL)], BENCH, SNAP, source="s", deployed=True)


def test_a_deployed_row_that_failed_its_checks_is_written_but_listed_as_refused():
    bad = {**CLEAN, "numbers_grounded": False, "unmatched_numbers": ["$190 billion"]}
    doc = be.build_examples(deployed_runs(checks=bad), BENCH, SNAP, source="s", deployed=True)
    assert [i for i, _ in be.refused_examples(doc)] == ["N1"]
    with pytest.raises(ValueError, match="N1"):
        be.build_examples(deployed_runs(checks=bad), BENCH, SNAP, source="s", deployed=True, strict=True)


# --- a question the correctness judge graded incorrect is not seeded (third deployed run: T1, T2, T3 were judged 0 of 3) ---

def test_excluded_ids_are_left_out_of_the_examples_and_recorded_with_the_reason():
    doc = be.build_examples(deployed_runs(), BENCH, SNAP, source="s", deployed=True, exclude={"N1": "judged incorrect: 0 of 3 votes"})
    assert [e["id"] for e in doc["examples"]] == ["U1"]
    assert doc["excluded"] == [{"id": "N1", "reason": "judged incorrect: 0 of 3 votes"}]


def test_the_ids_a_report_judged_incorrect_by_majority_are_the_ones_to_exclude():
    report = {"votes": 3, "judged": {"votes": {"A": 3, "B": 2, "C": 1, "D": 0}}}
    assert be.judged_incorrect(report) == {"C": "judged incorrect by the correctness judge: 1 of 3 votes correct",
                                           "D": "judged incorrect by the correctness judge: 0 of 3 votes correct"}


def test_excluding_an_unknown_id_is_an_error_not_a_silent_no_op():
    with pytest.raises(ValueError, match="not in the benchmark"):
        be.build_examples(deployed_runs(), BENCH, SNAP, source="s", deployed=True, exclude={"ZZ": "typo"})


# --- --exclude ID=REASON: an example the owner or a reviewer does not want pre-seeded, with the reason on record ---

def test_exclude_options_parse_into_an_id_to_reason_map_and_reject_a_malformed_one():
    assert be.parse_excludes(["R2=answer says only one company is affected", "T6=citation on a none-found sentence"]) == {
        "R2": "answer says only one company is affected", "T6": "citation on a none-found sentence"}
    for bad in ("R2", "=reason", "R2="):
        with pytest.raises(ValueError, match="ID=REASON"):
            be.parse_excludes([bad])
