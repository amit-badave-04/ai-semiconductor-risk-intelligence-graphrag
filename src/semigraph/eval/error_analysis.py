"""Error analysis — turn benchmark scores into a failure taxonomy (the
"evaluate -> error analysis -> fix -> re-evaluate" loop from the Agentic_Evals
methodology), mechanical-first like the rest of this evaluation.

A run FAILS when it is incorrect, unfaithful (< FAITH_FLOOR), or cites
outside the retrieved context. Failures get a label from LABELS:

1. mechanically, when the scores already say why (missed/over-triggered
   refusal, numeric miss, invalid citation with correct content, low context
   recall = retrieval miss) — no LLM call, no judge bias;
2. otherwise by one Haiku call with the packaged ``failure_taxonomy`` prompt.

Output: rows (one per failing run) and counts per system per label, written
to ``<artifacts_dir>/error_analysis.json`` by :func:`analyze_failures`.
"""

import json
import logging
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from ..artifacts import read_prompt
from ..llm import llm_json
from .runner import REFUSAL_PAT

logger = logging.getLogger("semigraph.eval.error_analysis")

LABELS = ("RETRIEVAL_MISS", "UNGROUNDED_CLAIM", "WRONG_ENTITY", "STALE_RISK_LEAK",
          "NUMERIC_MISMATCH", "REFUSAL_OVERTRIGGER", "REFUSAL_MISSED",
          "FORMAT_OR_CITATION", "OTHER")
FAITH_FLOOR = 0.8
RECALL_FLOOR = 0.5
TAXONOMY_PROMPT = read_prompt("failure_taxonomy")


class FailureLabel(BaseModel):
    label: str
    evidence: str


def is_failure(score: dict) -> bool:
    return (not score.get("correct", True)
            or score.get("faithfulness", 1.0) < FAITH_FLOOR
            or not score.get("citation_ok", True))


def mechanical_label(score: dict, run: dict, bench: dict) -> str | None:
    """Label from the scores alone when they are unambiguous; None otherwise."""
    answer = run.get("answer", "")
    refused = bool(REFUSAL_PAT.search(answer))
    if bench["type"] == "refusal" and not score.get("correct", True):
        return "REFUSAL_MISSED"
    if bench["type"] != "refusal" and refused and not score.get("correct", True):
        return "REFUSAL_OVERTRIGGER"
    if score.get("correct", True) and not score.get("citation_ok", True):
        return "FORMAT_OR_CITATION"
    if score.get("context_recall") is not None and score["context_recall"] < RECALL_FLOOR:
        return "RETRIEVAL_MISS"
    if bench["type"] == "numeric" and not score.get("correct", True):
        return "NUMERIC_MISMATCH"
    return None


def classify_failures(scored: list[dict], runs: list[dict], benchmark: list[dict], *,
                      judge=None, model: str | None = None) -> list[dict]:
    """One labelled row per failing (question, system)."""
    judge = judge or llm_json
    bench_by_id = {b["id"]: b for b in benchmark}
    run_by_key = {(r["id"], r["system"]): r for r in runs}
    rows = []
    for score in scored:
        if not is_failure(score):
            continue
        bench = bench_by_id[score["id"]]
        run = run_by_key.get((score["id"], score["system"]), {})
        label = mechanical_label(score, run, bench)
        evidence, mechanical = "", True
        if label is None:
            mechanical = False
            notes = bench.get("judge_notes") or json.dumps(bench.get("expect", {}))
            scores_txt = json.dumps({k: score.get(k) for k in
                                     ("correct", "faithfulness", "citation_ok",
                                      "context_precision", "context_recall")}, default=str)
            verdict = judge(TAXONOMY_PROMPT.format(q=bench["q"], notes=notes, scores=scores_txt,
                                                   a=run.get("answer", "")[:3000],
                                                   ctx=(run.get("context") or "")[:6000]),
                            FailureLabel, model=model, max_tokens=200, thinking_off=False)
            label = verdict.label if verdict.label in LABELS else "OTHER"
            evidence = verdict.evidence
        rows.append({"id": score["id"], "system": score["system"], "type": bench["type"],
                     "label": label, "mechanical": mechanical, "evidence": evidence,
                     "correct": score.get("correct"), "faithfulness": score.get("faithfulness"),
                     "citation_ok": score.get("citation_ok"),
                     "context_recall": score.get("context_recall")})
    return rows


def summarize_failures(rows: list[dict]) -> dict:
    counts: dict[str, Counter] = {}
    for r in rows:
        counts.setdefault(r["system"], Counter())[r["label"]] += 1
    return {"n_failures": len(rows),
            "counts": {s: dict(c.most_common()) for s, c in counts.items()},
            "mechanical_share": (sum(r["mechanical"] for r in rows) / len(rows)) if rows else None}


def analyze_failures(scored: list[dict], runs: list[dict], benchmark: list[dict], *,
                     artifacts_dir: Path | str = Path("artifacts"), judge=None,
                     model: str | None = None) -> dict:
    rows = classify_failures(scored, runs, benchmark, judge=judge, model=model)
    report = {**summarize_failures(rows), "labels": list(LABELS), "rows": rows}
    out = Path(artifacts_dir) / "error_analysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("error analysis -> %s (%d failures)", out, len(rows))
    return report
