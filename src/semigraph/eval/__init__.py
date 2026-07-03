"""Evaluation benchmark (ported from notebook 14, Milestone M6).

Public surface:

    from semigraph.eval import run_benchmark
"""

from .runner import (
    FAITH_PROMPT,
    JUDGE_PROMPT,
    NUM_PAT,
    REFUSAL_PAT,
    REL_PROMPT,
    Correct,
    Faithfulness,
    Relevance,
    parse_numbers,
    run_benchmark,
    run_systems,
    score_runs,
    summarize,
)

__all__ = [
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
