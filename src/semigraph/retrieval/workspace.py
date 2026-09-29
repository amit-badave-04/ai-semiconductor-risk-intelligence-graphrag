"""Answering over an upload workspace (M4, docs/v2/M4_PLAN.md 4.2 and 4.3).

STEP 0 STUB: the entry point's signature is final (``serve.routes._paid_stream`` calls it exactly like ``answer_stream``);
Worker C implements it:

    stream_workspace_answer(question, driver, embedder, *, strategy="hybrid", workspace_id, as_of=None, timeout=None,
                            max_tokens=None, escalation_model=None, **stream_kwargs) -> Iterator[dict]

= ``hybrid_retrieve`` (unchanged SEC retrieval) + workspace retrieval (``uploads.repo.search_chunks``, filtered by workspace and
``as_of``) + the separate ``answer_workspace.txt`` template (uploaded text inside a per-request random delimiter, as data) +
``answerer.stream_answer_for_prompt(..., postprocess=strip_links_images, force_buffered=True)``. Events: ``retrieval`` (with
``doc_chunks``), ``delta``, ``escalated``, ``done`` (with a ``workspace`` block: ``id_hash``, ``doc_chunks``,
``stale_citations``, ``suspicious``) or ``error``. Never the agent, never the answer cache.
"""

from __future__ import annotations


def stream_workspace_answer(question: str, driver, embedder, *, strategy: str = "hybrid", workspace_id: str,
                            as_of: str | None = None, timeout: float | None = None, max_tokens: int | None = None,
                            escalation_model: str | None = None, **stream_kwargs):
    raise NotImplementedError("retrieval.workspace: M4 Worker C")
