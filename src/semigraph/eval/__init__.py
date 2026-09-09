"""Evaluation benchmark (ported from notebook 14, Milestone M6).

Public surface:

    from semigraph.eval import run_benchmark
"""

from .error_analysis import LABELS, analyze_failures, classify_failures, is_failure
from .runner import (
    FAITH_PROMPT,
    JUDGE_PROMPT,
    NUM_PAT,
    REFUSAL_PAT,
    REL_PROMPT,
    RECALL_PROMPT,
    SUMMARY_COLUMNS,
    Correct,
    Faithfulness,
    Recall,
    Relevance,
    parse_numbers,
    run_benchmark,
    run_systems,
    score_runs,
    summarize,
)

__all__ = [
    "LABELS",
    "RECALL_PROMPT",
    "Recall",
    "SUMMARY_COLUMNS",
    "analyze_failures",
    "classify_failures",
    "is_failure",
    "Correct",
    "FAITH_PROMPT",
    "Faithfulness",
    "JUDGE_PROMPT",
    "NUM_PAT",
    "REFUSAL_PAT",
    "REL_PROMPT",
    "Relevance",
    "parse_numbers",
    "run_benchmark",
    "run_systems",
    "score_runs",
    "summarize",
]
