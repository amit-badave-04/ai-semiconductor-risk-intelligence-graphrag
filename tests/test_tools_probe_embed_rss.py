"""tools/probe/embed_rss.py: the resident-memory probe of ONE query-embedder model (window W1, M5 decision 14).

Nothing here loads a model or needs Linux: the backend is a stub that counts words, ``/proc/self/status`` is text or a fake
reader, psutil is a namespace, the clock is a counter. What is pinned: the /proc parser; the Windows fallback and its labels
(a Windows run is never gate evidence); the prediction arithmetic (live G4 peak + this model's peak - the q8 peak, judged
against 2,400,000 kB, the boundary included); that a reference which does not fit (other memory source, not q8, other workload)
yields no prediction; the shape of the one JSON object and that it carries no path; how the 60 queries are built (30 questions,
each plain and salted in the loadtest salt's format) and how the passage chunks are built (the real upload chunker, exactly
15,849 tokens, one chunk per call); that the probe loads the backend, the warm-up question and the thread count the way the
service does; that it is stdlib only at module level and imports nothing from ``tools`` (it runs as a loose file in the image);
and that the Dockerfile and windows.json agree on where it lives.
"""

import ast
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.loadtest import salt as loadtest_salt  # noqa: E402
from tools.probe import embed_rss as rss  # noqa: E402

MODULE = ROOT / "tools" / "probe" / "embed_rss.py"
Q8_TOKENIZER = ROOT / "models" / "qwen3-embedding-0.6b-q8" / "tokenizer.json"

PROC_STATUS_TEXT = """Name:\tpython
Umask:\t0022
VmPeak:\t 4000000 kB
VmSize:\t 3900000 kB
VmHWM:\t 1500000 kB
VmRSS:\t 1400000 kB
RssAnon:\t  900000 kB
RssFile:\t  499000 kB
RssShmem:\t    1000 kB
VmSwap:\t       0 kB
Threads:\t4
"""


def word_tokens(text: str) -> int:
    """A stand-in tokenizer: words and punctuation marks."""
    return len(re.findall(r"\w+|[^\w\s]", text))


# --- /proc/self/status and the Windows fallback -----------------------------------------------------------------------------

def test_the_proc_status_parser_reads_the_six_counters_in_kb():
    assert rss.parse_proc_status(PROC_STATUS_TEXT) == {
        "vmrss_kb": 1_400_000, "vmhwm_kb": 1_500_000, "rss_anon_kb": 900_000, "rss_file_kb": 499_000,
        "rss_shmem_kb": 1_000, "vm_swap_kb": 0}


def test_a_counter_the_kernel_does_not_write_is_none_not_zero():
    parsed = rss.parse_proc_status("VmHWM:\t 10 kB\nVmRSS:\t 8 kB\n")

    assert parsed["vmhwm_kb"] == 10 and parsed["rss_anon_kb"] is None and parsed["vm_swap_kb"] is None


def test_read_proc_status_reads_a_file(tmp_path):
    path = tmp_path / "status"
    path.write_text(PROC_STATUS_TEXT, encoding="ascii")

    assert rss.read_proc_status(str(path))["vmhwm_kb"] == 1_500_000


def fake_psutil(rss_bytes=2_000_000_000, peak_bytes=2_500_000_000, private=1_900_000_000, peak_private=2_400_000_000):
    info = SimpleNamespace(rss=rss_bytes, peak_wset=peak_bytes, pagefile=private, peak_pagefile=peak_private)
    return SimpleNamespace(Process=lambda: SimpleNamespace(memory_info=lambda: info))


def test_the_windows_reader_reports_the_peak_working_set_as_the_peak():
    sample = rss.read_working_set(fake_psutil())

    assert sample["vmrss_kb"] == 2_000_000_000 // 1024 and sample["vmhwm_kb"] == 2_500_000_000 // 1024
    assert sample["private_kb"] == 1_900_000_000 // 1024 and sample["peak_private_kb"] == 2_400_000_000 // 1024


def test_a_linux_machine_with_proc_uses_it_and_anything_else_falls_back_to_psutil_labelled(tmp_path):
    proc = tmp_path / "status"
    proc.write_text(PROC_STATUS_TEXT, encoding="ascii")

    linux = rss.pick_source("linux", str(proc))
    windows = rss.pick_source("win32", str(proc), fake_psutil())
    no_proc = rss.pick_source("linux", str(tmp_path / "missing"), fake_psutil())

    assert linux.is_linux and linux.source_id == rss.SOURCE_PROC and linux.read()["vmhwm_kb"] == 1_500_000
    for fallback in (windows, no_proc):
        assert not fallback.is_linux and fallback.source_id == rss.SOURCE_PSUTIL
        assert "not the Linux number" in fallback.label


def test_no_proc_and_no_psutil_is_an_error_not_a_silent_zero(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)               # makes ``import psutil`` raise ImportError

    with pytest.raises(rss.ProbeError, match="no memory counter"):
        rss.pick_source("win32", str(tmp_path / "missing"))


# --- statistics ---------------------------------------------------------------------------------------------------------------

def test_summarize_gives_median_p90_and_extremes():
    stats = rss.summarize([0.4, 0.1, 0.3, 0.2, 0.5])

    assert stats == {"n": 5, "median_s": 0.3, "p90_s": 0.46, "min_s": 0.1, "max_s": 0.5, "total_s": 1.5}


def test_summarize_refuses_nothing_to_summarize():
    with pytest.raises(ValueError):
        rss.summarize([])


# --- the queries -----------------------------------------------------------------------------------------------------------

def test_the_salt_format_is_the_loadtest_salt_s():
    for worker, n in ((1, 0), (1, 29), (0, 1999), (9, 999_999)):
        assert rss.apply_salt("What is X?", worker, n) == loadtest_salt.apply_salt("What is X?", worker, n)


def test_the_warm_up_question_and_threads_are_the_service_s():
    main_source = (ROOT / "src" / "semigraph" / "serve" / "main.py").read_text(encoding="utf-8")
    assert f'embedder.encode_query("{rss.WARMUP_QUESTION}")' in main_source
    for toml in ("fly.toml", "deploy/staging/fly.stg.toml"):
        pinned = re.search(r'ONNX_THREADS\s*=\s*"(\d+)"', (ROOT / toml).read_text(encoding="utf-8")).group(1)
        assert int(pinned) == rss.DEFAULT_THREADS


def test_pick_questions_spreads_over_the_length_order_and_is_deterministic():
    pool = [f"{'w' * n} ?" for n in range(1, 101)]
    picked = rss.pick_questions(pool)

    assert len(picked) == rss.QUERY_QUESTIONS and picked == rss.pick_questions(list(reversed(pool)))
    assert picked[0] == pool[0] and picked[-1] == pool[-1]
    assert [len(q) for q in picked] == sorted(len(q) for q in picked)


def test_pick_questions_keeps_a_small_pool_whole_and_drops_repeats():
    assert rss.pick_questions(["b?", "a?", "a?", "ccc?"]) == ["a?", "b?", "ccc?"]


def test_the_plan_is_every_question_plain_and_salted_in_alternating_order():
    questions = [f"Question number {i}?" for i in range(30)]
    plan = rss.query_plan(questions)

    assert len(plan) == rss.QUERY_CALLS == 60 and sum(salted for _, salted in plan) == 30
    for n, question in enumerate(questions):
        first, second = plan[2 * n], plan[2 * n + 1]
        assert {first[1], second[1]} == {False, True}
        assert (first if not first[1] else second) == (question, False)
        assert (first if first[1] else second)[0] == loadtest_salt.apply_salt(question, rss.SALT_WORKER, n)
        assert (first[1] is False) == (n % 2 == 0)                  # plain first on even rounds, salted first on odd ones


def test_the_question_pool_is_the_packaged_benchmark_and_examples():
    pool = rss.load_question_pool()

    assert len(pool) >= rss.QUERY_QUESTIONS and len(set(pool)) == len(pool)
    assert "What was Nvidia's total revenue for the fiscal year ended January 28, 2024?" in pool


# --- the passages ----------------------------------------------------------------------------------------------------------

def test_fit_to_tokens_is_exact_and_pads_with_single_tokens():
    text = "alpha beta, gamma delta epsilon zeta eta theta"

    for target in (1, 4, 7, 12, 25):
        assert word_tokens(rss.fit_to_tokens(text, target, word_tokens)) == target


def test_the_synthetic_prose_is_deterministic_and_changes_paragraph_to_paragraph():
    assert rss.paragraph(7) == rss.paragraph(7) and rss.paragraph(7) != rss.paragraph(8)
    assert all(len(rss.paragraph(i)) > 400 for i in range(20))


@pytest.mark.parametrize("total", [30, 700, 5000, rss.G4_PASSAGE_TOKENS])
def test_the_chunks_hold_exactly_the_requested_tokens_and_none_is_over_the_cap(total):
    texts = rss.build_passage_chunks(word_tokens, total)
    sizes = [word_tokens(t) for t in texts]

    assert sum(sizes) == total and max(sizes) <= rss.MAX_CHUNK_TOKENS and texts == rss.build_passage_chunks(word_tokens, total)


def test_a_full_size_workload_is_many_chunks_cut_by_the_upload_chunker():
    texts = rss.build_passage_chunks(word_tokens, rss.G4_PASSAGE_TOKENS)

    assert len(texts) > 40 and len(set(texts)) == len(texts)
    assert all(len(t) <= 1800 for t in texts[:-1])                   # the chunker's hard ceiling in characters


@pytest.mark.skipif(not Q8_TOKENIZER.is_file(), reason="the local q8 model directory is not present")
def test_with_the_real_tokenizer_the_g4_workload_is_about_sixty_chunks_of_up_to_a_few_hundred_tokens():
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(Q8_TOKENIZER))
    count = lambda text: sum(len(tokenizer.encode(piece).ids) for piece in [text])  # noqa: E731
    texts = rss.build_passage_chunks(count, rss.G4_PASSAGE_TOKENS)
    sizes = [count(t) for t in texts]

    assert sum(sizes) == rss.G4_PASSAGE_TOKENS and max(sizes) <= rss.MAX_CHUNK_TOKENS
    assert 45 <= len(texts) <= 80 and max(sizes) >= 350              # the G4 document: 60 chunks, the largest 420 tokens


# --- the run with a stub backend ----------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.125
        return self.now


class Counters:
    """A fake /proc/self/status: every read grows the peak, so the stages come out in order."""

    def __init__(self, start=500_000, step=100_000):
        self.value, self.step, self.reads = start, step, 0

    def __call__(self):
        self.reads += 1
        self.value += self.step
        return {"vmrss_kb": self.value - 1_000, "vmhwm_kb": self.value, "rss_anon_kb": self.value - 5_000,
                "rss_file_kb": 4_000, "rss_shmem_kb": 0, "vm_swap_kb": 0}


class StubBackend:
    variant = "q8"

    def __init__(self, model, threads, variant="q8"):
        self.model, self.threads, self.variant = model, threads, variant
        self.queries, self.passages = [], []

    def encode_query(self, text):
        self.queries.append(text)

    def encode_passages(self, texts, batch_size=8, show_progress=False):
        self.passages.append(list(texts))

    def count_tokens(self, text):
        return word_tokens(text)


def make_hooks(loaded, *, linux=True, step=100_000, start=500_000, questions=None, variant="q8", reference=None):
    source = rss.MemorySource(Counters(start, step), rss.SOURCE_PROC if linux else rss.SOURCE_PSUTIL,
                              "fake", linux)

    def load(model, threads):
        backend = StubBackend(model, threads, variant)
        loaded.append(backend)
        return backend

    return rss.Hooks(source=source, preload=lambda: None, load_backend=load, clock=Clock(), log=lambda text: None,
                     load_questions=lambda: questions or [f"Realistic question number {i:03d}, with some detail?" for i in range(60)],
                     run_reference=(lambda model, args: reference) if reference is not None else None)


def run_main(argv, hooks):
    out = io.StringIO()
    with redirect_stdout(out):
        code = rss.main(argv, hooks)
    return code, out.getvalue()


@pytest.fixture
def model_file(tmp_path):
    path = tmp_path / "weights" / "model_q8.onnx"
    path.parent.mkdir()
    path.write_bytes(b"x")
    return path


def test_the_run_loads_like_the_service_and_makes_the_warm_up_query_then_60_queries_then_one_chunk_per_call(model_file):
    loaded = []
    code, text = run_main([str(model_file), "--passage-tokens", "700"], make_hooks(loaded))
    backend = loaded[0]

    assert code == 0 and json.loads(text)["workload"]["passage_tokens_embedded"] == 700
    assert backend.model == model_file and backend.threads == rss.DEFAULT_THREADS == 1
    assert backend.queries[0] == rss.WARMUP_QUESTION and len(backend.queries) == 1 + rss.QUERY_CALLS
    assert all(len(call) == 1 for call in backend.passages) and len(backend.passages) >= 2
    assert sum(1 for q in backend.queries[1:] if re.search(r" \(ref \d{7}\)$", q)) == 30


def test_the_report_has_the_documented_shape(model_file):
    code, text = run_main([str(model_file), "--passage-tokens", "700"], make_hooks([]))
    report = json.loads(text)

    assert code == 0 and text.strip().startswith("{") and text.strip().endswith("}")      # one object, nothing else on stdout
    assert set(report) == {"probe", "version", "platform", "memory_source", "memory_source_label", "model", "workload",
                           "memory_kb", "peak_kb", "timing", "gate_evidence", "not_gate_evidence_because",
                           "predicted_uvicorn_vmhwm_kb", "limit_kb", "margin_kb", "verdict", "prediction"}
    assert list(report["memory_kb"]) == list(rss.STAGES)
    assert report["model"] == {"file": "model_q8.onnx", "variant": "q8", "threads": 1}
    assert report["peak_kb"] == report["memory_kb"]["after_passages"]["vmhwm_kb"]
    assert {"queries_plain", "queries_salted", "salt_delta_pct", "passages", "load_s", "warmup_query_s"} <= set(report["timing"])
    assert report["timing"]["queries_plain"]["n"] == report["timing"]["queries_salted"]["n"] == 30
    assert report["timing"]["passages"]["n"] == report["workload"]["passage_chunks"]


def test_the_report_names_the_model_by_file_name_only_and_carries_no_path(model_file, tmp_path):
    reference = tmp_path / "ref" / "q8_result.json"
    reference.parent.mkdir()
    reference.write_text(json.dumps({"probe": "embed_rss"}), encoding="utf-8")
    code, text = run_main([str(model_file), "--passage-tokens", "700", "--compare", str(reference)], make_hooks([]))

    assert code == 0
    for forbidden in (str(tmp_path), str(model_file.parent), "q8_result.json", "weights"):
        assert forbidden not in text
    assert "\\" not in text and not re.search(r'"[A-Za-z]:', text)


def test_memory_is_read_at_every_stage_in_order(model_file):
    hooks = make_hooks([])
    code, text = run_main([str(model_file), "--passage-tokens", "700"], hooks)
    stages = json.loads(text)["memory_kb"]

    assert code == 0 and hooks.source.read.reads == len(rss.STAGES)
    peaks = [stages[name]["vmhwm_kb"] for name in rss.STAGES]
    assert peaks == sorted(peaks) and len(set(peaks)) == len(peaks)


def test_the_out_file_holds_the_same_json(model_file, tmp_path):
    out = tmp_path / "result.json"
    code, text = run_main([str(model_file), "--passage-tokens", "700", "--out", str(out)], make_hooks([]))

    assert code == 0 and json.loads(out.read_text(encoding="utf-8")) == json.loads(text)


def test_a_missing_model_is_exit_2_with_the_file_name_and_nothing_on_stdout(tmp_path, capsys):
    code = rss.main([str(tmp_path / "gone" / "model_q8.onnx")], make_hooks([]))
    captured = capsys.readouterr()

    assert code == 2 and "model_q8.onnx" in captured.err and captured.out == ""
    assert str(tmp_path) not in captured.err


def test_a_backend_error_is_exit_2_not_a_memory_fail(model_file, capsys):
    hooks = make_hooks([])
    boom = rss.Hooks(source=hooks.source, preload=lambda: None, load_backend=lambda m, t: (_ for _ in ()).throw(RuntimeError("ort")),
                     log=lambda text: None)

    assert rss.main([str(model_file)], boom) == 2
    assert "RuntimeError: ort" in capsys.readouterr().err


# --- the prediction -------------------------------------------------------------------------------------------------------------

def reference_result(peak=1_000_000, source=rss.SOURCE_PROC, variant="q8", threads=1, tokens=rss.G4_PASSAGE_TOKENS):
    return {"probe": "embed_rss", "memory_source": source, "peak_kb": peak,
            "model": {"variant": variant, "threads": threads}, "workload": {"passage_tokens_requested": tokens}}


MINE = {"memory_source": rss.SOURCE_PROC, "threads": 1, "passage_tokens": rss.G4_PASSAGE_TOKENS}


def test_the_prediction_is_the_live_peak_plus_the_difference_from_the_q8_peak():
    got = rss.predict(1_640_000, reference_result(1_000_000), MINE, binding=True)

    assert got["predicted_uvicorn_vmhwm_kb"] == 1_125_116 + 640_000 == 1_765_116
    assert got["limit_kb"] == 2_400_000 and got["margin_kb"] == 2_400_000 - 1_765_116 == 634_884 and got["verdict"] == "PASS"
    assert got["prediction"] == {"live_g4_vmhwm_kb": 1_125_116, "this_peak_kb": 1_640_000, "q8_reference_peak_kb": 1_000_000,
                                 "delta_kb": 640_000, "margin_pct_of_limit": 26.5}


def test_over_the_limit_is_a_fail_with_a_negative_margin():
    got = rss.predict(2_500_000, reference_result(1_000_000), MINE, binding=True)

    assert got["predicted_uvicorn_vmhwm_kb"] == 2_625_116 and got["margin_kb"] == -225_116 and got["verdict"] == "FAIL"


def test_exactly_at_the_limit_passes_and_one_kb_over_fails():
    at_limit = 2_400_000 - 1_125_116 + 1_000_000

    assert rss.predict(at_limit, reference_result(1_000_000), MINE, binding=True)["verdict"] == "PASS"
    assert rss.predict(at_limit + 1, reference_result(1_000_000), MINE, binding=True)["verdict"] == "FAIL"


def test_a_model_that_is_smaller_than_q8_lowers_the_prediction():
    assert rss.predict(900_000, reference_result(1_000_000), MINE, binding=True)["predicted_uvicorn_vmhwm_kb"] == 1_025_116


def test_a_non_binding_run_gives_the_number_but_only_an_indicative_verdict():
    assert rss.predict(1_640_000, reference_result(), MINE, binding=False)["verdict"] == "INDICATIVE_PASS"
    assert rss.predict(2_500_000, reference_result(), MINE, binding=False)["verdict"] == "INDICATIVE_FAIL"


@pytest.mark.parametrize("reference, fragment", [
    (None, "no --compare"),
    ("text", "not the output of this probe"),
    ({"probe": "other"}, "not the output of this probe"),
    ({"probe": "embed_rss", "peak_kb": None}, "no peak"),
    (reference_result(source=rss.SOURCE_PSUTIL), "counted memory with"),
    (reference_result(variant="fp32"), "not q8"),
    (reference_result(threads=2), "thread count"),
    (reference_result(tokens=100), "passage workload"),
], ids=["none", "text", "foreign", "no-peak", "other-source", "not-q8", "threads", "workload"])
def test_a_reference_that_does_not_fit_gives_no_prediction_and_says_why(reference, fragment):
    got = rss.predict(1_640_000, reference, MINE, binding=True)

    assert got["predicted_uvicorn_vmhwm_kb"] is None and got["margin_kb"] is None and got["verdict"] == "NO_REFERENCE"
    assert fragment in got["prediction"]["reason"]


def test_compare_with_a_result_file_predicts_from_it(model_file, tmp_path):
    reference = tmp_path / "q8.json"
    reference.write_text(json.dumps(reference_result(peak=1_500_000, tokens=700)), encoding="utf-8")
    hooks = make_hooks([], start=900_000, step=100_000)
    code, text = run_main([str(model_file), "--passage-tokens", "700", "--compare", str(reference)], hooks)
    report = json.loads(text)

    assert code == 0 and report["prediction"]["q8_reference_peak_kb"] == 1_500_000
    assert report["predicted_uvicorn_vmhwm_kb"] == 1_125_116 + report["peak_kb"] - 1_500_000


def test_compare_with_a_q8_model_file_measures_it_first_in_a_fresh_process(model_file, tmp_path):
    q8 = tmp_path / "q8dir" / "model_q8.onnx"
    q8.parent.mkdir()
    q8.write_bytes(b"x")
    calls = []

    def fake_child(model, args):
        calls.append((model, args.threads, args.passage_tokens))
        return reference_result(peak=1_000_000, tokens=700)

    loaded = []
    base = make_hooks(loaded)
    hooks = rss.Hooks(source=base.source, preload=lambda: calls.append("preload"), load_backend=base.load_backend,
                      clock=base.clock, log=base.log, load_questions=base.load_questions, run_reference=fake_child)
    code, text = run_main([str(model_file), "--passage-tokens", "700", "--compare", str(q8)], hooks)

    assert code == 0 and calls == [(str(q8), 1, 700), "preload"] and len(loaded) == 1      # the reference ran first
    assert json.loads(text)["prediction"]["q8_reference_peak_kb"] == 1_000_000


def test_a_compare_that_cannot_be_read_is_exit_2_and_does_not_echo_its_path(model_file, tmp_path, capsys):
    code = rss.main([str(model_file), "--compare", str(tmp_path / "secret_dir" / "q8.json")], make_hooks([]))
    captured = capsys.readouterr()

    assert code == 2 and "--compare" in captured.err and "secret_dir" not in captured.err and captured.out == ""


def test_a_fail_exits_1(model_file, tmp_path):
    reference = tmp_path / "q8.json"
    reference.write_text(json.dumps(reference_result(peak=10)), encoding="utf-8")
    # peak ~ 4.4 GB above the reference: a Linux run with the full G4 workload
    hooks = make_hooks([], step=1_000_000_000)
    code, text = run_main([str(model_file), "--compare", str(reference)], hooks)

    assert code == 1 and json.loads(text)["verdict"] == "FAIL" and json.loads(text)["gate_evidence"] is True


def test_a_smoke_run_with_a_smaller_workload_is_not_gate_evidence(model_file):
    code, text = run_main([str(model_file), "--passage-tokens", "700"], make_hooks([]))
    report = json.loads(text)

    assert code == 0 and report["gate_evidence"] is False
    assert any("700 tokens" in reason for reason in report["not_gate_evidence_because"])


# --- Windows ------------------------------------------------------------------------------------------------------------------

def test_a_windows_run_is_labelled_and_is_never_gate_evidence(model_file, tmp_path):
    reference = tmp_path / "q8.json"
    reference.write_text(json.dumps(reference_result(source=rss.SOURCE_PSUTIL, peak=1_000_000)), encoding="utf-8")
    hooks = make_hooks([], linux=False, start=1_500_000, step=0)
    code, text = run_main([str(model_file), "--compare", str(reference)], hooks)
    report = json.loads(text)

    assert code == 0 and report["memory_source"] == rss.SOURCE_PSUTIL and report["platform"]["is_linux"] is False
    assert report["gate_evidence"] is False and report["verdict"] == "INDICATIVE_PASS"
    assert any("not Linux" in reason for reason in report["not_gate_evidence_because"])
    assert report["predicted_uvicorn_vmhwm_kb"] == 1_125_116 + 500_000       # the number is still shown, labelled as indicative


def test_a_windows_result_cannot_be_the_reference_of_a_linux_run(model_file, tmp_path):
    reference = tmp_path / "q8.json"
    reference.write_text(json.dumps(reference_result(source=rss.SOURCE_PSUTIL)), encoding="utf-8")
    code, text = run_main([str(model_file), "--compare", str(reference)], make_hooks([], linux=True))

    assert code == 0 and json.loads(text)["verdict"] == "NO_REFERENCE"


# --- packaging ------------------------------------------------------------------------------------------------------------------

def imported_roots(nodes) -> set[str]:
    roots = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
    return roots


def test_the_module_imports_only_the_standard_library_at_top_level_and_nothing_from_tools():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))

    assert imported_roots(tree.body) <= set(sys.stdlib_module_names)
    assert "tools" not in imported_roots(ast.walk(tree))              # runs as a loose file: the package is not in the image


def test_windows_json_w1_steps_and_the_dockerfile_agree_on_where_the_probe_lives():
    windows = json.loads((ROOT / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))["windows"]["W1"]
    steps = " ".join(windows["steps"])
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "/srv/models/embed_rss.py" in steps and "/models/embed_rss.py" in dockerfile
    assert "S6_fly_timing" in steps and "S6_fly_rss" in steps
    assert "KEEP_UNPATCHED=1" in " ".join([windows["note"], steps])
    assert "builder" in windows["note"] and "UNVERIFIED" in windows["note"]
    assert windows["quote_usd"] == 0.08 and windows["cap_usd"] == 0.15           # the owner-facing figures did not move
