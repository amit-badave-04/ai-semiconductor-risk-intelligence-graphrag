"""Retrieval + grounded answering (ported from notebooks 10/11/14).

Public surface:

    from semigraph.retrieval import hybrid_retrieve, vector_retrieve, answer
"""

from .answerer import (ANSWER_PROMPT, CITE_RE, TextStream, answer, answer_stream,
                       build_blocks, format_metric_line, llm_text, usage_cost)
from .retriever import (
    DEFAULT_ANCHOR_CIK,
    detect_anchors,
    hybrid_retrieve,
    run_cypher,
    vector_retrieve,
)

__all__ = [
    "ANSWER_PROMPT",
    "CITE_RE",
    "TextStream",
    "DEFAULT_ANCHOR_CIK",
    "answer",
    "answer_stream",
    "build_blocks",
    "detect_anchors",
    "format_metric_line",
    "hybrid_retrieve",
    "llm_text",
    "usage_cost",
    "run_cypher",
    "vector_retrieve",
]
