"""Benchmark runner — ported from notebook 14 (Milestone M6).

Runs the packaged 20-question gold benchmark over the systems under test
(entity-first hybrid vs vector-only baseline), then scores with the same
programmatic checks + LLM judges that produced the M6 result
(hybrid 100% correct / 0.865 faithful / 0 hallucinated citations).

Battle scars preserved:
- the faithfulness judge sees the FULL context the answering model saw
  (graph blocks + excerpts), truncated at 24000 chars — judging against
  excerpts alone was a real metric bug (0.95 correct yet "0.36 faithful")
- faithfulness/correctness judges run on Sonnet (settings.llm_model);
  per-chunk relevance runs on Haiku (settings.critic_model) with
  thinking_off=False (Haiku is thinking-off by default)
- every judge call goes through the hardened ``semigraph.llm.llm_json``
- runs are checkpointed per (question, system) in an append-only jsonl so
  an interrupted (paid) benchmark resumes instead of re-spending

Output artifacts (notebook 14 paths, parameterized):
- ``<settings.processed_dir>/eval_runs.jsonl``  (append-only run log)
- ``<artifacts_dir>/eval_scores.json``          (per-run score rows)
- ``<artifacts_dir>/eval_report.json``          (overall + by-type summary)
"""

import json
import logging
import re
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from ..artifacts import load_benchmark, read_prompt
from ..llm import llm_json
from ..retrieval.answerer import answer

logger = logging.getLogger("semigraph.eval")


# --- judge response schemas (verbatim from notebook 14) ---
class Faithfulness(BaseModel):
    total_claims: int
    supported_claims: int


class Relevance(BaseModel):
    verdicts: list[bool]


class Correct(BaseModel):
    correct: bool
    reason: str


# Verbatim notebook 14 judge prompts (packaged as template files).
FAITH_PROMPT = read_prompt("faith_judge")
REL_PROMPT = read_prompt("relevance_judge")
JUDGE_PROMPT = read_prompt("correctness_judge")

# Verbatim notebook 14 programmatic patterns.
REFUSAL_PAT = re.compile(r"does not (contain|include|provide)|not available|no (information|data|filings)|"
                         r"cannot (be )?(determin|answer|find)|isn't|is not in the (context|filings|corpus)|not an SEC filer", re.I)
NUM_PAT = re.compile(r"\$?([0-9][0-9,\.]*)\s*(billion|bn|b\b|million|mn|m\b|trillion)?", re.I)


def parse_numbers(text: str) -> list[float]:
    """Extract dollar/scale-suffixed numbers as absolute values
    (notebook 14 numeric-consistency check)."""
    out = []
    for m in NUM_PAT.finditer(text.replace(",", "")):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        mult = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
                "m": 1e6, "trillion": 1e12}.get(unit, 1)
        out.append(v * mult)
    return out


def run_systems(benchmark: list[dict], driver, embedder, systems, results_path: Path,
                llm=None) -> list[dict]:
    """Answer every (question, system) pair, checkpointed per pair in an
    append-only jsonl (notebook 14 section 3). Returns all logged runs."""
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if results_path.exists():
        done = {(json.loads(l)["id"], json.loads(l)["system"])
                for l in results_path.open(encoding="utf-8") if l.strip()}
    logger.info("%d runs checkpointed — resuming", len(done))

    with results_path.open("a", encoding="utf-8") as sink:
        for q in benchmark:
            for system in systems:
                if (q["id"], system) in done:
                    continue
                a = answer(q["q"], driver, embedder, strategy=system, llm=llm)
                sink.write(json.dumps({"id": q["id"], "system": system, "type": q["type"], "q": q["q"],
                                       "answer": a["answer"], "cited": sorted(a["cited"]),
                                       "valid_ids": sorted(a["valid_ids"]),
                                       "hallucinated": sorted(a["hallucinated"]),
                                       "context": a["context"],
                                       "chunk_texts": {c["chunk_id"]: c["text"] for c in a["retrieval"]["chunks"]}}) + "\n")
                sink.flush()
                logger.info("  %s/%s done", q["id"], system)
    return [json.loads(l) for l in results_path.open(encoding="utf-8") if l.strip()]


def score_runs(runs: list[dict], benchmark: list[dict], *, judge=None,
               judge_model: str | None = None, critic_model: str | None = None) -> list[dict]:
    """Score runs: programmatic checks + LLM judges (notebook 14 section 4).

    ``judge`` is an injectable ``llm_json``-compatible callable
    ``(prompt, model_cls, *, model=..., max_tokens=..., thinking_off=...)``.
    """
    judge = judge or llm_json
    bench_by_id = {b["id"]: b for b in benchmark}
    scored = []
    for run in runs:
        b = bench_by_id[run["id"]]
        row = {"id": run["id"], "system": run["system"], "type": run["type"]}
        row["citation_ok"] = len(run["hallucinated"]) == 0
        row["n_citations"] = len(run["cited"])
        ans = run["answer"]
        # correctness
        if run["type"] == "refusal":
            row["correct"] = bool(REFUSAL_PAT.search(ans))
        elif "expect" in b and "value" in b["expect"]:
            target = b["expect"]["value"]
            row["correct"] = any(abs(v - target) / target < 0.005 for v in parse_numbers(ans))
        elif "expect" in b and "any_of" in b["expect"]:
            row["correct"] = any(s.lower() in ans.lower() for s in b["expect"]["any_of"])
        else:
            v = judge(JUDGE_PROMPT.format(q=b["q"], notes=b.get("judge_notes", ""), a=ans[:4000]),
                      Correct, model=judge_model, max_tokens=300)
            row["correct"] = v.correct
        # faithfulness (skip refusals — nothing to fact-check)
        if run["type"] != "refusal":
            # judge against the FULL context the answering model saw (graph blocks + excerpts) —
            # judging hybrid answers against excerpts alone falsely marks graph-derived claims unsupported
            ctx = run.get("context") or "\n".join(f"[{cid}] {t[:600]}" for cid, t in run["chunk_texts"].items())
            ctx = ctx[:24000]
            f = judge(FAITH_PROMPT.format(q=b["q"], a=ans[:4000], ctx=ctx or "(no context retrieved)"),
                      Faithfulness, model=judge_model, max_tokens=200)
            row["faithfulness"] = (f.supported_claims / f.total_claims) if f.total_claims else 1.0
        # context precision (Haiku, one call per run)
        if run["chunk_texts"]:
            numbered = "\n".join(f"{i+1}. {t[:350]}" for i, t in enumerate(run["chunk_texts"].values()))
            rel = judge(REL_PROMPT.format(q=b["q"], chunks=numbered), Relevance,
                        model=critic_model, max_tokens=200, thinking_off=False)
            if len(rel.verdicts) == len(run["chunk_texts"]):
                row["context_precision"] = sum(rel.verdicts) / len(rel.verdicts)
        scored.append(row)
        logger.info("  scored %s/%s", run["id"], run["system"])
    return scored


def summarize(scored_df: pd.DataFrame, n_questions: int) -> dict:
    """Aggregate score rows into the notebook 14 report structure."""
    summary = (scored_df.groupby("system")
               .agg(correct=("correct", "mean"), faithfulness=("faithfulness", "mean"),
                    context_precision=("context_precision", "mean"),
                    citation_validity=("citation_ok", "mean"), avg_citations=("n_citations", "mean"))
               .round(3))
    by_type = (scored_df.pivot_table(index="type", columns="system", values="correct",
                                     aggfunc="mean").round(2))
    return {"overall": summary.to_dict(), "by_type": by_type.to_dict(),
            "n_questions": n_questions, "scored_runs": len(scored_df)}


def run_benchmark(settings, driver, embedder, systems=("hybrid", "vector"),
                  limit: int | None = None, llm=None, judge=None,
                  artifacts_dir: Path | str = Path("artifacts")) -> dict:
    """Run + score the packaged gold benchmark (notebook 14 end to end).

    ``llm`` is the injectable plain-text answering callable (see
    ``semigraph.retrieval.answerer.answer``); ``judge`` the injectable
    structured judge (defaults to ``semigraph.llm.llm_json``).

    Returns {"report", "scored_df", "runs", "results_path", "scores_path",
    "report_path"}.
    """
    benchmark = load_benchmark()
    if limit is not None:
        benchmark = benchmark[:limit]
    results_path = settings.processed_dir / "eval_runs.jsonl"
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    runs = run_systems(benchmark, driver, embedder, systems, results_path, llm=llm)
    # scope scoring to this invocation's questions/systems (the log may hold
    # more when resuming a fuller earlier run)
    bench_ids = {b["id"] for b in benchmark}
    runs = [r for r in runs if r["id"] in bench_ids and r["system"] in systems]

    scored = score_runs(runs, benchmark, judge=judge,
                        judge_model=settings.llm_model, critic_model=settings.critic_model)
    scored_df = pd.DataFrame(scored)
    scores_path = artifacts_dir / "eval_scores.json"
    scored_df.to_json(scores_path, orient="records", indent=2)

    report = summarize(scored_df, n_questions=len(benchmark))
    report_path = artifacts_dir / "eval_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("report -> %s", report_path)
    return {"report": report, "scored_df": scored_df, "runs": runs,
            "results_path": results_path, "scores_path": scores_path,
            "report_path": report_path}
