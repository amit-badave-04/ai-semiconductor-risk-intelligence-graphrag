"""F4 per sentence: on the workspace path a figure is grounded only by the SEC context plus the sources cited in ITS
OWN sentence. A mixed answer ("$9.9 billion [SEC]. The plan is ambitious [DOC].") must not ground an upload-only amount.
Drives the sync and the async (production) workspace streams with faked retrieval and writer."""

from __future__ import annotations

import asyncio

import pytest
from test_redteam_RT004 import DOC_CHUNK, QUESTION, SEC_CHUNK, WORKSPACE_ID, _done_checks, _FakeEmbedder, _fakes, _sec_retrieval  # noqa: F401

from semigraph.retrieval import workspace as ws
from semigraph.retrieval import workspace_async as wsa
from semigraph.serve.limiters import make_limiters

MIXED = f"Nvidia reported revenue of $9.9 billion [{SEC_CHUNK}]. The plan is ambitious [{DOC_CHUNK}]."


def test_f4_mixed_answer_upload_only_amount_behind_sec_citation_is_unmatched():
    checks = _done_checks(MIXED)
    assert "$9.9 billion" in checks["unmatched_numbers"] and checks["numbers_grounded"] is False, checks


def test_f4_same_figure_in_a_doc_cited_sentence_stays_grounded():
    checks = _done_checks(f"Nvidia reported revenue of $9.9 billion [{DOC_CHUNK}]. The plan is ambitious [{SEC_CHUNK}].")
    assert checks["numbers_grounded"] is True and checks["unmatched_numbers"] == [], checks


def test_f4_public_path_grounds_amounts_on_the_context_only_as_before():
    """A plain sources dict (the public path) keeps the pre-fix behaviour exactly: amounts are grounded by the context
    alone, so a cited source holding an amount the context does not contain grounds nothing (verifier, 2026-11-22)."""
    from semigraph.retrieval.verify import answer_checks
    ctx = f"[{SEC_CHUNK}]\nplain"
    public = answer_checks(MIXED, {SEC_CHUNK, DOC_CHUNK}, {SEC_CHUNK, DOC_CHUNK}, ctx,
                           sources={DOC_CHUNK: "revenue of $9.9 billion"}, question="")
    assert public.numbers_grounded is False and "$9.9 billion" in public.unmatched_numbers


class _Settings:
    embed_slots, db_thread_limit = 2, 4


class _AsyncStream:
    model, usage, finish_reason = "anthropic/claude-sonnet-5", {"prompt_tokens": 5, "completion_tokens": 5}, "stop"

    def __init__(self, text):
        self.text = text

    async def __aiter__(self):
        yield self.text


def test_f4_async_workspace_path_upload_only_amount_behind_sec_citation_is_unmatched(monkeypatch):
    monkeypatch.setattr(wsa, "hybrid_retrieve", lambda *a, **kw: _sec_retrieval())
    monkeypatch.setattr(wsa, "workspace_retrieve", lambda *a, **kw: {"doc_chunks": [
        {"chunk_id": DOC_CHUNK, "text": "Internal plan: revenue of $9.9 billion.", "document_id": "0123456789ab",
         "version": 1, "is_current": True, "title": "plan.pdf"}], "stale_ids": []})
    monkeypatch.setattr(wsa, "_stale_citations", lambda *a, **kw: [])

    async def main():
        events = [e async for e in wsa.astream_workspace_answer(
            QUESTION, object(), _FakeEmbedder(), limiters=make_limiters(_Settings()), workspace_id=WORKSPACE_ID,
            llm_stream=lambda prompt: _AsyncStream(MIXED))]
        return events[-1]

    done = asyncio.run(main())
    assert done["event"] == "done", done
    assert "$9.9 billion" in done["checks"]["unmatched_numbers"], done["checks"]
