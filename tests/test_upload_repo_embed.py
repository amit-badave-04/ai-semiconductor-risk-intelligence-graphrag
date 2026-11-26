"""I4a / R15, R16: the repo functions behind the keyword-ready write and the background embedding (fake driver, no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_serve_upload_repo import FakeDriver, _chunk, _unit

from semigraph.uploads import repo


def _put(driver, chunks):
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h", method="text", pages=1,
                     chars=5, chars_per_page=5.0, text="hello", units=[_unit()], chunks=chunks,
                     change_report={"items_compared": False, "not_compared_reason": "first_version"}, suspicious=False,
                     now=datetime.now(UTC))


def _chunk_call(driver):
    return [p for q, p in driver.transactions[0].calls if q == repo.CREATE_CHUNK_QUERY]


def test_a_null_vector_chunk_is_written_pending_with_its_priority_and_no_quota_bump():
    driver = FakeDriver()
    pending = {**_chunk(embedded=False), "embedding": None, "priority": 2}
    _put(driver, [pending])
    (params,) = _chunk_call(driver)
    assert params["embedding"] is None and params["embed_state"] == "pending" and params["priority"] == 2
    assert repo.BUMP_EMBEDDED_TOKENS_QUERY not in [q for q, _ in driver.transactions[0].calls]


def test_a_chunk_with_a_reused_vector_is_written_done_and_a_chunk_without_priority_is_non_priority():
    driver = FakeDriver()
    reused = {**_chunk(embedded=False), "embedding": [0.1, 0.2]}
    _put(driver, [reused])
    (params,) = _chunk_call(driver)
    assert params["embed_state"] == "done" and params["priority"] == repo.NON_PRIORITY_RANK


def test_the_reuse_query_ignores_null_vectors():
    assert "c.embedding IS NOT NULL" in repo.EMBEDDED_CHUNKS_QUERY


def test_set_chunk_embedding_locks_the_workspace_writes_the_vector_and_bumps_the_quota_in_one_transaction():
    driver = FakeDriver(tx_responses={repo.SET_CHUNK_EMBEDDING_QUERY: [{"chunk_id": "c1"}]})
    assert repo.set_chunk_embedding(driver, "ws1", "c1", [0.1, 0.2], 40) is True
    (tx,) = driver.transactions
    queries = [q for q, _ in tx.calls]
    assert queries == [repo.LOCK_WORKSPACE_QUERY, repo.SET_CHUNK_EMBEDDING_QUERY, repo.BUMP_EMBEDDED_TOKENS_QUERY]
    assert dict(tx.calls[2][1])["delta"] == 40


def test_set_chunk_embedding_on_a_chunk_that_is_no_longer_pending_changes_nothing():
    driver = FakeDriver(tx_responses={repo.SET_CHUNK_EMBEDDING_QUERY: []})
    assert repo.set_chunk_embedding(driver, "ws1", "c1", [0.1], 40) is False
    assert repo.BUMP_EMBEDDED_TOKENS_QUERY not in [q for q, _ in driver.transactions[0].calls]


def test_set_chunk_embedding_raises_workspace_gone_and_writes_nothing():
    driver = FakeDriver(tx_responses={repo.LOCK_WORKSPACE_QUERY: []})
    with pytest.raises(repo.WorkspaceGone):
        repo.set_chunk_embedding(driver, "ws1", "c1", [0.1], 40)
    assert [q for q, _ in driver.transactions[0].calls] == [repo.LOCK_WORKSPACE_QUERY]


def test_pending_chunks_are_read_in_priority_then_document_order(monkeypatch):
    seen = {}

    def fake(driver, query, **params):
        seen["query"], seen["params"] = query, params
        return [{"chunk_id": "c1"}]

    monkeypatch.setattr(repo, "run_cypher", fake)
    assert repo.pending_chunks(object(), "ws1", "doc", 3) == [{"chunk_id": "c1"}]
    assert seen["query"] == repo.PENDING_CHUNKS_QUERY and seen["params"] == {"ws": "ws1", "document_id": "doc", "version": 3}
    assert "ORDER BY c.priority, c.seq" in repo.PENDING_CHUNKS_QUERY and "c.embed_state = 'pending'" in repo.PENDING_CHUNKS_QUERY
    assert "c.is_current = true" in repo.PENDING_CHUNKS_QUERY


def test_finish_embedding_marks_what_is_left_keyword_only(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [{"skipped": 7}] if q == repo.SKIP_PENDING_CHUNKS_QUERY else [])
    assert repo.finish_embedding(object(), "ws1", "doc", 1) == 7
    assert "SET c.embed_state = 'skipped'" in repo.SKIP_PENDING_CHUNKS_QUERY


def test_resumable_embeds_lists_pending_current_chunks_per_version(monkeypatch):
    rows = [{"workspace_id": "w", "document_id": "d", "version": 2, "job_id": "j"}]
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows if q == repo.RESUMABLE_EMBEDS_SELECT_QUERY else [])
    assert repo.resumable_embeds(object()) == rows
    assert "$ws" not in repo.RESUMABLE_EMBEDS_SELECT_QUERY       # cross-workspace by necessity: a restart requeues every workspace
    assert "embed_state = 'pending'" in repo.RESUMABLE_EMBEDS_SELECT_QUERY
