"""Interleaved single-question timing of a baseline and a candidate query embedder (M5 decision 2.1, gate 3).

    python scripts/embedder_timing.py BASELINE_MODEL CANDIDATE_MODEL [-n 50] [--threads 1] [--warmup 2]
                                      [--tokenizer tokenizer.json] [--out timing.json]

Both models are loaded in this process. Every round embeds ONE question with each of them (same question, same thread
count), alternating who goes first (A B, B A, A B, ...) so that neither always runs first in its pair, and slow drift such
as a shared Fly vCPU running out of burst balance hits both alike. ``-n`` rounds give ``n`` timed calls per model; the
pre-registered gate asks for at least 50. Each call is timed around the whole query embed (tokenize, run, pool, normalise).
The first ``--warmup`` rounds are run and discarded.

Prints the median and p90 per model, the median speedup, and RESULT PASS / RESULT FAIL; the exit code is 0 only if the
candidate's median is strictly lower than the baseline's, 1 if it is not, 2 if the run could not be done (a missing model
or tokenizer).

The tokenizer is ``tokenizer.json`` beside each model unless --tokenizer is given. Memory: the two sessions are alive
together (about 1.1 GB each for the 8-bit Qwen3 model), so on a 4 GB machine that is also serving the app, expect about
2.2 GB on top of it; use a temporary machine of the same class rather than the live one.

SELF-CONTAINED ON PURPOSE: only the standard library, numpy, onnxruntime and tokenizers are imported (no pandas, no onnx
package, no semigraph), so this single file can be copied into a Fly machine of the serve image and run there. The encoder
below mirrors semigraph.embeddings_onnx.OnnxBackend.encode_query (tests pin it to the serving backend and prompt).
"""

import argparse
import importlib.metadata
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

DEFAULT_N = 50
GATE_MIN_CALLS = 50  # the pre-registered minimum number of interleaved calls
DEFAULT_WARMUP = 2
MAX_TOKENS = 8192  # the serving backend's truncation

# Identical to semigraph.embeddings.QUERY_PROMPT (pinned by tests/test_embedder_timing.py): no trailing space.
QUERY_PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"

# Realistic questions of the sizes the live site embeds (10 to 50 tokens after the prompt), cycled when n is larger.
QUESTIONS: tuple[str, ...] = (
    "What export-control risks does Nvidia report in its latest annual filing?",
    "Which foundries does Qualcomm rely on to manufacture its chips?",
    "What was Micron's net income for the fiscal year ended August 28, 2025?",
    "What does Intel disclose about U.S. export controls affecting its business?",
    "Compare the competition risks disclosed by AMD and Nvidia.",
    "Which companies does Nvidia depend on for chip manufacturing and assembly?",
    "Did Meta remove any risk factors between its FY2024 and FY2025 annual reports?",
    "What was Nvidia's total revenue for the fiscal year ended January 26, 2025?",
    "By what percentage did Broadcom's total revenue change from the fiscal year ended November 3, 2024 to the "
    "fiscal year ended November 2, 2025?",
    "Which memory suppliers does Nvidia buy HBM or memory from?",
    "How does Apple describe its dependence on single-source suppliers in Asia?",
    "What risks does Microsoft disclose about artificial intelligence regulation?",
)

Embed = Callable[[str], object]  # one question in, anything out (the vector is not needed for timing)
MakeEncoder = Callable[[Path, Path, int], object]  # (model, tokenizer.json, threads) -> object with embed_query()


class QueryEncoder:
    """Query embedding exactly as the serving backend does it: prompt + question, last-token pooling, L2 normalisation."""

    def __init__(self, model_path: Path | str, tokenizer_path: Path | str, threads: int = 1) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self._inputs = self.session.get_inputs()

    def _feeds(self, ids: np.ndarray) -> dict:
        feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
        for inp in self._inputs:
            if inp.name == "position_ids":
                feeds[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
            elif inp.name.startswith("past_key_values"):
                shape = [1 if isinstance(d, str) else d for d in inp.shape]
                shape[2] = 0  # empty cache: the graph is a plain encoder pass for us
                feeds[inp.name] = np.zeros(shape, dtype=np.float16 if "float16" in inp.type else np.float32)
        return feeds

    def embed_query(self, question: str) -> np.ndarray:
        ids = np.array([self.tokenizer.encode(QUERY_PROMPT + question).ids], dtype=np.int64)
        out = self.session.run(["last_hidden_state"], self._feeds(ids))[0]
        vec = out[0, -1, :].astype(np.float32)
        return vec / np.linalg.norm(vec)


# ------------------------------------------------------------------------------------------ statistics and ordering

def _describe(seconds: np.ndarray) -> dict:
    return {"median_s": float(np.median(seconds)), "p90_s": float(np.percentile(seconds, 90)),
            "mean_s": float(seconds.mean()), "min_s": float(seconds.min()), "max_s": float(seconds.max())}


def summarize_timings(baseline_s: Sequence[float], candidate_s: Sequence[float]) -> dict:
    """Median, p90 (linear interpolation, numpy's default), mean, min, max per model, the speedup of the medians and the
    verdict: the candidate must be strictly faster by MEDIAN, which a few throttled outliers cannot move."""
    base, cand = np.asarray(baseline_s, dtype=np.float64), np.asarray(candidate_s, dtype=np.float64)
    if base.shape != cand.shape or base.ndim != 1:
        raise ValueError(f"need two equal-length lists of timings, got {base.shape} and {cand.shape}")
    if base.size == 0:
        raise ValueError("no timings to summarize")
    if not (np.isfinite(base).all() and np.isfinite(cand).all() and (base > 0).all() and (cand > 0).all()):
        raise ValueError("timings must be positive, finite seconds")
    baseline, candidate = _describe(base), _describe(cand)
    return {"n": int(base.size), "baseline": baseline, "candidate": candidate,
            "speedup_median": baseline["median_s"] / candidate["median_s"],
            "candidate_faster": bool(candidate["median_s"] < baseline["median_s"]),
            "meets_gate_minimum_calls": bool(base.size >= GATE_MIN_CALLS)}


def pick_questions(n: int, questions: Sequence[str] = QUESTIONS) -> list[str]:
    return [questions[i % len(questions)] for i in range(n)]


def run_interleaved(embed_baseline: Embed, embed_candidate: Embed, questions: Sequence[str], *,
                    clock: Callable[[], float] = time.perf_counter) -> tuple[list[float], list[float]]:
    """One timed call per model per question; the order alternates by round (baseline first on even rounds)."""
    baseline_s: list[float] = []
    candidate_s: list[float] = []
    for round_no, question in enumerate(questions):
        pair = [(embed_baseline, baseline_s), (embed_candidate, candidate_s)]
        for embed, sink in pair if round_no % 2 == 0 else reversed(pair):
            started = clock()
            embed(question)
            sink.append(clock() - started)
    return baseline_s, candidate_s


# ------------------------------------------------------------------------------------------ command line and the run

def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("baseline", help="the model to beat (for example the unpatched model_q8.onnx)")
    ap.add_argument("candidate", help="the model to approve (for example the accuracy_level=4 model_q8.onnx)")
    ap.add_argument("-n", "--n", type=_positive, default=DEFAULT_N,
                    help=f"timed calls per model (default {DEFAULT_N}; the gate wants at least {GATE_MIN_CALLS})")
    ap.add_argument("--threads", type=_non_negative, default=1,
                    help="ONNX Runtime intra-op threads per session (default 1; 0 = its default)")
    ap.add_argument("--warmup", type=_non_negative, default=DEFAULT_WARMUP, help="discarded rounds before timing")
    ap.add_argument("--tokenizer", help="tokenizer.json for both models (default: the one beside each model)")
    ap.add_argument("--out", help="write the full timing report here as JSON")
    return ap.parse_args(argv)


def resolve_tokenizer(model: str, override: str | None) -> Path:
    path = Path(override) if override else Path(model).with_name("tokenizer.json")
    if not path.is_file():
        raise FileNotFoundError(f"no tokenizer.json at {path}; pass --tokenizer")
    return path


def _onnxruntime_version() -> str | None:
    try:
        return importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return None


def print_summary(summary: dict) -> None:
    for label in ("baseline", "candidate"):
        d = summary[label]
        print(f"{label:<9} median {d['median_s']:.4f} s   p90 {d['p90_s']:.4f} s   (n={summary['n']})")
    print(f"speedup (median) {summary['speedup_median']:.2f}x")
    print(f"RESULT {verdict(summary)}")


def verdict(summary: dict) -> str:
    """PASS only when the candidate is faster over at least GATE_MIN_CALLS interleaved calls (gate 3, M5 decision 2.1)."""
    if not summary["meets_gate_minimum_calls"]:
        return f"INCONCLUSIVE ({summary['n']} calls is below the gate's minimum of {GATE_MIN_CALLS})"
    return "PASS" if summary["candidate_faster"] else "FAIL"


def run(args: argparse.Namespace, *, make_encoder: MakeEncoder = QueryEncoder,
        clock: Callable[[], float] = time.perf_counter) -> int:
    try:
        tokenizers = [resolve_tokenizer(model, args.tokenizer) for model in (args.baseline, args.candidate)]
        baseline = make_encoder(Path(args.baseline), tokenizers[0], args.threads)
        candidate = make_encoder(Path(args.candidate), tokenizers[1], args.threads)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    warmup = pick_questions(args.warmup)
    run_interleaved(baseline.embed_query, candidate.embed_query, warmup, clock=clock)  # discarded
    baseline_s, candidate_s = run_interleaved(baseline.embed_query, candidate.embed_query, pick_questions(args.n),
                                              clock=clock)
    summary = summarize_timings(baseline_s, candidate_s)
    print_summary(summary)
    if args.out:
        report = {**summary, "baseline_model": args.baseline, "candidate_model": args.candidate, "threads": args.threads,
                  "warmup": args.warmup, "onnxruntime": _onnxruntime_version()}
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if verdict(summary) == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # an ONNX Runtime load/run error must not look like a gate FAIL (exit 1)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
