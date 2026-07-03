"""Retrieval + grounded answering (ported from notebooks 10/11/14).

Public surface:

    from semigraph.retrieval import hybrid_retrieve, vector_retrieve, answer
"""

from .answerer import ANSWER_PROMPT, CITE_RE, answer, build_blocks, llm_text
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
    "DEFAULT_ANCHOR_CIK",
    "answer",
    "build_blocks",
    "detect_anchors",
    "hybrid_retrieve",
    "llm_text",
    "run_cypher",
    "vector_retrieve",
]
