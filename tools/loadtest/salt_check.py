"""Council 5's offline checks of the per-ask salt, run BEFORE any staging window (developer side; imports ``semigraph``).

For every pool question (and every agent question under the ``agent`` strategy) and each of three adversarial salts
(:data:`tools.loadtest.salt.ADVERSARIAL_SALTS`), the salted question must be decided exactly as the unsalted one by every
piece of server code that reads the question text:

* ``guard.validate_question`` accepts it and the result is within ``max_question_chars`` (500);
* ``retriever.mentioned_periods`` (the year / date detector), ``retriever.pair_selection_mode`` (which filing pair a
  change question reads), ``retriever.detect_anchors`` + ``cap_anchors`` (company and alias detection) and
  ``router.needs_strong_model`` (the draft-or-strong route) return the same thing;

and across the whole send schedule (every worker digit x several counters x every question) ``store.cache_key`` must give as
many DISTINCT keys as there are sends, none equal to an unsalted pool or cached-example key (the cached examples are sent
unsalted and must keep hitting; the salted asks must never hit).

The opt-in local-graph check (``LOADTEST_SALT_CHECK_LOCAL_GRAPH=1``; needs the local Neo4j graph and the embedder that will
ship) retrieves a sample of questions with and without the salt and requires a MEAN top-8 chunk overlap of at least 0.90 and a
MEDIAN context size within 5 %. It is not run by tests (no database), only unit-tested with a fake probe.

The result is written as JSON (``--out``) for ``scripts/loadtest_report.py``, whose VOID rule includes "an offline check
failed". If a check misses, council 5 allows ONE fallback salt format; after that the decision goes to the owner.

    PYTHONPATH=. uv run python -m tools.loadtest.salt_check --out artifacts/loadtest/<date>/salt_check.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tools.loadtest import salt
from tools.loadtest.pool import Pool, load_pool

CHECK_VERSION = 1
LOCAL_GRAPH_ENV = "LOADTEST_SALT_CHECK_LOCAL_GRAPH"
SAMPLE_ENV = "LOADTEST_SALT_CHECK_SAMPLE"
TOP_K = 8
MIN_MEAN_OVERLAP = 0.90
MAX_MEDIAN_CONTEXT_DELTA = 0.05
DEFAULT_SAMPLE = 60
SCHEDULE_COUNTERS = (0, 1, 7, 1899, 1900, 1999, 2000, 2024, 2099, 2100, 99_999, 999_999)   # counters in the distinct-key sweep
MAX_FAILURES_LISTED = 20
CACHE_KEY_TEMPLATE = "salt-check"                      # constant: only the question and strategy vary in the comparison

Probe = Callable[[str], dict]                          # question -> {"top8": [chunk ids], "context_chars": int}


def _decisions(question: str) -> dict:
    """Everything the server decides from the question text, as plain comparable data."""
    from semigraph.retrieval import retriever, router

    periods = retriever.mentioned_periods(question)
    kept, dropped = retriever.cap_anchors(retriever.detect_anchors(question))
    return {"periods": periods, "pair_mode": retriever.pair_selection_mode(question, periods),
            "anchors": [kept, dropped], "strong": router.needs_strong_model(question)}


def _validated(question: str, max_chars: int) -> str:
    from semigraph.serve import guard

    return guard.validate_question(question, max_chars)


def _questions(pool: Pool) -> list[tuple[str, str, str]]:
    """``(id, text, strategy)``: every live question as ``hybrid``, every agent question again as ``agent``."""
    return [(q.id, q.text, "hybrid") for q in pool.live] + [(f"{q.id}#agent", q.text, "agent") for q in pool.agent]


def _decision_checks(pool: Pool, salts: Sequence[tuple[int, int]], max_chars: int) -> tuple[dict, list[dict]]:
    failures: list[dict] = []
    longest, compared = 0, 0
    for qid, text, _strategy in _questions(pool):
        base = _decisions(text)
        base_valid = _validated(text, max_chars)
        for worker, n in salts:
            salted = salt.apply_salt(text, worker, n)
            compared += 1
            try:
                valid = _validated(salted, max_chars)
            except Exception as e:                                    # noqa: BLE001 - any refusal is a failed check
                failures.append({"check": "validate_question", "id": qid, "salt": salt.salt_token(worker, n),
                                 "detail": f"{type(e).__name__}: {getattr(e, 'detail', e)}"})
                continue
            longest = max(longest, len(valid))
            if valid != base_valid + salt.salt_suffix(worker, n):
                failures.append({"check": "validate_question", "id": qid, "salt": salt.salt_token(worker, n),
                                 "detail": "the normalized salted question is not the normalized question + the suffix"})
            after = _decisions(salted)
            for name in base:
                if after[name] != base[name]:
                    failures.append({"check": name, "id": qid, "salt": salt.salt_token(worker, n),
                                     "detail": f"{base[name]!r} -> {after[name]!r}"})
    checks = {"decisions": {"questions": len(_questions(pool)), "salts": len(salts), "comparisons": compared,
                            "longest_salted_chars": longest, "max_chars": max_chars}}
    return checks, failures


def _distinct_key_check(pool: Pool, example_questions: Sequence[str]) -> tuple[dict, list[dict]]:
    """``store.cache_key`` over the whole send schedule: distinct keys == sends, and no salted key is an unsalted one."""
    from semigraph.serve import store

    unsalted = {store.cache_key(q.text, "hybrid", "S", template=CACHE_KEY_TEMPLATE) for q in pool.live}
    unsalted |= {store.cache_key(q.text, "agent", "S", template=CACHE_KEY_TEMPLATE) for q in pool.agent}
    unsalted |= {store.cache_key(text, "hybrid", "S", template=CACHE_KEY_TEMPLATE) for text in example_questions}
    sent: set[str] = set()
    sends = 0
    for _, text, strategy in _questions(pool):
        for worker in range(salt.MAX_WORKER + 1):
            for n in SCHEDULE_COUNTERS:
                sends += 1
                sent.add(store.cache_key(salt.apply_salt(text, worker, n), strategy, "S", template=CACHE_KEY_TEMPLATE))
    failures = []
    if len(sent) != sends:
        failures.append({"check": "cache_keys_distinct", "id": "-", "salt": "-",
                         "detail": f"{sends} sends made {len(sent)} distinct store.cache_key values"})
    if sent & unsalted:
        failures.append({"check": "cache_keys_unsalted_overlap", "id": "-", "salt": "-",
                         "detail": f"{len(sent & unsalted)} salted keys equal an unsalted pool or example key"})
    return {"cache_keys": {"sends": sends, "distinct": len(sent), "unsalted_keys": len(unsalted)}}, failures


def local_graph_check(pool: Pool, probe: Probe, salts: Sequence[tuple[int, int]], *, sample: int) -> dict:
    """Mean top-8 overlap and median context-size delta between each sampled question and its salted twin, via ``probe``."""
    questions = [q.text for q in pool.live][:: max(1, len(pool.live) // max(sample, 1))][:sample]
    overlaps: list[float] = []
    deltas: list[float] = []
    for text in questions:
        base = probe(text)
        for worker, n in salts:
            twin = probe(salt.apply_salt(text, worker, n))
            ids, twin_ids = list(base["top8"])[:TOP_K], list(twin["top8"])[:TOP_K]
            overlaps.append(len(set(ids) & set(twin_ids)) / max(len(set(ids)), 1))
            deltas.append(abs(twin["context_chars"] - base["context_chars"]) / max(base["context_chars"], 1))
    mean_overlap, median_delta = statistics.fmean(overlaps), statistics.median(deltas)
    return {"requested": True, "ran": True, "questions": len(questions), "salts": len(salts), "comparisons": len(overlaps),
            "mean_top8_overlap": round(mean_overlap, 4), "median_context_delta": round(median_delta, 4),
            "ok": mean_overlap >= MIN_MEAN_OVERLAP and median_delta <= MAX_MEDIAN_CONTEXT_DELTA,
            "limits": {"mean_top8_overlap_min": MIN_MEAN_OVERLAP, "median_context_delta_max": MAX_MEDIAN_CONTEXT_DELTA}}


def build_local_probe() -> Probe:
    """The real probe: hybrid retrieval over the local graph with the embedder that ships. Needs Neo4j and the model files."""
    from semigraph.embeddings import Embedder
    from semigraph.graph.client import get_driver
    from semigraph.retrieval import answerer, retriever

    driver, embedder = get_driver(), Embedder()

    def probe(question: str) -> dict:
        result = retriever.hybrid_retrieve(question, driver, embedder)
        _, context, _ = answerer.build_blocks(result)
        return {"top8": [c["chunk_id"] for c in result["chunks"]][:TOP_K], "context_chars": len(context)}

    return probe


def run_checks(pool: Pool, *, salts: Sequence[tuple[int, int]] = salt.ADVERSARIAL_SALTS,
               max_chars: int = salt.MAX_QUESTION_CHARS, local_probe: Probe | None = None,
               local_requested: bool = False, sample: int = DEFAULT_SAMPLE) -> dict:
    """All checks, as the JSON document the report reads (``ok`` is the one bit it needs)."""
    decision_info, failures = _decision_checks(pool, salts, max_chars)
    key_info, key_failures = _distinct_key_check(pool, [e["question"] for e in pool.examples])
    failures += key_failures
    local: dict = {"requested": local_requested, "ran": False}
    if local_requested:
        try:
            probe = local_probe or build_local_probe()
            local = local_graph_check(pool, probe, salts, sample=sample)
        except Exception as e:                                         # noqa: BLE001 - requested but could not run = not ok
            local = {"requested": True, "ran": False, "ok": False, "error": f"{type(e).__name__}: {e}"}
    names = ("validate_question", "periods", "pair_mode", "anchors", "strong", "cache_keys_distinct",
             "cache_keys_unsalted_overlap")
    failed = {f["check"] for f in failures}
    checks = [{"name": n, "ok": n not in failed} for n in names]
    ok = not failures and (not local_requested or local.get("ok") is True)
    return {"version": CHECK_VERSION, "ok": ok, "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "pool_sha256": pool.sha256, "pool_size": len(pool.live),
            "salts": [{"worker": w, "n": n, "token": salt.salt_token(w, n)} for w, n in salts],
            "salt_format": salt.SALT_FORMAT, **decision_info, **key_info, "checks": checks,
            "local_graph": local, "failures": failures[:MAX_FAILURES_LISTED], "failure_count": len(failures)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Council 5's offline salt checks (no staging, no provider calls).")
    parser.add_argument("--pool", type=Path, default=None, help="pool.json (default: the committed one)")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON result here")
    args = parser.parse_args(argv)
    requested = os.environ.get(LOCAL_GRAPH_ENV, "") not in ("", "0")
    sample = int(os.environ.get(SAMPLE_ENV) or DEFAULT_SAMPLE)
    result = run_checks(load_pool(args.pool), local_requested=requested, sample=sample)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(("OFFLINE SALT CHECKS: PASS" if result["ok"] else "OFFLINE SALT CHECKS: FAIL")
          + ("" if requested else f"  (the local-graph top-8 / context check did not run: set {LOCAL_GRAPH_ENV}=1)"),
          file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
