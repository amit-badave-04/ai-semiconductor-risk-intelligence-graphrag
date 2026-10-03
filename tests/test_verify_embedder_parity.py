"""scripts/verify_embedder_parity.py: the pre-merge retrieval-parity gate for a patched query embedder.

M5 decision 2.1 gate (1): over the corpus vectors the live site searches, a candidate query encoder must keep the query
vectors within cosine mean 0.998 / min 0.997 of the baseline, the top-1 chunk identical for >= 95% of the questions and the
mean top-8 overlap >= 0.97. The metric computation is a pure function over arrays, so these tests use small synthetic
vectors with hand-computable geometry; only the corpus loader (pandas + pyarrow, dev-only) and the end-to-end runs (a tiny
ONNX model built with the dev-only ``onnx`` package) skip in the shipped serve venv.
"""

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_embedder_parity.py"
spec = importlib.util.spec_from_file_location("verify_embedder_parity_script", SCRIPT)
pr = importlib.util.module_from_spec(spec)
sys.modules["verify_embedder_parity_script"] = pr
spec.loader.exec_module(pr)

BENCHMARK = ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json"
EXAMPLES = ROOT / "src" / "semigraph" / "artifacts" / "examples.json"

requires_onnx = pytest.mark.skipif(importlib.util.find_spec("onnx") is None,
                                   reason="onnx is a dev-only package (not installed in the shipped serve venv)")
requires_parquet = pytest.mark.skipif(
    importlib.util.find_spec("pandas") is None or importlib.util.find_spec("pyarrow") is None,
    reason="pandas + pyarrow are dev-only packages (not installed in the shipped serve venv)")


# ---------------------------------------------------------------- geometry: unit vectors in a plane at chosen angles

def at(*degrees: float) -> np.ndarray:
    rad = np.radians(np.array(degrees, dtype=np.float64))
    return np.stack([np.cos(rad), np.sin(rad)], axis=1)


CORPUS = at(*[10.0 * i for i in range(12)])  # twelve documents, 10 degrees apart: doc i sits at 10*i degrees


def passing_metrics() -> dict:
    return {"query_cosine": {"mean": 0.9988, "min": 0.9983, "p05": 0.9985},
            "top1_identical_fraction": 0.96, "top8_overlap": {"mean": 0.979, "min": 0.875, "identical_set_fraction": 0.83}}


def verdicts(metrics: dict) -> dict[str, bool]:
    return {g.name: g.passed for g in pr.evaluate_gates(metrics)}


# ---------------------------------------------------------------- compute_parity_metrics

def test_identical_encoders_are_perfectly_equivalent():
    queries = at(0.0, 33.0, 71.0)

    m = pr.compute_parity_metrics(queries, queries.copy(), CORPUS)

    assert m["questions"] == 3 and m["corpus_vectors"] == 12
    assert m["query_cosine"]["mean"] == pytest.approx(1.0) and m["query_cosine"]["min"] == pytest.approx(1.0)
    assert m["top8_overlap"] == {"mean": 1.0, "min": 1.0, "identical_set_fraction": 1.0}
    assert m["top10_overlap"] == {"mean": 1.0, "min": 1.0, "identical_set_fraction": 1.0}
    assert m["top1_identical_fraction"] == 1.0 and m["top1_changed_questions"] == 0
    assert m["max_top10_score_gap"] == pytest.approx(0.0, abs=1e-12)


def test_a_known_swap_gives_the_hand_computed_metrics():
    """Baseline queries all at 0 degrees. Candidate queries at 0 (same), 18 (top-1 flips to doc 2, both top-k sets keep the
    same members) and 47 (top-8 loses doc 0 for doc 8: overlap 7/8; top-10 set unchanged)."""
    base, cand = at(0.0, 0.0, 0.0), at(0.0, 18.0, 47.0)

    m = pr.compute_parity_metrics(base, cand, CORPUS)

    cosines = [1.0, math.cos(math.radians(18)), math.cos(math.radians(47))]
    assert m["query_cosine"]["mean"] == pytest.approx(sum(cosines) / 3)
    assert m["query_cosine"]["min"] == pytest.approx(min(cosines))
    assert m["query_cosine"]["p05"] == pytest.approx(float(np.percentile(cosines, 5)))
    assert m["top1_identical_fraction"] == pytest.approx(1 / 3) and m["top1_changed_questions"] == 2
    assert m["top8_overlap"]["mean"] == pytest.approx((1.0 + 1.0 + 7 / 8) / 3)
    assert m["top8_overlap"]["min"] == pytest.approx(7 / 8)
    assert m["top8_overlap"]["identical_set_fraction"] == pytest.approx(2 / 3)
    assert m["top10_overlap"] == {"mean": 1.0, "min": 1.0, "identical_set_fraction": 1.0}

    def top10_scores(angle: float) -> list[float]:  # a query at ``angle`` against documents at 10*j degrees
        return sorted((math.cos(math.radians(10 * j - angle)) for j in range(12)), reverse=True)[:10]

    expected_gap = max(abs(a - b) for angle in (0.0, 18.0, 47.0) for a, b in zip(top10_scores(0.0), top10_scores(angle)))
    assert m["max_top10_score_gap"] == pytest.approx(expected_gap)


def test_per_question_values_are_returned_in_question_order():
    m = pr.compute_parity_metrics(at(0.0, 0.0), at(0.0, 47.0), CORPUS)

    per = m["per_question"]
    assert per["top1_same"] == [True, False]  # at 47 degrees the nearest document is doc 5, not doc 0
    assert per["top8_overlap"] == [1.0, 7 / 8] and per["top10_overlap"] == [1.0, 1.0]
    assert per["cosine"][0] == pytest.approx(1.0) and per["cosine"][1] == pytest.approx(math.cos(math.radians(47)))


def test_a_top1_flip_reports_both_documents_and_how_close_to_a_tie_each_encoder_saw_them():
    """Baseline at 0 degrees picks doc 0 (10-degree grid); the candidate at 18 degrees picks doc 2. The margin of a flip
    says whether it was a near-tie: the score gap between the two documents as each encoder scored them."""
    m = pr.compute_parity_metrics(at(0.0, 0.0), at(0.0, 18.0), CORPUS)

    per = m["per_question"]
    assert per["top1_baseline_index"] == [0, 0] and per["top1_candidate_index"] == [0, 2]
    assert per["top1_baseline_gap"][0] == 0.0 and per["top1_candidate_gap"][0] == 0.0  # no flip, no margin
    assert per["top1_baseline_gap"][1] == pytest.approx(1.0 - math.cos(math.radians(20)))  # doc 0 over doc 2 for baseline
    assert per["top1_candidate_gap"][1] == pytest.approx(math.cos(math.radians(2)) - math.cos(math.radians(18)))


def test_vectors_are_normalized_before_comparing():
    base, cand = at(0.0, 18.0), at(0.0, 47.0)

    plain = pr.compute_parity_metrics(base, cand, CORPUS)
    scaled = pr.compute_parity_metrics(base * 7.0, cand * 0.1, CORPUS * 3.0)

    assert scaled["query_cosine"] == pytest.approx(plain["query_cosine"])
    assert scaled["top8_overlap"] == pytest.approx(plain["top8_overlap"])
    assert scaled["max_top10_score_gap"] == pytest.approx(plain["max_top10_score_gap"])


def test_a_corpus_of_exactly_eight_uses_every_document():
    small = CORPUS[:8]

    m = pr.compute_parity_metrics(at(0.0), at(47.0), small)  # all eight documents rank in both lists, in another order

    assert m["top8_overlap"]["mean"] == 1.0 and m["top10_overlap"]["mean"] == 1.0


def test_a_corpus_smaller_than_the_top_8_window_is_an_error_not_a_vacuous_pass():
    with pytest.raises(ValueError, match="at least 8"):
        pr.compute_parity_metrics(at(0.0), at(47.0), CORPUS[:5])


def test_duplicate_corpus_vectors_do_not_make_identical_encoders_disagree():
    corpus = np.vstack([CORPUS, CORPUS])  # every score tied with its twin: ranks must still be reproducible

    m = pr.compute_parity_metrics(at(0.0, 25.0), at(0.0, 25.0), corpus)

    assert m["top8_overlap"]["identical_set_fraction"] == 1.0 and m["top1_identical_fraction"] == 1.0


def test_questions_against_an_empty_corpus_are_an_error():
    with pytest.raises(ValueError, match="corpus"):
        pr.compute_parity_metrics(at(0.0), at(0.0), np.empty((0, 2)))


def test_no_questions_is_an_error():
    with pytest.raises(ValueError, match="question"):
        pr.compute_parity_metrics(np.empty((0, 2)), np.empty((0, 2)), CORPUS)


def test_shape_and_dimension_mismatches_are_errors():
    with pytest.raises(ValueError, match="shape"):
        pr.compute_parity_metrics(at(0.0, 1.0), at(0.0), CORPUS)
    with pytest.raises(ValueError, match="dimension"):
        pr.compute_parity_metrics(np.ones((1, 3)), np.ones((1, 3)), CORPUS)


# ---------------------------------------------------------------- the pre-registered gates

def test_gate_thresholds_are_the_pre_registered_ones():
    assert (pr.COSINE_MEAN_MIN, pr.COSINE_MIN_MIN) == (0.998, 0.997)
    assert (pr.TOP1_IDENTICAL_MIN, pr.TOP8_OVERLAP_MEAN_MIN) == (0.95, 0.97)


def test_the_measured_spike_values_pass_every_gate():
    results = pr.evaluate_gates(passing_metrics())

    assert [g.name for g in results] == ["query cosine mean", "query cosine min", "top-1 identical fraction",
                                         "mean top-8 overlap"]
    assert all(g.passed for g in results)


def test_values_exactly_at_each_threshold_pass():
    m = {"query_cosine": {"mean": 0.998, "min": 0.997}, "top1_identical_fraction": 0.95,
         "top8_overlap": {"mean": 0.97}}

    assert all(verdicts(m).values())


@pytest.mark.parametrize("path, value, failing", [
    (("query_cosine", "mean"), 0.9979, "query cosine mean"),
    (("query_cosine", "min"), 0.9969, "query cosine min"),
    (("top1_identical_fraction",), 0.94, "top-1 identical fraction"),
    (("top8_overlap", "mean"), 0.969, "mean top-8 overlap"),
])
def test_each_threshold_fails_on_its_own(path, value, failing):
    m = passing_metrics()
    target = m
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    result = verdicts(m)

    assert result[failing] is False
    assert [name for name, ok in result.items() if not ok] == [failing]


def test_gate_lines_say_pass_or_fail_with_value_and_threshold():
    passing = pr.evaluate_gates(passing_metrics())[0]
    failing = pr.evaluate_gates({**passing_metrics(), "top1_identical_fraction": 0.9})[2]

    assert pr.format_gate_line(passing) == "PASS query cosine mean 0.998800 >= 0.998"
    assert pr.format_gate_line(failing) == "FAIL top-1 identical fraction 0.900000 >= 0.95"


# ---------------------------------------------------------------- questions

def test_questions_are_read_from_both_artifact_shapes_deduplicated_in_order():
    benchmark = [{"id": "N1", "q": "What was Nvidia's total revenue for fiscal 2024?"},
                 {"id": "N2", "q": "Which foundries does Qualcomm rely on?"}]
    examples = {"examples": [{"id": "N2", "question": "Which foundries does Qualcomm rely on?", "answer": "x"},
                             {"id": "R1", "question": "Did Meta remove any risk factors in FY2025?"}], "excluded": []}

    got = pr.collect_questions(benchmark) + pr.collect_questions(examples)

    assert list(dict.fromkeys(got)) == ["What was Nvidia's total revenue for fiscal 2024?",
                                        "Which foundries does Qualcomm rely on?",
                                        "Did Meta remove any risk factors in FY2025?"]


def test_a_plain_list_of_strings_is_a_question_file_and_junk_is_ignored(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps(["What is the first question here?", "short", "  What is the second question here?  "]),
                    encoding="utf-8")

    assert pr.load_questions([path]) == ["What is the first question here?", "What is the second question here?"]


def test_load_questions_merges_files_without_duplicates(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps([{"q": "Which suppliers does Intel name?"}, {"q": "What is AMD's fiscal year end?"}]),
                 encoding="utf-8")
    b.write_text(json.dumps({"examples": [{"question": "What is AMD's fiscal year end?"},
                                           {"question": "Who competes with Micron in DRAM?"}]}), encoding="utf-8")

    assert pr.load_questions([a, b]) == ["Which suppliers does Intel name?", "What is AMD's fiscal year end?",
                                         "Who competes with Micron in DRAM?"]


def test_an_empty_question_set_is_an_error(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="question"):
        pr.load_questions([path])


@pytest.mark.skipif(not (BENCHMARK.exists() and EXAMPLES.exists()), reason="artifact question files not present")
def test_the_default_question_files_yield_every_benchmark_and_example_question():
    """The 2026-10-03 spike read only the key "question", so it saw the 53 example questions and missed the benchmark's "q"
    key: the default here is the union of both files (60 distinct questions on the day this was written)."""
    bench = [b["q"] for b in json.loads(BENCHMARK.read_text(encoding="utf-8"))]
    ex = [e["question"] for e in json.loads(EXAMPLES.read_text(encoding="utf-8"))["examples"]]

    got = pr.load_questions(pr.DEFAULT_QUESTION_FILES)

    assert set(got) == set(bench) | set(ex)
    assert len(got) == len(set(got)) >= 53


# ---------------------------------------------------------------- corpus

@requires_parquet
def test_the_corpus_is_the_stacked_embedding_column_of_every_matching_file_in_name_order(tmp_path):
    import pandas as pd
    pd.DataFrame({"chunk_id": ["b1", "b2"], "embedding": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]}
                 ).to_parquet(tmp_path / "B.parquet")
    pd.DataFrame({"chunk_id": ["a1"], "embedding": [[0.0, 0.0, 2.0]]}).to_parquet(tmp_path / "A.parquet")

    corpus = pr.load_corpus(str(tmp_path / "*.parquet"))

    np.testing.assert_array_equal(corpus, [[0, 0, 2], [1, 0, 0], [0, 1, 0]])


@requires_parquet
def test_a_glob_without_files_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="parquet"):
        pr.load_corpus(str(tmp_path / "*.parquet"))


@requires_parquet
def test_a_file_without_the_embedding_column_is_an_error(tmp_path):
    import pandas as pd
    pd.DataFrame({"chunk_id": ["a"]}).to_parquet(tmp_path / "A.parquet")

    with pytest.raises(ValueError, match="embedding"):
        pr.load_corpus(str(tmp_path / "*.parquet"))


# ---------------------------------------------------------------- command line and the run() flow

class FakeEncoder:
    """encode_query(question) -> list[float], like OnnxBackend; one vector per known question."""

    def __init__(self, vectors: dict[str, np.ndarray]):
        self.vectors, self.seen = vectors, []

    def encode_query(self, question: str) -> list[float]:
        self.seen.append(question)
        return self.vectors[question].tolist()


QUESTIONS = [f"Question number {i} about export controls?" for i in range(10)]


def encoders(candidate_shift_degrees: float) -> dict[str, FakeEncoder]:
    angles = [17.0 * i for i in range(len(QUESTIONS))]
    base = dict(zip(QUESTIONS, at(*angles), strict=True))
    cand = dict(zip(QUESTIONS, at(*[a + candidate_shift_degrees for a in angles]), strict=True))
    return {"base.onnx": FakeEncoder(base), "cand.onnx": FakeEncoder(cand)}


def write_questions(tmp_path: Path) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
    return path


def run_with(tmp_path, shift, corpus=CORPUS, extra=()):
    fakes = encoders(shift)
    args = pr.parse_args(["--baseline", "base.onnx", "--candidate", "cand.onnx", "--questions",
                          str(write_questions(tmp_path)), "--out", str(tmp_path / "report.json"), *extra])
    code = pr.run(args, load_encoder=lambda path, threads: fakes[path], corpus_loader=lambda pattern: corpus)
    return code, fakes


def test_cli_defaults():
    args = pr.parse_args(["--baseline", "a.onnx", "--candidate", "b.onnx"])

    assert (args.baseline, args.candidate) == ("a.onnx", "b.onnx")
    assert args.corpus_glob == pr.DEFAULT_CORPUS_GLOB
    assert Path(args.corpus_glob).parts[-4:] == ("data", "processed", "embeddings", "*.parquet")
    assert [Path(p).name for p in args.questions] == ["benchmark.json", "examples.json"]
    assert args.out is None and args.threads == 4


def test_cli_requires_both_models(capsys):
    with pytest.raises(SystemExit) as raised:
        pr.parse_args(["--baseline", "a.onnx"])

    assert raised.value.code == 2


def test_the_same_file_as_baseline_and_candidate_is_refused_with_exit_two(tmp_path, capsys):
    fakes = encoders(0.0)
    args = pr.parse_args(["--baseline", "same.onnx", "--candidate", "./same.onnx", "--questions",
                          str(write_questions(tmp_path))])

    code = pr.run(args, load_encoder=lambda path, threads: fakes["base.onnx"], corpus_loader=lambda pattern: CORPUS)

    assert code == 2 and "same file" in capsys.readouterr().err


def test_an_encoder_crash_exits_two_not_one(tmp_path, monkeypatch, capsys):
    def boom(path, threads):
        raise RuntimeError("session failed to load")

    monkeypatch.setattr(pr, "load_onnx_encoder", boom)
    monkeypatch.setattr(pr, "load_corpus", lambda pattern: CORPUS)

    code = pr.main(["--baseline", "a.onnx", "--candidate", "b.onnx", "--questions", str(write_questions(tmp_path))])

    assert code == 2 and "session failed to load" in capsys.readouterr().err


def report_prompt_matches_serving(tmp_path: Path) -> bool:
    """The report records which query prompt the encoders used (the spike scripts used a different one)."""
    embeddings = pytest.importorskip("semigraph.embeddings")
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    return report["query_prompt"] == embeddings.QUERY_PROMPT


def test_a_passing_run_exits_zero_prints_pass_lines_and_writes_the_report(tmp_path, capsys):
    code, fakes = run_with(tmp_path, shift=0.0)

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert len([ln for ln in lines if ln.startswith("PASS ")]) == 4 and not [ln for ln in lines if "FAIL" in ln]
    assert "RESULT PASS" in lines
    assert report_prompt_matches_serving(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True and report["questions"] == 10 and report["corpus_vectors"] == 12
    assert [g["name"] for g in report["gates"]] == ["query cosine mean", "query cosine min", "top-1 identical fraction",
                                                    "mean top-8 overlap"]
    assert report["baseline"] == "base.onnx" and report["candidate"] == "cand.onnx"
    assert report["metrics"]["top1_changed_questions"] == 0 and "per_question" not in report["metrics"]
    assert fakes["base.onnx"].seen == QUESTIONS and fakes["cand.onnx"].seen == QUESTIONS


def test_a_regressed_candidate_exits_one_and_names_the_failed_gates(tmp_path, capsys):
    code, _ = run_with(tmp_path, shift=9.0)  # every query vector turned by 9 degrees: cosine 0.988

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert any(ln.startswith("FAIL query cosine mean") for ln in lines)
    assert any(ln.startswith("FAIL query cosine min") for ln in lines)
    assert "RESULT FAIL" in lines
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False and not any(g["passed"] for g in report["gates"][:2])
    changed = report["changed_top1_questions"]
    assert len(changed) == report["metrics"]["top1_changed_questions"]
    assert all(any(q.startswith(c) for q in QUESTIONS) for c in changed)
    details = report["changed_top1_details"]
    assert [d["q"] for d in details] == changed
    assert all(d["baseline_top1"] != d["candidate_top1"] and d["baseline_score_gap"] >= 0 and d["candidate_score_gap"] >= 0
               for d in details)


def test_an_empty_corpus_is_reported_as_an_error_not_a_gate_failure(tmp_path, capsys):
    fakes = encoders(0.0)
    args = pr.parse_args(["--baseline", "base.onnx", "--candidate", "cand.onnx",
                          "--questions", str(write_questions(tmp_path))])

    code = pr.run(args, load_encoder=lambda path, threads: fakes[path], corpus_loader=lambda pattern: np.empty((0, 2)))

    assert code == 2
    assert "corpus" in capsys.readouterr().err.lower()


def test_main_turns_an_unmatched_corpus_glob_into_exit_code_two(tmp_path, capsys):
    code = pr.main(["--baseline", "a.onnx", "--candidate", "b.onnx", "--questions", str(write_questions(tmp_path)),
                    "--corpus-glob", str(tmp_path / "none-*.parquet")])

    assert code == 2 and "ERROR" in capsys.readouterr().err


def test_embed_queries_stacks_the_encoders_vectors_in_question_order():
    fake = encoders(0.0)["base.onnx"]

    vecs = pr.embed_queries(fake, QUESTIONS[:3])

    assert vecs.shape == (3, 2)
    np.testing.assert_allclose(vecs, at(0.0, 17.0, 34.0))


# ---------------------------------------------------------------- end to end with the production encoder on a tiny model

@requires_onnx
@requires_parquet
def test_end_to_end_with_the_serving_backend_on_tiny_models(tmp_path):
    import pandas as pd
    from test_build_onnx_embedder import TINY_DIM, write_tiny_embedder
    pytest.importorskip("semigraph.embeddings_onnx")
    model_a = write_tiny_embedder(tmp_path / "a", seed=0)
    model_a_copy = write_tiny_embedder(tmp_path / "a2", seed=0)  # same weights, another file (the script refuses one file twice)
    model_b = write_tiny_embedder(tmp_path / "b", seed=1)  # a different model: its vectors must NOT pass the gate
    rng = np.random.default_rng(7)
    pd.DataFrame({"embedding": list(rng.standard_normal((40, TINY_DIM)))}).to_parquet(tmp_path / "corpus.parquet")
    common = ["--corpus-glob", str(tmp_path / "*.parquet"), "--questions", str(write_questions(tmp_path)), "--threads", "1"]

    same = pr.main(["--baseline", str(model_a), "--candidate", str(model_a_copy), "--out", str(tmp_path / "same.json"),
                    *common])
    different = pr.main(["--baseline", str(model_a), "--candidate", str(model_b),
                         "--out", str(tmp_path / "diff.json"), *common])

    assert same == 0 and different == 1
    same_report = json.loads((tmp_path / "same.json").read_text(encoding="utf-8"))
    assert same_report["metrics"]["query_cosine"]["min"] == pytest.approx(1.0) and same_report["corpus_vectors"] == 40
    assert json.loads((tmp_path / "diff.json").read_text(encoding="utf-8"))["passed"] is False
