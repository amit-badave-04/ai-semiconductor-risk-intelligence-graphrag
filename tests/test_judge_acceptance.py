"""scripts/judge_acceptance.py: the instrument acceptance test (the correctness judge must grade a frozen set of ADVERSARIAL PROBES
as expected: wrong answers incorrect, answers consistent with the notes correct, by majority of the votes).

Nothing here calls a model: the paid path takes an injected fake judge, and the default (dry-run) path must not reach one.
"""

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import temporalfix as fx

import semigraph.eval.runner as runner
from semigraph.eval import gold

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "judge_acceptance.py"
spec = importlib.util.spec_from_file_location("judge_acceptance", SCRIPT)
ja = importlib.util.module_from_spec(spec)
sys.modules["judge_acceptance"] = ja
spec.loader.exec_module(ja)

A1, A2 = "0001045810-25-000023:I.1A:0257", "0001045810-26-000021:I.1A:0344"
Q4 = "Did AMD remove any risk factors between its FY2024 and FY2025 annual reports?"
Q5 = "Did Meta remove any risk factors between its FY2024 and FY2025 annual reports?"


def _probe(pid, notes_from, question, expected, ids=(A1,)):
    return {"id": pid, "kind": "test", "source": "test", "notes_from": notes_from, "question": question, "expected_correct": expected,
            "basis": "the notes say so", "valid_ids": list(ids), "answer": f"MARK[{pid}] Data privacy risk was dropped [{ids[0]}]."}


# two probes on the SAME question (T4) with different answers: verdicts must never be keyed by question id
PROBES = [
    _probe("legacy-T1", {"temporal_legacy": "T1"}, "Did Nvidia stop disclosing any risk factors?", False),
    _probe("legacy-T3", {"temporal_legacy": "T3"}, "How has Nvidia's risk profile evolved?", False, ids=(A1, A2)),
    _probe("real-T4", {"benchmark": "T4"}, Q4, True),
    _probe("syn-T4-wrong", {"benchmark": "T4"}, Q4, False),
    _probe("real-T5", {"benchmark": "T5"}, Q5, True),
]
WRONG = [p["id"] for p in PROBES if not p["expected_correct"]]
RIGHT = [p["id"] for p in PROBES if p["expected_correct"]]
BENCH = [{"id": "T4", "type": "temporal", "q": Q4, "judge_notes": "NOTES FOR T4: NO whole risk factor was removed"},
         {"id": "T5", "type": "temporal", "q": Q5, "judge_notes": "NOTES FOR T5: no risk factor is new"},
         {"id": "T1", "type": "temporal", "q": "q", "judge_notes": "BENCH NOTES ONE"},
         {"id": "T3", "type": "temporal", "q": "q", "judge_notes": "BENCH NOTES THREE"}]


def mark(pid: str) -> str:
    return f"MARK[{pid}]"


class FakeJudge:
    """llm_json-compatible fake. ``correct`` = ids of probes whose answer it grades correct (it finds the answer by its marker, since
    two probes share a question); ``fail_on`` raises for that probe's prompt (first call only when ``fail_first_only``)."""

    def __init__(self, correct=(), fail_on=(), fail_first_only=False):
        self.correct, self.fail_on, self.fail_first_only, self.calls, self.failed = tuple(correct), tuple(fail_on), fail_first_only, [], 0

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append((prompt, kw))
        if any(mark(f) in prompt for f in self.fail_on) and (not self.fail_first_only or not self.failed):
            self.failed += 1
            raise RuntimeError("provider down")
        return model_cls(correct=any(mark(c) in prompt for c in self.correct), reason=f"scripted {len(self.calls)}",
                         unsupported_claims=["dropped privacy risk"])


class ScriptedJudge:
    """Grades each probe's answer by a per-probe list of votes (True = correct), consumed call by call; missing = incorrect."""

    def __init__(self, script):
        self.script, self.calls = {pid: list(v) for pid, v in script.items()}, []

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append(prompt)
        pid = next(p for p in (q["id"] for q in PROBES) if mark(p) in prompt)
        votes = self.script.get(pid) or [False]
        return model_cls(correct=votes.pop(0) if len(votes) > 1 else votes[0], reason="scripted", unsupported_claims=[])


def _write_temporal(tmp_path, world):
    """Run the real builder on the fixture gold so the acceptance script reads a genuine temporal questions file."""
    spec_b = importlib.util.spec_from_file_location("btq_for_acceptance", ROOT / "scripts" / "build_temporal_questions.py")
    btq = importlib.util.module_from_spec(spec_b)
    sys.modules["btq_for_acceptance"] = btq
    spec_b.loader.exec_module(btq)
    out = tmp_path / "temporal.json"
    assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
                     "--xbrl-dir", str(world["xbrl_dir"]), "--flagship-pair", fx.FLAG, "--out", str(out)]) == 0
    return out


def _freeze(path, probes, as_of="2026-09-24"):
    doc = {"kind": "judge_probes", "as_of": as_of, "description": "test probes", "probes": probes}
    return gold.freeze(doc, path)


def _args(tmp_path, probes_path, temporal, gold_path, bench):
    return ["--probes", str(probes_path), "--temporal", str(temporal), "--gold", str(gold_path), "--benchmark", str(bench),
            "--report", str(tmp_path / "report.json")]


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    world = fx.build(tmp_path)
    temporal = _write_temporal(tmp_path, world)
    bench = tmp_path / "bench.json"
    bench.write_text(json.dumps(BENCH), encoding="utf-8")
    probes = tmp_path / "probes.json"
    digest = _freeze(probes, PROBES)
    monkeypatch.setattr(ja, "PINNED_PROBES_SHA256", digest)      # the probes of the fixture are the pinned set
    return SimpleNamespace(base=_args(tmp_path, probes, temporal, world["gold"], bench), report=tmp_path / "report.json",
                           probes=probes, digest=digest, tmp=tmp_path, temporal=temporal, gold=world["gold"], bench=bench)


def _report(s):
    return json.loads(s.report.read_text(encoding="utf-8"))


# --- the shipped probes file ----------------------------------------------------------------------------------------------

SHIPPED = ROOT / "artifacts" / "gold" / "judge_probes.json"


def test_the_shipped_probes_file_is_frozen_pinned_and_holds_the_designed_probe_set():
    assert gold.verify_frozen(SHIPPED)                                           # the gold-file convention
    doc = ja.load_probes(SHIPPED, pin=ja.PINNED_PROBES_SHA256)                  # hash + pin + shape
    by_id = {p["id"]: p for p in doc["probes"]}
    wrong = {"legacy-T1", "legacy-T3", "real-T7", "real-T12", "syn-T5-removed-advertising", "syn-T7-none-removed-hedged",
             "syn-T8-removed-new-item", "syn-T11-stopped-reworded", "syn-T4-carried-over-unchanged"}
    right = {"real-T8", "real-T10", "real-T11"}          # real T4/T6/T9 are dropped (their counts differ from the notes'), and real-T5 was dropped after review
    assert {i for i, p in by_id.items() if not p["expected_correct"]} == wrong
    assert {i for i, p in by_id.items() if p["expected_correct"]} == right
    assert doc["as_of"] == "2026-09-24"
    (dropped,) = doc["dropped_after_review"]
    assert dropped["id"] == "real-T5" and "1 of 3 votes" in dropped["observed"]      # the drop and why are on the record


def test_every_shipped_probe_is_self_consistent_and_cites_only_its_valid_ids():
    for p in ja.load_probes(SHIPPED, pin=None)["probes"]:
        cited = set(runner.CITE_RE.findall(p["answer"]))
        assert cited <= set(p["valid_ids"]), p["id"]                             # ids the service verified, as for a served answer
        assert len(p["answer"]) < runner.MAX_JUDGE_ANSWER_CHARS, p["id"]          # the judge sees the whole answer
        assert len(p["basis"]) > 60 and p["kind"] in {"legacy_wrong", "deployed_real", "synthetic"}, p["id"]


def test_the_shipped_probes_resolve_against_the_real_benchmark_and_temporal_notes():
    args = ja._parse(["--probes", str(SHIPPED), "--temporal", str(ROOT / ja.DEFAULT_TEMPORAL), "--gold", str(ROOT / ja.DEFAULT_GOLD),
                      "--benchmark", str(ROOT / ja.DEFAULT_BENCHMARK)])
    doc, items, rows, as_of = ja._load_cases(args)
    assert len(items) == len(rows) == 12 and as_of == "2026-09-24"
    notes = {i["id"]: i["judge_notes"] for i in items}
    temporal = json.loads((ROOT / ja.DEFAULT_TEMPORAL).read_text(encoding="utf-8"))
    assert notes["legacy-T1"] == temporal["legacy_notes"]["T1"] and notes["legacy-T3"] == temporal["legacy_notes"]["T3"]
    assert "NO whole risk factor was removed" in notes["real-T8"] and "YES, exactly 1" in notes["real-T7"]
    assert all(n.strip() for n in notes.values())


DEPLOYED_RUNS = ROOT / "data" / "processed" / "eval_runs.v2-deployed.jsonl"


@pytest.mark.skipif(not DEPLOYED_RUNS.exists(), reason="the deployed run rows are not in this checkout (data/processed is not versioned)")
def test_the_real_deployed_probes_are_verbatim_copies_of_the_deployed_rows_and_synthetic_ids_come_from_them():
    rows = {r["id"]: r for r in (json.loads(x) for x in DEPLOYED_RUNS.read_text(encoding="utf-8").splitlines() if x.strip())}
    for p in ja.load_probes(SHIPPED, pin=None)["probes"]:
        if p["kind"] == "legacy_wrong":
            continue
        row = rows[p["notes_from"]["benchmark"]]
        assert p["question"] == row["q"], p["id"]
        if p["kind"] == "deployed_real":
            assert p["answer"] == row["answer"] and p["valid_ids"] == row["valid_ids"], p["id"]
        else:
            assert set(p["valid_ids"]) <= set(row["valid_ids"]), p["id"]


# --- the probes file: hash, pin, shape ---------------------------------------------------------------------------------------

def test_the_probes_hash_is_the_gold_convention_over_the_document_without_its_own_hash(setup):
    doc = json.loads(setup.probes.read_text(encoding="utf-8"))
    assert doc["sha256"] == ja.probes_sha256(doc) == setup.digest and gold.verify_frozen(setup.probes)


def test_a_tampered_probe_is_refused(setup, capsys):
    doc = json.loads(setup.probes.read_text(encoding="utf-8"))
    doc["probes"][0]["expected_correct"] = True                       # flipped to make a lenient judge pass, hash not updated
    setup.probes.write_text(json.dumps(doc), encoding="utf-8")
    assert ja.main(setup.base) == 2
    err = capsys.readouterr().err
    assert "no longer hashes" in err and "edited" in err


def test_a_tampered_probe_that_was_rehashed_is_refused_by_the_pin(setup, capsys):
    doc = json.loads(setup.probes.read_text(encoding="utf-8"))
    probes = doc["probes"]
    probes[0] = {**probes[0], "expected_correct": True}
    _freeze(setup.probes, probes)                                     # edited AND re-frozen: the in-file hash is valid again
    assert gold.verify_frozen(setup.probes)
    assert ja.main(setup.base) == 2
    err = capsys.readouterr().err
    assert "not the pinned probe set" in err and "PINNED_PROBES_SHA256" in err
    assert ja.main([*setup.base, "--allow-unpinned-probes"]) == 0     # only for a probe set under review


def test_an_unpinned_probe_set_is_reported_as_such(setup, capsys):
    ja.main([*setup.base, "--allow-unpinned-probes", "--go"], judge=FakeJudge(correct=RIGHT))
    assert _report(setup)["probes_pinned"] is True                    # the fixture pin matches: pinned even with the override flag
    probes = json.loads(setup.probes.read_text(encoding="utf-8"))["probes"]
    _freeze(setup.probes, [*probes, _probe("extra", {"benchmark": "T5"}, Q5, True)])
    assert ja.main([*setup.base, "--allow-unpinned-probes", "--go"], judge=FakeJudge(correct=RIGHT + ["extra"])) == 0
    assert _report(setup)["probes_pinned"] is False and "NOT pinned" in capsys.readouterr().out


def test_a_missing_probes_file_is_a_clear_error(setup, capsys):
    args = [a if a != str(setup.probes) else str(setup.tmp / "nope.json") for a in setup.base]
    assert ja.main(args) == 2 and "judge_probes.json" in capsys.readouterr().err


def _mutated(setup, mutate):
    probes = json.loads(setup.probes.read_text(encoding="utf-8"))["probes"]
    _freeze(setup.probes, mutate(probes))


@pytest.mark.parametrize("mutate,message", [
    (lambda ps: [{k: v for k, v in ps[0].items() if k != "basis"}, *ps[1:]], "lacks basis"),
    (lambda ps: [ps[0], {**ps[1], "id": ps[0]["id"]}, *ps[2:]], "duplicate probe id"),
    (lambda ps: [{**p, "expected_correct": False} for p in ps], "at least one expected-correct"),
    (lambda ps: [{**p, "expected_correct": True} for p in ps], "at least one expected-correct"),
    (lambda ps: [{**ps[0], "notes_from": {"somewhere": "T1"}}, *ps[1:]], "notes_from"),
    (lambda ps: [{**ps[0], "valid_ids": "not-a-list"}, *ps[1:]], "valid_ids"),
    (lambda ps: [{**ps[0], "expected_correct": "yes"}, *ps[1:]], "true/false"),
    (lambda ps: [], "no probes"),
])
def test_a_malformed_or_one_sided_probe_set_is_refused(setup, capsys, mutate, message):
    _mutated(setup, mutate)
    assert ja.main([*setup.base, "--allow-unpinned-probes"]) == 2
    assert message in capsys.readouterr().err


def test_a_benchmark_probe_whose_question_is_not_the_benchmarks_is_refused(setup, capsys):
    _mutated(setup, lambda ps: [*ps[:2], {**ps[2], "question": "some other question?"}, *ps[3:]])
    assert ja.main([*setup.base, "--allow-unpinned-probes"]) == 2 and "question" in capsys.readouterr().err


# --- grading notes -------------------------------------------------------------------------------------------------------------

def test_the_notes_are_the_legacy_notes_the_temporal_file_carries_for_that_question(setup):
    doc = json.loads(setup.temporal.read_text(encoding="utf-8"))
    notes = ja.notes_from_temporal(doc, "T1")
    assert notes == doc["legacy_notes"]["T1"]
    assert "NO whole risk factor was removed" in notes and "exactly 1" in notes and "Notified Advanced Computing" in notes
    assert notes.count("Correct answer:") == 1               # never several questions' verdicts stitched together
    with pytest.raises(ValueError, match="T9"):
        ja.notes_from_temporal(doc, "T9")


def test_the_acceptance_notes_are_byte_equal_to_the_notes_production_scoring_will_use(setup, tmp_path):
    """One source of truth: after `build_temporal_questions.py --merge`, the benchmark's T1/T3 judge_notes ARE the acceptance notes."""
    spec_b = importlib.util.spec_from_file_location("btq_merge", ROOT / "scripts" / "build_temporal_questions.py")
    btq = importlib.util.module_from_spec(spec_b)
    sys.modules["btq_merge"] = btq
    spec_b.loader.exec_module(btq)
    (tmp_path / "w").mkdir()
    world = fx.build(tmp_path / "w")
    bench = tmp_path / "bench2.json"
    bench.write_bytes(json.dumps([{"id": "T1", "type": "temporal", "q": "q", "judge_notes": "old"},
                                  {"id": "T3", "type": "temporal", "q": "q", "judge_notes": "old"}], indent=2).encode("utf-8"))
    out = tmp_path / "tq2.json"
    assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
                     "--xbrl-dir", str(world["xbrl_dir"]), "--flagship-pair", fx.FLAG, "--out", str(out), "--merge",
                     "--benchmark", str(bench)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    merged = {e["id"]: e for e in json.loads(bench.read_text(encoding="utf-8"))}
    for tid in ("T1", "T3"):
        assert ja.notes_from_temporal(doc, tid) == merged[tid]["judge_notes"]


def test_a_missing_temporal_file_is_a_clear_error_that_points_at_the_builder(setup, capsys):
    args = [a if a != str(setup.temporal) else "/nope/temporal.json" for a in setup.base]
    assert ja.main(args) == 2
    assert "build_temporal_questions.py" in capsys.readouterr().err


def test_a_temporal_file_that_does_not_match_the_frozen_gold_is_refused(setup, capsys):
    doc = json.loads(setup.temporal.read_text(encoding="utf-8"))
    doc["gold_sha256"] = "0" * 64
    setup.temporal.write_text(json.dumps(doc), encoding="utf-8")
    assert ja.main(setup.base) == 2 and "gold" in capsys.readouterr().err


def test_legacy_probes_resolve_their_notes_from_the_temporal_file_and_the_others_from_the_benchmark(setup):
    judge = FakeJudge()
    assert ja.main([*setup.base, "--go", "--votes", "1"], judge=judge) == 1          # fake grades all incorrect: the RIGHT ones fail
    prompts = {pid: next(p for p, _ in judge.calls if mark(pid) in p) for pid in WRONG + RIGHT}
    temporal = json.loads(setup.temporal.read_text(encoding="utf-8"))["legacy_notes"]
    assert temporal["T1"] in prompts["legacy-T1"] and temporal["T3"] in prompts["legacy-T3"]
    assert "Notified Advanced Computing" in prompts["legacy-T1"] and "BENCH NOTES" not in prompts["legacy-T1"]
    assert "NOTES FOR T4" in prompts["real-T4"] and "NOTES FOR T4" in prompts["syn-T4-wrong"] and "NOTES FOR T5" in prompts["real-T5"]


def test_notes_of_the_legacy_probes_can_come_from_the_merged_benchmark_instead(setup, capsys):
    args = [a if a != str(setup.temporal) else "/nope/temporal.json" for a in setup.base]     # the temporal file is not consulted
    assert ja.main([*args, "--notes", "benchmark", "--show-prompts"]) == 0
    out = capsys.readouterr().out
    assert "BENCH NOTES ONE" in out and "BENCH NOTES THREE" in out and "NOTES FOR T4" in out


# --- dry run (the default) ------------------------------------------------------------------------------------------------------

def test_the_default_is_a_dry_run_that_prints_lengths_and_the_cost_and_makes_zero_judge_calls(setup, capsys, monkeypatch):
    monkeypatch.setattr(runner, "llm_json", lambda *a, **k: pytest.fail("the dry run reached the judge"))

    class Tripwire(FakeJudge):
        def __call__(self, *a, **k):
            pytest.fail("the dry run called the injected judge")

    judge = Tripwire()
    assert ja.main(setup.base, judge=judge) == 0
    out = capsys.readouterr().out
    assert judge.calls == [] and not setup.report.exists()
    assert "dry run" in out and runner.JUDGE_PROMPT_VERSION in out and "2026-09-24" in out and "5 probes" in out
    assert all(p["id"] in out for p in PROBES) and out.count("characters") >= len(PROBES)
    assert "15 judge calls" in out and re.search(r"worst case \$0\.\d\d", out)
    assert "GRADING NOTES" not in out                              # prompts are only printed with --show-prompts


def test_show_prompts_prints_every_rendered_prompt(setup, capsys):
    assert ja.main([*setup.base, "--show-prompts"]) == 0
    out = capsys.readouterr().out
    assert out.count("GRADING NOTES (verified facts)") == len(PROBES)
    assert all(mark(p["id"]) in out for p in PROBES) and A1 in out and "2026-09-24" in out


def test_the_estimate_scales_up_for_prompts_longer_than_the_per_call_bound():
    bound = ja.bakeoff.JUDGE_CALL_USD
    assert ja.estimate_cost([ja.BOUND_PROMPT_CHARS], 3) == pytest.approx(3 * bound)
    assert ja.estimate_cost([ja.BOUND_PROMPT_CHARS * 2], 3) == pytest.approx(6 * bound)
    assert ja.estimate_cost([100], 3) == pytest.approx(3 * bound)


def test_as_of_defaults_to_the_probes_file_and_can_be_overridden_by_an_iso_date(setup, capsys):
    ja.main([*setup.base, "--go", "--as-of", "2026-01-31"], judge=FakeJudge(correct=RIGHT))
    assert _report(setup)["as_of"] == "2026-01-31"
    assert ja.main([*setup.base, "--as-of", "yesterday"]) == 2 and "ISO date" in capsys.readouterr().err
    ja.main([*setup.base, "--go"], judge=FakeJudge(correct=RIGHT))
    assert _report(setup)["as_of"] == "2026-09-24"


# --- the paid path (only with --go, and only through judge_open) -----------------------------------------------------------------

def test_the_oracle_judge_is_a_pass_and_the_report_records_the_instrument(setup, capsys):
    judge = FakeJudge(correct=RIGHT)
    assert ja.main([*setup.base, "--go"], judge=judge) == 0
    assert len(judge.calls) == 15 and all(kw["max_tokens"] == runner.JUDGE_MAX_TOKENS for _, kw in judge.calls)
    doc = _report(setup)
    assert doc["verdict"] == "PASS" and doc["as_of"] == "2026-09-24" and doc["votes"] == 3 and doc["n_probes"] == 5
    assert doc["judge_prompt_version"] == runner.JUDGE_PROMPT_VERSION == "cj-v3"
    assert doc["prompt_sha256"] == hashlib.sha256(runner.JUDGE_PROMPT.encode("utf-8")).hexdigest()
    assert doc["probes_sha256"] == setup.digest and doc["probes_pinned"] is True and doc["model"]
    assert doc["leniency_failures"] == [] and doc["strictness_failures"] == [] and doc["errors"] == {}
    assert set(doc["results"]) == {p["id"] for p in PROBES} and doc["estimated_cost_usd"] > 0
    first = doc["results"]["legacy-T1"]
    assert first["expected_correct"] is False and first["votes_correct"] == 0 and first["outcome"] == "ok"
    assert first["verdicts"][0]["unsupported_claims"] == ["dropped privacy risk"] and len(first["verdicts"]) == 3
    assert set(doc["notes_sha256"]) == {p["id"] for p in PROBES}
    assert "acceptance: PASS" in capsys.readouterr().out


def test_probes_on_the_same_question_are_judged_apart(setup):
    judge = FakeJudge(correct=["real-T4"] + ["real-T5"])
    assert ja.main([*setup.base, "--go"], judge=judge) == 0
    res = _report(setup)["results"]
    assert res["real-T4"]["votes_correct"] == 3 and res["syn-T4-wrong"]["votes_correct"] == 0     # same question, opposite verdicts


def test_a_too_lenient_judge_fails_and_names_every_leniency_failure(setup, capsys):
    """A judge that grades everything correct accepts the wrong answers: the serious failure, and it is listed first."""
    assert ja.main([*setup.base, "--go"], judge=FakeJudge(correct=WRONG + RIGHT)) == 1
    out = capsys.readouterr().out
    doc = _report(setup)
    assert doc["verdict"] == "FAIL" and doc["leniency_failures"] == WRONG and doc["strictness_failures"] == []
    assert "LENIENCY FAILURES" in out and "STRICTNESS FAILURES" not in out and "acceptance: FAIL" in out
    assert all(f"{pid}: 3 of 3 votes 'correct' (expected INCORRECT)" in out for pid in WRONG)
    assert doc["results"]["legacy-T1"]["outcome"] == "leniency_failure"


def test_a_too_strict_judge_fails_and_names_every_strictness_failure(setup, capsys):
    """A judge that grades everything incorrect punishes the required hedged wording: the answers consistent with the notes fail."""
    assert ja.main([*setup.base, "--go"], judge=FakeJudge(correct=())) == 1
    out = capsys.readouterr().out
    doc = _report(setup)
    assert doc["verdict"] == "FAIL" and doc["strictness_failures"] == RIGHT and doc["leniency_failures"] == []
    assert "STRICTNESS FAILURES" in out and "LENIENCY FAILURES" not in out
    assert all(f"{pid}: 0 of 3 votes 'correct' (expected correct)" in out for pid in RIGHT)


def test_leniency_failures_are_printed_before_strictness_failures(setup, capsys):
    judge = FakeJudge(correct=["syn-T4-wrong", "real-T4"])          # syn-T4-wrong wrongly correct; real-T5 wrongly incorrect
    assert ja.main([*setup.base, "--go"], judge=judge) == 1
    out = capsys.readouterr().out
    assert 0 <= out.index("LENIENCY FAILURES") < out.index("STRICTNESS FAILURES")
    doc = _report(setup)
    assert doc["leniency_failures"] == ["syn-T4-wrong"] and doc["strictness_failures"] == ["real-T5"]


def test_the_verdict_of_a_probe_is_the_majority_of_its_votes(setup):
    one_of_three = ScriptedJudge({"syn-T4-wrong": [True, False, False], "real-T5": [True], "real-T4": [True]})
    assert ja.main([*setup.base, "--go"], judge=one_of_three) == 0                   # 1 of 3 'correct' on a wrong answer: incorrect
    two_of_three = ScriptedJudge({"syn-T4-wrong": [True, True, False], "real-T5": [True], "real-T4": [True]})
    assert ja.main([*setup.base, "--go"], judge=two_of_three) == 1                   # 2 of 3 'correct': too lenient
    assert _report(setup)["leniency_failures"] == ["syn-T4-wrong"]
    strict_split = ScriptedJudge({"real-T4": [True, False, False], "real-T5": [True]})
    assert ja.main([*setup.base, "--go"], judge=strict_split) == 1                   # 1 of 3 'correct' on a consistent answer
    assert _report(setup)["strictness_failures"] == ["real-T4"]


def test_an_errored_vote_makes_the_result_inconclusive_never_a_pass(setup, capsys):
    """An errored vote counts as 'not correct' in judge_open; for the expected-incorrect probes that would be a false PASS, so any
    error is INCONCLUSIVE with its own exit code."""
    judge = FakeJudge(correct=RIGHT, fail_on=("legacy-T1",), fail_first_only=True)
    assert ja.main([*setup.base, "--go"], judge=judge) == 3
    doc = _report(setup)
    assert doc["verdict"] == "INCONCLUSIVE" and doc["errors"] == {"legacy-T1": 1} and doc["results"]["legacy-T1"]["outcome"] == "errored"
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out and "legacy-T1: 1 of 3 votes errored" in out and "provider down" in out


def test_an_error_on_an_expected_correct_probe_is_inconclusive_not_a_strictness_failure(setup):
    judge = FakeJudge(correct=RIGHT, fail_on=("real-T5",))
    assert ja.main([*setup.base, "--go"], judge=judge) == 3
    doc = _report(setup)
    assert doc["verdict"] == "INCONCLUSIVE" and doc["strictness_failures"] == []


def test_go_and_dry_run_together_are_refused(setup):
    with pytest.raises(SystemExit):
        ja.main([*setup.base, "--go", "--dry-run"])


def test_zero_votes_is_refused(setup, capsys):
    assert ja.main([*setup.base, "--votes", "0"]) == 2 and "--votes" in capsys.readouterr().err


# --- the instrument's version --------------------------------------------------------------------------------------------------

# The judge prompt is the instrument: its version must change whenever its text changes (the version is recorded in the deployed-eval
# score and in the acceptance report). If this test fails you edited correctness_judge.txt: bump runner.JUDGE_PROMPT_VERSION, add the
# new (version, sha256) here, and re-run scripts/judge_acceptance.py against the frozen probes.
JUDGE_PROMPT_VERSIONS = {"cj-v2": "8f92cf059b1e4c484a89b48150776c17638b9d75263424fce0b48f73583bf7c3",
                         "cj-v3": "e5b727c722dac1eb77db16721cc945f8d42a037558a6ac6e71e037564f8c637d"}


def test_the_judge_prompt_version_names_exactly_this_prompt_text():
    digest = hashlib.sha256(runner.JUDGE_PROMPT.encode("utf-8")).hexdigest()
    assert runner.JUDGE_PROMPT_VERSION in JUDGE_PROMPT_VERSIONS
    assert JUDGE_PROMPT_VERSIONS[runner.JUDGE_PROMPT_VERSION] == digest, f"the prompt changed: bump JUDGE_PROMPT_VERSION (sha256 {digest})"


def test_the_script_never_imports_the_paid_judge_before_go():
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.index("LITELLM_LOCAL_MODEL_COST_MAP") < src.index("from semigraph")     # offline-safe before any litellm import
    assert not hasattr(ja, "llm_json")                                                 # imported lazily inside main, on --go only
