"""scripts/embedder_timing.py: interleaved single-question timing of a baseline and a candidate query embedder.

M5 decision 2.1 gate (3): on Fly, on the live class and the staging class, the patched model must beat the unpatched one by
median over >= 50 interleaved calls. The script runs inside a Fly machine of the serve image, so it may use only
onnxruntime, numpy and tokenizers (pinned below by reading its imports). The statistics, the call order and the exit logic
are tested with a fake clock and fake encoders; the tiny-model run only checks the wiring, never which model is faster.
"""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "embedder_timing.py"
spec = importlib.util.spec_from_file_location("embedder_timing_script", SCRIPT)
et = importlib.util.module_from_spec(spec)
sys.modules["embedder_timing_script"] = et
spec.loader.exec_module(et)

REAL_MODEL = ROOT / "models" / "qwen3-embedding-0.6b-q8" / "model_q8.onnx"
requires_onnx = pytest.mark.skipif(importlib.util.find_spec("onnx") is None,
                                   reason="onnx is a dev-only package (not installed in the shipped serve venv)")
requires_real_model = pytest.mark.skipif(not REAL_MODEL.exists(), reason=f"{REAL_MODEL} is not built on this machine")


# ---------------------------------------------------------------- fakes: a clock that only moves when an embed "runs"

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeEmbed:
    """Callable standing in for one encoder's embed: costs ``cost`` seconds of fake time and logs (name, question)."""

    def __init__(self, name: str, cost: float, clock: Clock, log: list) -> None:
        self.name, self.cost, self.clock, self.log = name, cost, clock, log

    def __call__(self, question: str):
        self.log.append((self.name, question))
        self.clock.now += self.cost


# ---------------------------------------------------------------- summarize_timings

def test_summary_reports_median_p90_and_speedup():
    baseline = [float(i) for i in range(1, 11)]  # 1..10 s
    candidate = [0.5] * 10

    s = et.summarize_timings(baseline, candidate)

    assert s["n"] == 10
    assert s["baseline"]["median_s"] == pytest.approx(5.5)
    assert s["baseline"]["p90_s"] == pytest.approx(9.1)  # linear interpolation between ranks (numpy's default)
    assert (s["baseline"]["min_s"], s["baseline"]["max_s"]) == (1.0, 10.0)
    assert s["candidate"]["median_s"] == pytest.approx(0.5) and s["candidate"]["p90_s"] == pytest.approx(0.5)
    assert s["speedup_median"] == pytest.approx(11.0)
    assert s["candidate_faster"] is True


def test_the_verdict_uses_the_median_not_the_mean():
    outlier = [1.0, 1.0, 1.0, 1.0, 100.0]  # a throttling spike: mean 20.8 s, median 1 s

    s = et.summarize_timings(outlier, [2.0] * 5)
    assert s["candidate_faster"] is False  # median 2 s is slower than the baseline's median of 1 s
    s = et.summarize_timings([2.0] * 5, outlier)
    assert s["candidate_faster"] is True  # even though the candidate's mean is far worse


@pytest.mark.parametrize("candidate, faster", [([1.0] * 4, False), ([1.5] * 4, False), ([0.99] * 4, True)])
def test_the_candidate_must_be_strictly_faster(candidate, faster):
    s = et.summarize_timings([1.0] * 4, candidate)

    assert s["candidate_faster"] is faster
    assert (s["speedup_median"] > 1.0) is faster


def test_the_summary_flags_runs_below_the_gate_minimum_of_50_calls():
    assert et.GATE_MIN_CALLS == 50 and et.DEFAULT_N == 50
    assert et.summarize_timings([1.0] * 49, [0.5] * 49)["meets_gate_minimum_calls"] is False
    assert et.summarize_timings([1.0] * 50, [0.5] * 50)["meets_gate_minimum_calls"] is True


@pytest.mark.parametrize("baseline, candidate", [([], []), ([1.0, 2.0], [1.0]), ([1.0, 0.0], [1.0, 1.0]),
                                                 ([1.0], [float("nan")]), ([-1.0], [1.0])])
def test_the_summary_rejects_empty_unequal_or_non_positive_timings(baseline, candidate):
    with pytest.raises(ValueError):
        et.summarize_timings(baseline, candidate)


# ---------------------------------------------------------------- questions and the interleaving order

def test_the_built_in_questions_are_enough_distinct_realistic_questions():
    assert len(et.QUESTIONS) >= 10 and len(set(et.QUESTIONS)) == len(et.QUESTIONS)
    assert all(isinstance(q, str) and 20 < len(q) < 600 for q in et.QUESTIONS)


def test_questions_cycle_when_n_exceeds_the_list():
    assert et.pick_questions(7, ("a", "b", "c")) == ["a", "b", "c", "a", "b", "c", "a"]
    assert et.pick_questions(2, ("a", "b", "c")) == ["a", "b"]


def test_both_models_embed_the_same_question_in_every_round_in_counterbalanced_order():
    clock, log = Clock(), []
    base, cand = FakeEmbed("base", 1.0, clock, log), FakeEmbed("cand", 0.25, clock, log)

    baseline_s, candidate_s = et.run_interleaved(base, cand, ["q0", "q1", "q2", "q3"], clock=clock)

    assert log == [("base", "q0"), ("cand", "q0"), ("cand", "q1"), ("base", "q1"),
                   ("base", "q2"), ("cand", "q2"), ("cand", "q3"), ("base", "q3")]  # A B | B A | A B | B A
    assert baseline_s == [1.0] * 4 and candidate_s == [0.25] * 4


def test_each_call_is_timed_on_its_own():
    clock, log = Clock(), []

    class Varying(FakeEmbed):
        def __call__(self, question):
            self.cost = float(question[1:]) / 10  # "q3" costs 0.3 s
            super().__call__(question)

    baseline_s, candidate_s = et.run_interleaved(Varying("a", 0, clock, log), FakeEmbed("b", 0.5, clock, log),
                                                 ["q1", "q2", "q3"], clock=clock)

    assert baseline_s == pytest.approx([0.1, 0.2, 0.3]) and candidate_s == pytest.approx([0.5, 0.5, 0.5])


# ---------------------------------------------------------------- command line

def test_cli_defaults_are_n_50_one_thread_two_warmups():
    args = et.parse_args(["base.onnx", "cand.onnx"])

    assert (args.baseline, args.candidate) == ("base.onnx", "cand.onnx")
    assert (args.n, args.threads, args.warmup) == (50, 1, 2)
    assert args.tokenizer is None and args.out is None


def test_cli_accepts_n_threads_warmup_tokenizer_and_out():
    args = et.parse_args(["b.onnx", "c.onnx", "-n", "10", "--threads", "2", "--warmup", "0", "--tokenizer", "t.json",
                          "--out", "r.json"])

    assert (args.n, args.threads, args.warmup, args.tokenizer, args.out) == (10, 2, 0, "t.json", "r.json")
    assert et.parse_args(["b.onnx", "c.onnx", "--n", "7"]).n == 7


@pytest.mark.parametrize("flags", [["-n", "0"], ["-n", "-3"], ["-n", "x"], ["--threads", "-1"], ["--warmup", "-1"]])
def test_cli_rejects_bad_numbers(flags):
    with pytest.raises(SystemExit) as raised:
        et.parse_args(["b.onnx", "c.onnx", *flags])

    assert raised.value.code == 2


def test_cli_needs_both_models():
    with pytest.raises(SystemExit):
        et.parse_args(["only-one.onnx"])


# ---------------------------------------------------------------- run(): wiring, exit codes, report

def make_run(tmp_path, base_cost, cand_cost, argv_extra=(), n=50):
    clock, log, made = Clock(), [], []
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

    def make_encoder(model, tokenizer, threads):
        name = Path(model).name
        made.append((name, Path(tokenizer), threads))
        cost = base_cost if name.startswith("base") else cand_cost
        embed = FakeEmbed(name, cost, clock, log)
        return type("Enc", (), {"embed_query": staticmethod(embed)})()

    args = et.parse_args([str(tmp_path / "base.onnx"), str(tmp_path / "cand.onnx"), "-n", str(n), "--out",
                          str(tmp_path / "timing.json"), *argv_extra])
    code = et.run(args, make_encoder=make_encoder, clock=clock)
    return code, log, made


def test_exit_zero_when_the_candidate_median_is_lower(tmp_path, capsys):
    code, log, made = make_run(tmp_path, 1.0, 0.25)

    lines = capsys.readouterr().out.splitlines()
    assert code == 0 and "RESULT PASS" in lines
    report = json.loads((tmp_path / "timing.json").read_text(encoding="utf-8"))
    assert report["candidate_faster"] is True and report["speedup_median"] == pytest.approx(4.0)
    assert report["n"] == 50 and report["threads"] == 1 and report["warmup"] == 2
    assert report["baseline"]["median_s"] == pytest.approx(1.0) and report["candidate"]["median_s"] == pytest.approx(0.25)
    assert report["meets_gate_minimum_calls"] is True
    assert any("speedup" in ln and "4.00x" in ln for ln in lines)
    assert {(name, tok.name, th) for name, tok, th in made} == {("base.onnx", "tokenizer.json", 1),
                                                                ("cand.onnx", "tokenizer.json", 1)}


def test_a_faster_candidate_over_too_few_calls_is_inconclusive_not_a_pass(tmp_path, capsys):
    """Gate 3 needs >= 50 interleaved calls: a script or CI step that reads only the exit code must not record a pass."""
    code, _, _ = make_run(tmp_path, 1.0, 0.25, n=6)

    lines = capsys.readouterr().out.splitlines()
    assert code == 1 and not any(ln == "RESULT PASS" for ln in lines)
    assert any(ln.startswith("RESULT INCONCLUSIVE") for ln in lines)
    report = json.loads((tmp_path / "timing.json").read_text(encoding="utf-8"))
    assert report["candidate_faster"] is True and report["meets_gate_minimum_calls"] is False


def test_exit_one_when_the_candidate_is_not_faster(tmp_path, capsys):
    for cand_cost in (1.0, 2.0):
        code, _, _ = make_run(tmp_path, 1.0, cand_cost)
        assert code == 1
    assert "RESULT FAIL" in capsys.readouterr().out.splitlines()


def test_warmup_calls_run_first_and_are_not_timed(tmp_path):
    code, log, _ = make_run(tmp_path, 1.0, 0.25, ["--warmup", "3"])

    assert code == 0 and len(log) == 3 * 2 + 50 * 2  # 3 warm-up rounds then 50 timed rounds, two encoders each
    report = json.loads((tmp_path / "timing.json").read_text(encoding="utf-8"))
    assert report["n"] == 50 and report["warmup"] == 3


def test_a_shared_tokenizer_and_threads_are_passed_to_both_encoders(tmp_path):
    shared = tmp_path / "shared" / "tok.json"
    shared.parent.mkdir()
    shared.write_text("{}", encoding="utf-8")

    _, _, made = make_run(tmp_path, 1.0, 0.25, ["--tokenizer", str(shared), "--threads", "2"])

    assert {(tok, th) for _, tok, th in made} == {(shared, 2)}


def test_a_missing_tokenizer_is_an_error_not_a_verdict(tmp_path, capsys):
    args = et.parse_args([str(tmp_path / "base.onnx"), str(tmp_path / "cand.onnx")])  # no tokenizer.json written

    code = et.run(args, make_encoder=lambda *a: pytest.fail("no encoder may load"), clock=Clock())

    assert code == 2 and "tokenizer.json" in capsys.readouterr().err


# ---------------------------------------------------------------- what the script may import

def test_the_script_imports_only_numpy_onnxruntime_and_tokenizers_besides_the_stdlib():
    """It is copied alone into a Fly machine of the serve image: no pandas, no onnx package, no semigraph."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add(node.module.split(".")[0])

    assert roots - set(sys.stdlib_module_names) <= {"numpy", "onnxruntime", "tokenizers"}


def test_the_inlined_query_prompt_is_the_serving_prompt():
    embeddings = pytest.importorskip("semigraph.embeddings")

    assert et.QUERY_PROMPT == embeddings.QUERY_PROMPT


# ---------------------------------------------------------------- wiring on tiny ONNX models, and the real encoder

@requires_onnx
def test_main_times_two_tiny_models_and_reports_without_asserting_which_is_faster(tmp_path):
    from test_build_onnx_embedder import write_tiny_embedder
    model_a = write_tiny_embedder(tmp_path / "a", seed=0)
    model_b = write_tiny_embedder(tmp_path / "b", seed=1)

    code = et.main([str(model_a), str(model_b), "-n", "5", "--warmup", "1", "--out", str(tmp_path / "t.json")])

    assert code == 1  # 5 calls can never satisfy the gate's 50-call minimum, whichever model is faster
    report = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert report["n"] == 5 and report["baseline"]["median_s"] > 0 and report["candidate"]["median_s"] > 0
    assert report["meets_gate_minimum_calls"] is False


@requires_real_model
def test_the_inlined_encoder_matches_the_serving_backend_on_the_real_model():
    backend_module = pytest.importorskip("semigraph.embeddings_onnx")
    tokenizer = REAL_MODEL.parent / "tokenizer.json"
    question = "Which foundries does Qualcomm rely on to manufacture its chips?"

    mine = et.QueryEncoder(REAL_MODEL, tokenizer, threads=2).embed_query(question)
    serving = np.array(backend_module.OnnxBackend(REAL_MODEL, tokenizer, threads=2).encode_query(question),
                       dtype=np.float32)

    np.testing.assert_allclose(mine, serving, atol=1e-6)
