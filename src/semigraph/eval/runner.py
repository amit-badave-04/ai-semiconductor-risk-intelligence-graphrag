"""Benchmark runner — ported from notebook 14 (Milestone M6).

Runs the packaged gold benchmark (``artifacts/benchmark.json``: the original 20 questions, 24 numeric gold questions from
XBRL, 4 misattribution probes and, once merged, the source-text temporal questions) over the systems under test
(entity-first hybrid vs vector-only baseline), then scores with programmatic checks + LLM judges (the notebook 14 M6
result was hybrid 100% correct / 0.865 faithful / 0 hallucinated citations on the original 20).

The correctness judge sees the valid citation ids, the data as-of date and the verified grading notes through ONE renderer,
``render_judge_prompt`` (docs/v2/M1B_PLAN.md section E); a misattribution probe is correct only when its deterministic guard
(``expect.not_company_disclosure``) AND the judge agree.

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
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from ..artifacts import load_benchmark, read_prompt
from ..llm import llm_json
from ..retrieval.answerer import TextStream, answer, usage_cost
from ..retrieval.ids import CITE_RE
from ..retrieval.verify import REFUSAL_RE, money_values
from .expect import NUM_PAT, check_expectation, parse_numbers  # noqa: F401  (re-exported: eval/__init__, bakeoff)

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
    # claims the grading notes contradict or do not support; defaulted so verdicts saved before this field still validate
    unsupported_claims: list[str] = []


class Recall(BaseModel):
    """Context recall: required facts present in the retrieved context."""
    needed: int
    present: int


# Verbatim notebook 14 judge prompts (packaged as template files).
FAITH_PROMPT = read_prompt("faith_judge")
REL_PROMPT = read_prompt("relevance_judge")
JUDGE_PROMPT = read_prompt("correctness_judge")
# Names the correctness_judge.txt in force. "cj-v1" was the prompt before 2026-09-27 (it penalised the hedged comparison wording the
# service is required to use); "cj-v2" added the hedged-wording section and rules 1-5 (acceptance run 2026-09-27: one leniency failure, a plain "carried over unchanged" claim about an item the notes list as new); "cj-v3" closes that direction in rule 2. Bump it with EVERY change to the prompt: it is
# recorded in the deployed-eval score and in the judge acceptance report (scripts/judge_acceptance.py), so a score is never read
# against a different instrument than the one that graded it.
JUDGE_PROMPT_VERSION = "cj-v3"
RECALL_PROMPT = read_prompt("recall_judge")

# Verbatim notebook 14 programmatic patterns (the refusal wording is shared with the serving-side verifier).
REFUSAL_PAT = REFUSAL_RE


def is_refusal_answer(text: str) -> bool:
    """A refusal the benchmark scores as correct: the (deliberately loose) refusal wording AND no money figure. A real refusal states
    no amount; an answer that gives one and then says "the filing does not provide ..." is an answer (closing review M3)."""
    return bool(REFUSAL_PAT.search(text)) and not money_values(text)

# --- the correctness judge: ONE renderer for every call site (score_runs, bakeoff.judge_open, eval-deployed) ---------
JUDGE_MAX_TOKENS = 600           # the verdict now carries up to five unsupported claims; 300 truncated
MAX_JUDGE_IDS = 40               # cited ids listed to the judge (the fixed prompt is sent up to 3 times per answer)
MAX_JUDGE_ANSWER_CHARS = 6000    # temporal answers are long; a cut answer would hide the very claims being checked
JUDGED_TYPES = frozenset({"misattribution"})   # a mechanical guard AND the judge (a probe passes only when both agree)


def needs_judge(item: Mapping) -> bool:
    """True when the item's correctness needs the LLM judge: every open question (no deterministic expectation, not a
    refusal) and every misattribution probe. A numeric or dependency question that merely carries ``judge_notes`` is
    scored deterministically and never judged."""
    if item["type"] in JUDGED_TYPES:
        return True
    return item["type"] != "refusal" and not item.get("expect")


def _citation_line(answer_text: str, valid_ids: Iterable[str]) -> str:
    valid = set(valid_ids)
    cited = sorted(set(CITE_RE.findall(answer_text)) & valid)
    listed = ", ".join(cited[:MAX_JUDGE_IDS]) if cited else "none"
    if len(cited) > MAX_JUDGE_IDS:
        listed += f" (+{len(cited) - MAX_JUDGE_IDS} more)"
    return f"{listed} ({len(valid)} ids were retrieved in total)"


def render_judge_prompt(item: Mapping, answer_text: str, *, valid_ids: Iterable[str] = (),
                        as_of: str | None = None) -> str:
    """The one correctness-judge prompt: the question, the verified grading notes, the data as-of date, the cited ids that
    were mechanically verified (out of the retrieved ones) and the answer. ``as_of`` None is stated as "not stated"."""
    return JUDGE_PROMPT.format(q=item["q"], as_of=as_of or "not stated", notes=item.get("judge_notes") or "(none)",
                               ids=_citation_line(answer_text, valid_ids), a=answer_text[:MAX_JUDGE_ANSWER_CHARS])


def data_as_of(settings) -> str | None:
    """The data as-of date shown to the judge: the newest filing or rule date in the local data lake (ISO), None if empty."""
    from ..snapshot import newest_lake_date

    newest = newest_lake_date(settings)
    return newest.isoformat() if newest else None


class SupersededLabelsError(ValueError):
    """The labels file was retired (its labellers could not see the filing), so it must not calibrate a judge."""


def load_judge_labels(path: Path | str = Path("artifacts/judge_labels.json"), *, include_superseded: bool = False) -> dict:
    """Load a judge-calibration label file, refusing one whose ``status`` is ``superseded`` unless
    ``include_superseded=True`` (docs/v2/REVIEW_2026-09-26.md: the 28 AI labels rated the wrong T1/T3 answers correct)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("status") == "superseded" and not include_superseded:
        raise SupersededLabelsError(
            f"{Path(path).name} is superseded and must not be used for calibration: "
            f"{doc.get('superseded_reason', 'no reason recorded')} (pass include_superseded=True to read it anyway)")
    return doc


class _UsageCapturingLLM:
    """Default answering callable for the benchmark: consumes a TextStream so
    provider-reported usage (tokens -> cost) is recorded per run, which the
    plain llm_text path does not expose."""

    def __init__(self):
        self.last_usage: dict | None = None

    def __call__(self, prompt: str) -> str:
        stream = TextStream(prompt, attempts=4, backoff=(15, 60, 180, 300))
        text = "".join(stream)
        self.last_usage = stream.usage
        return text


class AnswerBudgetExceeded(RuntimeError):
    """The answering spend in the run log reached ``max_usd``; nothing more is bought."""


def run_systems(benchmark: list[dict], driver, embedder, systems, results_path: Path,
                llm=None, max_usd: float | None = None) -> list[dict]:
    """Answer every (question, system) pair, checkpointed per pair in an
    append-only jsonl (notebook 14 section 3). Returns all logged runs.

    Each run row also carries ``latency_s``, ``usage`` and ``cost_usd`` (cost
    and latency per task, from the Agentic_Evals methodology); with an injected
    ``llm`` that exposes no ``last_usage`` they are recorded as None.

    ``max_usd`` caps the ANSWERING spend recorded in ``results_path`` (earlier
    sessions included, so a resume cannot re-spend it): before each paid answer
    the log's total is checked and ``AnswerBudgetExceeded`` raised once it has
    reached the cap. One answer can overshoot by its own cost.
    """
    capture = None
    if llm is None:
        llm = capture = _UsageCapturingLLM()
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done, spent = set(), 0.0
    if results_path.exists():
        for line in results_path.open(encoding="utf-8"):
            if line.strip():
                row = json.loads(line)
                done.add((row["id"], row["system"]))
                spent += row.get("cost_usd") or 0.0
    logger.info("%d runs checkpointed ($%.3f answering spend) — resuming", len(done), spent)

    with results_path.open("a", encoding="utf-8") as sink:
        for q in benchmark:
            for system in systems:
                if (q["id"], system) in done:
                    continue
                if max_usd is not None and spent >= max_usd:
                    raise AnswerBudgetExceeded(
                        f"answering spend ${spent:.3f} reached the ${max_usd:.2f} cap before {q['id']}/{system}; "
                        f"{len(done)} runs are checkpointed in {results_path.name}")
                t0 = time.monotonic()
                a = answer(q["q"], driver, embedder, strategy=system, llm=llm)
                usage = getattr(capture or llm, "last_usage", None)
                cost = usage_cost(usage)
                spent += cost or 0.0
                done.add((q["id"], system))
                sink.write(json.dumps({"id": q["id"], "system": system, "type": q["type"], "q": q["q"],
                                       "answer": a["answer"], "cited": sorted(a["cited"]),
                                       "valid_ids": sorted(a["valid_ids"]),
                                       "hallucinated": sorted(a["hallucinated"]),
                                       "context": a["context"],
                                       "chunk_texts": {c["chunk_id"]: c["text"] for c in a["retrieval"]["chunks"]},
                                       "latency_s": round(time.monotonic() - t0, 3),
                                       "usage": usage, "cost_usd": cost}) + "\n")
                sink.flush()
                logger.info("  %s/%s done", q["id"], system)
    return [json.loads(l) for l in results_path.open(encoding="utf-8") if l.strip()]


def score_runs(runs: list[dict], benchmark: list[dict], *, judge=None, judge_model: str | None = None,
               critic_model: str | None = None, as_of: str | None = None) -> list[dict]:
    """Score runs: programmatic checks + LLM judges (notebook 14 section 4).

    ``judge`` is an injectable ``llm_json``-compatible callable
    ``(prompt, model_cls, *, model=..., max_tokens=..., thinking_off=...)``; ``as_of`` is the data date shown to the
    correctness judge (see ``data_as_of``).
    """
    judge = judge or llm_json
    bench_by_id = {b["id"]: b for b in benchmark}
    scored = []

    def safe_judge(label: str, *args, **kwargs):
        """A judge that fails after its retries costs ONE metric, not the run."""
        try:
            return judge(*args, **kwargs)
        except RuntimeError as e:
            logger.error("%s judge failed for %s/%s — recorded as null: %s", label,
                         run["id"], run["system"], e)
            return None

    def judged_correct(b: dict, run: dict, ans: str) -> bool | None:
        prompt = render_judge_prompt(b, ans, valid_ids=run.get("valid_ids") or (), as_of=as_of)
        v = safe_judge("correctness", prompt, Correct, model=judge_model, max_tokens=JUDGE_MAX_TOKENS)
        return v.correct if v is not None else None

    for run in runs:
        b = bench_by_id[run["id"]]
        row = {"id": run["id"], "system": run["system"], "type": run["type"]}
        row["citation_ok"] = len(run["hallucinated"]) == 0
        row["n_citations"] = len(run["cited"])
        row["latency_s"] = run.get("latency_s")
        row["cost_usd"] = run.get("cost_usd")
        ans = run["answer"]
        # correctness
        if run["type"] == "refusal":
            row["correct"] = is_refusal_answer(ans)
        elif b.get("expect"):
            row["correct"] = check_expectation(b["expect"], ans)
            if row["correct"] and needs_judge(b):       # a probe: the guard passed, the judge decides the rest
                row["correct"] = judged_correct(b, run, ans)
        else:
            row["correct"] = judged_correct(b, run, ans)
        # faithfulness (skip refusals — nothing to fact-check)
        if run["type"] != "refusal":
            # judge against the FULL context the answering model saw (graph blocks + excerpts) —
            # judging hybrid answers against excerpts alone falsely marks graph-derived claims unsupported
            ctx = run.get("context") or "\n".join(f"[{cid}] {t[:600]}" for cid, t in run["chunk_texts"].items())
            ctx = ctx[:24000]
            f = safe_judge("faithfulness", FAITH_PROMPT.format(q=b["q"], a=ans[:4000], ctx=ctx or "(no context retrieved)"),
                           Faithfulness, model=judge_model, max_tokens=200)
            if f is not None:
                row["faithfulness"] = (f.supported_claims / f.total_claims) if f.total_claims else 1.0
            else:
                row["faithfulness"] = None
        # context recall (Haiku): are the facts the grading notes require in the context at all?
        notes = b.get("judge_notes") or (json.dumps(b["expect"]) if b.get("expect") else "")
        if run["type"] != "refusal" and notes:
            rctx = (run.get("context") or "")[:24000]
            rc = safe_judge("recall", RECALL_PROMPT.format(q=b["q"], notes=notes, ctx=rctx or "(no context retrieved)"),
                            Recall, model=critic_model, max_tokens=100, thinking_off=False)
            if rc is not None:
                row["context_recall"] = (min(rc.present, rc.needed) / rc.needed) if rc.needed else 1.0
        # context precision (Haiku, one call per run)
        if run["chunk_texts"]:
            numbered = "\n".join(f"{i+1}. {t[:350]}" for i, t in enumerate(run["chunk_texts"].values()))
            rel = safe_judge("relevance", REL_PROMPT.format(q=b["q"], chunks=numbered), Relevance,
                             model=critic_model, max_tokens=200, thinking_off=False)
            if rel is not None and len(rel.verdicts) == len(run["chunk_texts"]):
                row["context_precision"] = sum(rel.verdicts) / len(rel.verdicts)
        scored.append(row)
        logger.info("  scored %s/%s", run["id"], run["system"])
    return scored


SUMMARY_COLUMNS = {"correct": "correct", "faithfulness": "faithfulness",
                   "context_precision": "context_precision", "context_recall": "context_recall",
                   "citation_validity": "citation_ok", "avg_citations": "n_citations",
                   "avg_cost_usd": "cost_usd", "avg_latency_s": "latency_s"}


def summarize(scored_df: pd.DataFrame, n_questions: int, judge_model: str | None = None) -> dict:
    """Aggregate score rows into the notebook 14 report structure (+ recall,
    cost and latency per system when the rows carry them)."""
    aggs = {name: (col, "mean") for name, col in SUMMARY_COLUMNS.items() if col in scored_df.columns}
    summary = scored_df.groupby("system").agg(**aggs).round(4)
    by_type = (scored_df.pivot_table(index="type", columns="system", values="correct",
                                     aggfunc="mean").round(2))
    return {"overall": _nan_to_none(summary.to_dict()), "by_type": _nan_to_none(by_type.to_dict()),
            "n_questions": n_questions, "scored_runs": len(scored_df),
            "judge_model": judge_model}


def _nan_to_none(obj):
    """Runs that predate a metric aggregate to NaN, which is not JSON — emit null."""
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, float) and obj != obj:
        return None
    return obj


def run_benchmark(settings, driver, embedder, systems=("hybrid", "vector"),
                  limit: int | None = None, llm=None, judge=None,
                  artifacts_dir: Path | str = Path("artifacts"),
                  judge_model: str | None = None, rescore: bool = False,
                  report_suffix: str = "", runs_file: str = "eval_runs.jsonl",
                  max_answer_usd: float | None = None) -> dict:
    """Run + score the packaged gold benchmark (notebook 14 end to end).

    ``llm`` is the injectable plain-text answering callable (see
    ``semigraph.retrieval.answerer.answer``); ``judge`` the injectable
    structured judge (defaults to ``semigraph.llm.llm_json``).

    ``judge_model`` overrides the faithfulness/correctness judge (e.g. a
    different model family to cross-check judge self-preference);
    ``rescore=True`` skips answering and re-scores the checkpointed runs;
    ``report_suffix`` keeps such a re-scoring next to the primary report
    (``eval_report<suffix>.json``).

    ``runs_file`` names the checkpoint log inside ``settings.processed_dir``: a
    baseline for a NEW data snapshot must use its own file, else it would resume
    from (and answer nothing new against) the previous snapshot's runs.
    ``max_answer_usd`` caps the answering spend (see :func:`run_systems`).

    Returns {"report", "scored_df", "runs", "results_path", "scores_path",
    "report_path"}.
    """
    benchmark = load_benchmark()
    if limit is not None:
        benchmark = benchmark[:limit]
    results_path = settings.processed_dir / runs_file
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if rescore:
        runs = [json.loads(l) for l in results_path.open(encoding="utf-8") if l.strip()]
    else:
        runs = run_systems(benchmark, driver, embedder, systems, results_path, llm=llm,
                           max_usd=max_answer_usd)
    # scope scoring to this invocation's questions/systems (the log may hold
    # more when resuming a fuller earlier run)
    bench_ids = {b["id"] for b in benchmark}
    runs = [r for r in runs if r["id"] in bench_ids and r["system"] in systems]

    judge_model = judge_model or settings.llm_model
    scored = score_runs(runs, benchmark, judge=judge, judge_model=judge_model,
                        critic_model=settings.critic_model, as_of=data_as_of(settings))
    scored_df = pd.DataFrame(scored)
    scores_path = artifacts_dir / f"eval_scores{report_suffix}.json"
    scored_df.to_json(scores_path, orient="records", indent=2)

    report = summarize(scored_df, n_questions=len(benchmark), judge_model=judge_model)
    report_path = artifacts_dir / f"eval_report{report_suffix}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("report -> %s", report_path)
    return {"report": report, "scored_df": scored_df, "runs": runs,
            "results_path": results_path, "scores_path": scores_path,
            "report_path": report_path}
