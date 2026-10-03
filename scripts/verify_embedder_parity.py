"""Retrieval parity of two query encoders over the REAL corpus vectors: the pre-merge gate for a patched embedder.

    python scripts/verify_embedder_parity.py --baseline models/qwen3-embedding-0.6b-q8/model_q8.onnx \\
        --candidate <patched>/model_q8.onnx [--corpus-glob 'data/processed/embeddings/*.parquet'] \\
        [--questions benchmark.json examples.json] [--threads 4] [--out parity.json]

Baseline = the model production serves today; candidate = the same weights with ``accuracy_level=4`` (M5 decision 2.1).
Both are loaded through ``semigraph.embeddings_onnx.OnnxBackend``, the serving code path with the serving query prompt (the
M5 spike scripts used a prompt with a trailing space and a hand-rolled encoder). Every question is embedded by both, then
ranked by brute-force cosine against the corpus vectors the live site searches (data/processed/embeddings/*.parquet).

Reports query-vector cosine (mean, min, p05), top-8 and top-10 overlap (mean, min, fraction of identical sets), top-1
identical fraction and the questions whose top-1 changed, and the largest gap between the two top-10 score lists. Gates
(pre-registered, fixed before any run): cosine mean >= 0.998 and min >= 0.997, top-1 identical >= 0.95, mean top-8
overlap >= 0.97. Prints one PASS/FAIL line per gate and a RESULT line.

Exit codes: 0 every gate passed; 1 a gate failed; 2 the run itself could not be done (no corpus files, an empty corpus or
question set, a missing model). Dev-only (pandas + pyarrow read the corpus); the metric function needs only numpy.
"""

import argparse
import glob
import importlib.metadata
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_GLOB = str(REPO_ROOT / "data" / "processed" / "embeddings" / "*.parquet")
DEFAULT_QUESTION_FILES = (REPO_ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json",
                          REPO_ROOT / "src" / "semigraph" / "artifacts" / "examples.json")
EMBEDDING_COLUMN = "embedding"
QUESTION_KEYS = ("q", "question")  # benchmark.json uses "q", examples.json uses "question"
QUESTION_MIN_CHARS, QUESTION_MAX_CHARS = 10, 600
TOP_SMALL, TOP_LARGE = 8, 10
NOTABLE_QUESTIONS = 3  # how many lowest-overlap questions the report names

# The gate (M5 decision 2.1). Compared with >=, on the unrounded values.
COSINE_MEAN_MIN = 0.998
COSINE_MIN_MIN = 0.997
TOP1_IDENTICAL_MIN = 0.95
TOP8_OVERLAP_MEAN_MIN = 0.97

Encoder = object  # anything with encode_query(question) -> list[float], like semigraph.embeddings_onnx.OnnxBackend
LoadEncoder = Callable[[str, int], Encoder]
LoadCorpus = Callable[[str], np.ndarray]


class GateResult(NamedTuple):
    name: str
    value: float
    threshold: float
    passed: bool


# ------------------------------------------------------------------------------------------ questions and corpus

def collect_questions(obj) -> list[str]:
    """Question strings in a JSON document: a plain list of strings, or every ``q`` / ``question`` value anywhere in it."""
    if isinstance(obj, list) and obj and all(isinstance(item, str) for item in obj):
        candidates = obj
    else:
        candidates = []
        _walk(obj, candidates)
    stripped = (text.strip() for text in candidates)
    return [text for text in stripped if QUESTION_MIN_CHARS < len(text) < QUESTION_MAX_CHARS]


def _walk(node, out: list[str]) -> None:
    if isinstance(node, dict):
        out.extend(node[key] for key in QUESTION_KEYS if isinstance(node.get(key), str))
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out)


def load_questions(paths: Sequence[Path | str]) -> list[str]:
    questions: list[str] = []
    for path in paths:
        questions.extend(collect_questions(json.loads(Path(path).read_text(encoding="utf-8"))))
    unique = list(dict.fromkeys(questions))
    if not unique:
        raise ValueError(f"no questions found in {', '.join(str(p) for p in paths)}")
    return unique


def load_corpus(pattern: str) -> np.ndarray:
    """The ``embedding`` column of every parquet file matching ``pattern`` (name order), stacked: (chunks, dim)."""
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no parquet files match {pattern}")
    import pandas as pd  # dev-only, imported after the cheap failure above

    rows: list[np.ndarray] = []
    for file in files:
        frame = pd.read_parquet(file)
        if EMBEDDING_COLUMN not in frame.columns:
            raise ValueError(f"{file} has no '{EMBEDDING_COLUMN}' column")
        rows.extend(np.asarray(vector, dtype=np.float64) for vector in frame[EMBEDDING_COLUMN])
    return np.vstack(rows)


# ------------------------------------------------------------------------------------------ metrics and gates

def _unit_rows(vectors: np.ndarray) -> np.ndarray:
    v = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.where(norms == 0.0, 1.0, norms)


def _check_inputs(baseline: np.ndarray, candidate: np.ndarray, corpus: np.ndarray) -> None:
    if corpus.ndim != 2 or corpus.shape[0] == 0:
        raise ValueError("the corpus is empty: there is nothing to rank the questions against")
    if corpus.shape[0] < TOP_SMALL:  # top-8 overlap over fewer than 8 vectors is trivially 1.0: the gate would be vacuous
        raise ValueError(f"the corpus has {corpus.shape[0]} vectors; the top-{TOP_SMALL} overlap gate needs at least {TOP_SMALL}")
    if baseline.ndim != 2 or baseline.shape[0] == 0:
        raise ValueError("no questions to compare")
    if baseline.shape != candidate.shape:
        raise ValueError(f"baseline and candidate query arrays differ in shape: {baseline.shape} vs {candidate.shape}")
    if baseline.shape[1] != corpus.shape[1]:
        raise ValueError(f"query dimension {baseline.shape[1]} does not match corpus dimension {corpus.shape[1]}")


def _overlaps(order_a: np.ndarray, order_b: np.ndarray, k: int) -> list[float]:
    return [len(set(a[:k]) & set(b[:k])) / k for a, b in zip(order_a, order_b, strict=True)]


def _overlap_summary(values: list[float]) -> dict:
    return {"mean": float(np.mean(values)), "min": float(min(values)),
            "identical_set_fraction": sum(1 for v in values if v == 1.0) / len(values)}


def compute_parity_metrics(baseline_queries: np.ndarray, candidate_queries: np.ndarray, corpus: np.ndarray) -> dict:
    """Parity of two query-vector sets over ``corpus`` (all arrays are re-normalised; ties rank by corpus order)."""
    raw = [np.asarray(a, dtype=np.float64) for a in (baseline_queries, candidate_queries, corpus)]
    _check_inputs(*raw)
    base, cand, docs = (_unit_rows(a) for a in raw)
    scores_base, scores_cand = base @ docs.T, cand @ docs.T
    order_base = np.argsort(-scores_base, axis=1, kind="stable")
    order_cand = np.argsort(-scores_cand, axis=1, kind="stable")
    k_small, k_large = min(TOP_SMALL, docs.shape[0]), min(TOP_LARGE, docs.shape[0])
    cosine = np.einsum("ij,ij->i", base, cand)
    overlap_small = _overlaps(order_base, order_cand, k_small)
    overlap_large = _overlaps(order_base, order_cand, k_large)
    top_base, top_cand = order_base[:, 0], order_cand[:, 0]
    top1_same = top_base == top_cand
    rows = np.arange(base.shape[0])
    # How near a tie each flip was, as each encoder scored the two documents (0 when the top-1 did not change).
    gap_base = scores_base[rows, top_base] - scores_base[rows, top_cand]
    gap_cand = scores_cand[rows, top_cand] - scores_cand[rows, top_base]
    gap = np.abs(np.take_along_axis(scores_base, order_base[:, :k_large], axis=1)
                 - np.take_along_axis(scores_cand, order_cand[:, :k_large], axis=1)).max()
    return {
        "questions": int(base.shape[0]), "corpus_vectors": int(docs.shape[0]),
        "query_cosine": {"mean": float(cosine.mean()), "min": float(cosine.min()),
                         "p05": float(np.percentile(cosine, 5))},
        "top8_overlap": _overlap_summary(overlap_small), "top10_overlap": _overlap_summary(overlap_large),
        "top1_identical_fraction": float(top1_same.mean()), "top1_changed_questions": int((~top1_same).sum()),
        "max_top10_score_gap": float(gap),
        "per_question": {"cosine": cosine.tolist(), "top8_overlap": overlap_small, "top10_overlap": overlap_large,
                         "top1_same": top1_same.tolist(), "top1_baseline_index": top_base.tolist(),
                         "top1_candidate_index": top_cand.tolist(), "top1_baseline_gap": gap_base.tolist(),
                         "top1_candidate_gap": gap_cand.tolist()},
    }


def evaluate_gates(metrics: dict) -> list[GateResult]:
    checks = (
        ("query cosine mean", metrics["query_cosine"]["mean"], COSINE_MEAN_MIN),
        ("query cosine min", metrics["query_cosine"]["min"], COSINE_MIN_MIN),
        ("top-1 identical fraction", metrics["top1_identical_fraction"], TOP1_IDENTICAL_MIN),
        ("mean top-8 overlap", metrics["top8_overlap"]["mean"], TOP8_OVERLAP_MEAN_MIN),
    )
    return [GateResult(name, value, threshold, bool(value >= threshold)) for name, value, threshold in checks]


def format_gate_line(gate: GateResult) -> str:
    return f"{'PASS' if gate.passed else 'FAIL'} {gate.name} {gate.value:.6f} >= {gate.threshold:g}"


# ------------------------------------------------------------------------------------------ encoders and the run

def load_onnx_encoder(model_path: str, threads: int) -> Encoder:
    """The serving encoder (``tokenizer.json`` beside the model, as in production)."""
    from semigraph.embeddings_onnx import OnnxBackend

    return OnnxBackend(model_path, threads=threads)


def embed_queries(encoder: Encoder, questions: Sequence[str]) -> np.ndarray:
    return np.vstack([np.asarray(encoder.encode_query(q), dtype=np.float64) for q in questions])


def _embed_with(load_encoder: LoadEncoder, model_path: str, threads: int, questions: Sequence[str]) -> np.ndarray:
    """Embed everything, then let the encoder go: the two ~1 GB sessions are never alive together."""
    return embed_queries(load_encoder(model_path, threads), questions)


def _onnxruntime_version() -> str | None:
    try:
        return importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return None


def _query_prompt() -> str | None:
    """The prompt the serving code prepends to every query (recorded so a report says what was actually measured)."""
    try:
        from semigraph.embeddings import QUERY_PROMPT
    except ImportError:
        return None
    return QUERY_PROMPT


def _flip_details(questions: Sequence[str], per: dict) -> list[dict]:
    return [{"q": questions[i][:100], "baseline_top1": per["top1_baseline_index"][i],
             "candidate_top1": per["top1_candidate_index"][i], "baseline_score_gap": per["top1_baseline_gap"][i],
             "candidate_score_gap": per["top1_candidate_gap"][i]}
            for i, same in enumerate(per["top1_same"]) if not same]


def build_report(args: argparse.Namespace, metrics: dict, gates: list[GateResult], questions: Sequence[str],
                 elapsed: float) -> dict:
    per = metrics["per_question"]
    lowest = sorted(range(len(questions)), key=lambda i: (per["top10_overlap"][i], per["top8_overlap"][i]))
    flips = _flip_details(questions, per)
    return {
        "baseline": args.baseline, "candidate": args.candidate, "corpus_glob": args.corpus_glob,
        "corpus_vectors": metrics["corpus_vectors"], "questions": metrics["questions"],
        "question_files": [str(p) for p in args.questions], "threads": args.threads,
        "onnxruntime": _onnxruntime_version(), "query_prompt": _query_prompt(),
        "metrics": {k: v for k, v in metrics.items() if k != "per_question"},
        "gates": [g._asdict() for g in gates], "passed": all(g.passed for g in gates),
        "changed_top1_questions": [flip["q"] for flip in flips], "changed_top1_details": flips,
        "lowest_overlap_questions": [{"top10_overlap": per["top10_overlap"][i], "top8_overlap": per["top8_overlap"][i],
                                      "q": questions[i][:100]} for i in lowest[:NOTABLE_QUESTIONS]],
        "elapsed_s": round(elapsed, 1),
    }


def run(args: argparse.Namespace, *, load_encoder: LoadEncoder | None = None,
        corpus_loader: LoadCorpus | None = None) -> int:
    load_encoder = load_encoder or load_onnx_encoder
    corpus_loader = corpus_loader or load_corpus
    started = time.perf_counter()
    if Path(args.baseline).resolve() == Path(args.candidate).resolve():
        print("ERROR: --baseline and --candidate are the same file: every parity gate would pass trivially",
              file=sys.stderr)
        return 2
    try:
        questions = load_questions(args.questions)
        corpus = corpus_loader(args.corpus_glob)  # cheap failures first, before the two model loads
        baseline = _embed_with(load_encoder, args.baseline, args.threads, questions)
        candidate = _embed_with(load_encoder, args.candidate, args.threads, questions)
        metrics = compute_parity_metrics(baseline, candidate, corpus)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    gates = evaluate_gates(metrics)
    for gate in gates:
        print(format_gate_line(gate))
    passed = all(g.passed for g in gates)
    print(f"RESULT {'PASS' if passed else 'FAIL'}")
    report = build_report(args, metrics, gates, questions, time.perf_counter() - started)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if passed else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--baseline", required=True, metavar="MODEL", help="the model production serves today (model_q8.onnx)")
    ap.add_argument("--candidate", required=True, metavar="MODEL", help="the patched model to approve")
    ap.add_argument("--corpus-glob", default=DEFAULT_CORPUS_GLOB, help="parquet files with an 'embedding' column")
    ap.add_argument("--questions", nargs="+", default=[str(p) for p in DEFAULT_QUESTION_FILES], metavar="JSON",
                    help="JSON files read for q / question values (default: the benchmark and the examples)")
    ap.add_argument("--threads", type=int, default=4, help="ONNX Runtime intra-op threads per session (0 = its default)")
    ap.add_argument("--out", help="write the full report (metrics, gates, notable questions) here as JSON")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # an ONNX Runtime load/run error must not look like a gate FAIL (exit 1)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
