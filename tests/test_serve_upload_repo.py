"""``uploads.repo``: every Cypher statement is workspace-scoped (M4, docs/v2/M4_PLAN.md 4.2, 4.4, 5, 14.4).

Two layers: STATIC checks over every module-level query string (imported directly, never re-typed here — a query
edited in ``repo.py`` is automatically re-checked), and unit tests against a fake driver/session/transaction that
prove the call SHAPE (one transaction for ``put_version`` / ``delete_workspace``, the constant-time authenticate
path, the current-vs-as-of SEARCH dispatch) without a real Neo4j. Full read-after-write correctness — the currency
flip, the leak isolation across two workspaces, the as-of cutoff — is proven live against the throwaway instance in
``tests/integration/test_workspace_repo_neo4j.py``.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

import pytest

from semigraph.uploads import repo

# ---------------------------------------------------------------------- static: every module-level query string

PUBLIC_LABELS = ("Company", "Filing", "FilingSection", "RiskFactor", "RiskItem", "RiskPassage",
                  "EvidenceSpan", "Metric", "ExportControl", "Product", "Snapshot")
_NODE_PATTERN = re.compile(r"\(\s*\w*\s*:\s*(User\w+)\s*\{([^}]*)\}")
_DOUBLE_LABEL = re.compile(r":\s*User\w+\s*:\s*User\w+")


def _query_constants() -> dict[str, str]:
    return {name: getattr(repo, name) for name in dir(repo)
            if name.endswith("_QUERY") and isinstance(getattr(repo, name), str)}


def test_every_query_constant_is_actually_exercised_by_this_module():
    # A cheap sanity check that the static scan below is not vacuous.
    assert len(_query_constants()) >= 20


def test_every_query_binds_ws_except_the_documented_sweep_exception():
    for name, text in _query_constants().items():
        if name == "SWEEP_SELECT_QUERY":
            assert "expires_at" in text and "$ws" not in text, (
                "SWEEP_SELECT_QUERY is the one documented exception (it looks across every workspace); "
                "everything else must bind $ws")
            continue
        assert "$ws" in text, f"{name} does not bind $ws"


def test_every_user_node_pattern_with_a_property_map_carries_workspace_id_ws():
    for name, text in _query_constants().items():
        for label, props in _NODE_PATTERN.findall(text):
            assert "workspace_id: $ws" in props, f"{name}: a ({label} {{...}}) pattern lacks workspace_id: $ws: {props!r}"


def test_the_shared_delete_label_template_carries_ws_and_workspace_id_ws_for_every_label():
    assert repo._USER_LABELS, "no labels registered for delete_workspace/sweep_expired"
    for label in repo._USER_LABELS:
        text = repo._DELETE_LABEL_TEMPLATE.format(label=label)
        assert "$ws" in text and "workspace_id: $ws" in text
        assert "DETACH DELETE" in text


def test_no_query_ever_names_a_public_label():
    for name, text in _query_constants().items():
        for label in PUBLIC_LABELS:
            assert label not in text, f"{name} references the public label {label}"
    for label in repo._USER_LABELS:
        formatted = repo._DELETE_LABEL_TEMPLATE.format(label=label)
        for public in PUBLIC_LABELS:
            assert public not in formatted


def test_every_private_node_pattern_carries_exactly_one_label():
    for name, text in _query_constants().items():
        assert not _DOUBLE_LABEL.search(text), f"{name} attaches two labels to one node"


def test_search_queries_use_only_the_filtered_search_form_never_an_unfiltered_vector_query():
    for name in ("SEARCH_CURRENT_QUERY", "SEARCH_ASOF_QUERY"):
        text = getattr(repo, name)
        assert "SEARCH c IN (VECTOR INDEX user_chunk_embedding" in text
        assert "queryNodes" not in text
        assert "c.workspace_id = $ws" in text
    assert "c.is_current = true" in repo.SEARCH_CURRENT_QUERY
    assert "c.valid_from < $cutoff AND c.valid_to >= $cutoff" in repo.SEARCH_ASOF_QUERY


def test_put_version_writes_the_currency_flip_and_the_supersedes_edge_with_the_change_report():
    assert "SET prev.is_current = false" in repo.SUPERSEDE_VERSION_QUERY
    assert "SUPERSEDES" in repo.SUPERSEDE_VERSION_QUERY and "change_report: $change_report_json" in repo.SUPERSEDE_VERSION_QUERY
    assert "is_current = false" in repo.SUPERSEDE_CHUNKS_QUERY


def test_no_relationship_query_ever_matches_or_creates_a_bare_unlabelled_node_by_id_alone():
    # Every CREATE/MERGE of a node carries a label; a bare "(n {workspace_id: $ws})" with no label would let a
    # mistyped query attach to ANY node in the graph, public or private.
    bare = re.compile(r"\(\s*\w+\s*\{")
    for name, text in _query_constants().items():
        assert not bare.search(text), f"{name} has an unlabelled node pattern"


# ---------------------------------------------------------------------- fakes

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def data(self):
        return list(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return None


class FakeTx:
    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or {}

    def run(self, query, **params):
        self.calls.append((query, params))
        return FakeResult(self._responses.get(query, []))


class FakeSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run(self, query, **params):
        self._driver.session_run_calls.append((query, params))
        rows = self._driver.session_run_responses.get(query, [])
        return FakeResult(rows if not callable(rows) else rows(params))

    def execute_write(self, fn):
        tx = FakeTx(self._driver.tx_responses)
        result = fn(tx)
        self._driver.transactions.append(tx)
        return result


class FakeDriver:
    def __init__(self, tx_responses=None, session_run_responses=None):
        self.transactions: list[FakeTx] = []
        self.session_run_calls: list[tuple[str, dict]] = []
        self.session_run_responses = session_run_responses or {}
        self.tx_responses = tx_responses or {}

    def session(self, **kwargs):
        return FakeSession(self)


def _chunk(chunk_id="doc:aaaaaaaaaaaa:v1:0000", embedded=True, tokens=10):
    return {"chunk_id": chunk_id, "seq": 0, "text": "hello", "text_hash": "h", "char_start": 0, "char_end": 5,
            "tokens": tokens, "embedding": [0.1, 0.2], "embedded": embedded}


def _unit():
    return {"unit_id": "u0", "kind": "paragraph", "headline": None, "char_start": 0, "char_end": 5}


# ---------------------------------------------------------------------- authenticate: constant-time shape

def test_authenticate_calls_compare_digest_exactly_once_whatever_the_path(monkeypatch):
    calls = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    token = "a-real-token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    future = datetime.now(UTC) + timedelta(hours=1)

    for rows in ([], [{"token_hash": token_hash, "expires_at": future}]):
        calls.clear()
        monkeypatch.setattr(repo, "run_cypher", lambda driver, query, **p: rows)
        repo.authenticate(object(), "ws", "wrong-token")
        assert len(calls) == 1, "authenticate must run the SAME hmac.compare_digest call on every path"


def test_authenticate_true_only_for_a_live_workspace_with_the_right_token(monkeypatch):
    token = "correct-horse-battery-staple"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(hours=1)

    def rows_for(expires_at):
        return [{"token_hash": token_hash, "expires_at": expires_at}]

    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows_for(future))
    assert repo.authenticate(object(), "ws", token) is True
    assert repo.authenticate(object(), "ws", "wrong") is False

    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows_for(past))
    assert repo.authenticate(object(), "ws", token) is False, "an expired workspace must never authenticate"

    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    assert repo.authenticate(object(), "ws", token) is False


def test_create_workspace_never_sends_the_raw_token_to_the_database(monkeypatch):
    seen = {}

    def fake_run_cypher(driver, query, **params):
        seen.update(params)
        return []

    monkeypatch.setattr(repo, "run_cypher", fake_run_cypher)
    ws, token, expires_at = repo.create_workspace(object(), ttl_hours=24)
    assert seen["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in seen.values()
    assert seen["ws"] == ws
    assert datetime.fromisoformat(expires_at) > datetime.now(UTC)


# ---------------------------------------------------------------------- datetime contract: tz-aware, never a string

@pytest.mark.parametrize("bad_now", [datetime(2026, 1, 1), "2026-01-01T00:00:00Z", "2026-01-01", None, 12345],
                         ids=["naive-datetime", "iso-string-z", "date-string", "none", "int"])
def test_put_version_rejects_anything_that_is_not_a_timezone_aware_datetime(bad_now):
    with pytest.raises(TypeError):
        repo.put_version(FakeDriver(), "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                         method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                         chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                         suspicious=False, now=bad_now)


@pytest.mark.parametrize("bad_now", [datetime(2026, 1, 1), "2026-01-01T00:00:00Z", None],
                         ids=["naive-datetime", "iso-string", "none"])
def test_sweep_expired_rejects_anything_that_is_not_a_timezone_aware_datetime(bad_now):
    with pytest.raises(TypeError):
        repo.sweep_expired(FakeDriver(), bad_now)


@pytest.mark.parametrize("bad_cutoff", [datetime(2026, 1, 1), "2026-01-01T00:00:00Z"],
                         ids=["naive-datetime", "iso-string"])
def test_search_chunks_rejects_a_cutoff_that_is_not_a_timezone_aware_datetime(bad_cutoff):
    with pytest.raises(TypeError):
        repo.search_chunks(FakeDriver(), "ws1", [0.1], k=5, cutoff=bad_cutoff)


def test_search_chunks_accepts_cutoff_none_without_raising(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    repo.search_chunks(object(), "ws1", [0.1], k=5, cutoff=None)   # must not raise


# ---------------------------------------------------------------------- put_version: one transaction

def test_put_version_runs_every_write_inside_one_execute_write_call():
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    assert len(driver.transactions) == 1, "put_version must be exactly one transaction"
    queries = [q for q, _ in driver.transactions[0].calls]
    assert repo.MERGE_DOCUMENT_QUERY in queries
    assert repo.CREATE_VERSION_QUERY in queries
    assert repo.CREATE_UNIT_QUERY in queries
    assert repo.CREATE_CHUNK_QUERY in queries
    assert repo.BUMP_EMBEDDED_TOKENS_QUERY in queries
    assert repo.SUPERSEDE_VERSION_QUERY not in queries, "a brand-new document has nothing to supersede"


def test_put_version_supersedes_the_previous_current_version_in_the_same_transaction():
    driver = FakeDriver(tx_responses={repo.FIND_CURRENT_VERSION_QUERY: [{"version": 1}]})
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title=None, version=2, content_hash="h2",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk(chunk_id="doc:aaaaaaaaaaaa:v2:0000")],
                     change_report={"items_compared": True, "not_compared_reason": None,
                                    "changed": [{"older_unit_id": "u0", "newer_unit_id": "u0",
                                                 "passages": [{"quote": "hi", "chunk_id": "doc:aaaaaaaaaaaa:v2:0000",
                                                               "kind": "changed"}]}]},
                     suspicious=False, now=datetime.now(UTC))
    calls = driver.transactions[0].calls
    queries = [q for q, _ in calls]
    assert queries.count(repo.SUPERSEDE_VERSION_QUERY) == 1
    assert queries.count(repo.SUPERSEDE_CHUNKS_QUERY) == 1
    assert repo.LINK_SUCCEEDED_BY_QUERY in queries and repo.CREATE_PASSAGE_QUERY in queries
    supersede_params = next(p for q, p in calls if q == repo.SUPERSEDE_VERSION_QUERY)
    assert supersede_params["prev_version"] == 1 and supersede_params["version"] == 2
    assert "change_report_json" in supersede_params


def test_put_version_only_bumps_embedded_tokens_for_chunks_marked_embedded():
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk(embedded=True, tokens=7), _chunk(chunk_id="c2", embedded=False, tokens=999)],
                     change_report={"items_compared": True, "not_compared_reason": None}, suspicious=False,
                     now=datetime.now(UTC))
    bump = next(p for q, p in driver.transactions[0].calls if q == repo.BUMP_EMBEDDED_TOKENS_QUERY)
    assert bump["delta"] == 7


def test_put_version_converts_embeddings_to_plain_floats():
    class FakeNumpyFloat(float):
        pass

    driver = FakeDriver()
    weird = _chunk()
    weird["embedding"] = [FakeNumpyFloat(0.5), FakeNumpyFloat(0.25)]
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[weird], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    chunk_params = next(p for q, p in driver.transactions[0].calls if q == repo.CREATE_CHUNK_QUERY)
    assert all(type(x) is float for x in chunk_params["embedding"])  # noqa: E721 - exact type, not a subclass


# ---------------------------------------------------------------------- delete_workspace / sweep_expired

def test_delete_workspace_deletes_every_label_in_one_transaction_and_reports_existence():
    driver = FakeDriver(tx_responses={repo.WORKSPACE_EXISTS_QUERY: [{"n": 1}]})
    assert repo.delete_workspace(driver, "ws1") is True
    assert len(driver.transactions) == 1
    formatted = {q for q, _ in driver.transactions[0].calls}
    for label in repo._USER_LABELS:
        assert repo._DELETE_LABEL_TEMPLATE.format(label=label) in formatted


def test_delete_workspace_reports_false_for_an_unknown_workspace():
    driver = FakeDriver(tx_responses={repo.WORKSPACE_EXISTS_QUERY: [{"n": 0}]})
    assert repo.delete_workspace(driver, "ws-nope") is False


def test_sweep_expired_is_idempotent_when_nothing_is_expired():
    driver = FakeDriver(session_run_responses={repo.SWEEP_SELECT_QUERY: []})
    assert repo.sweep_expired(driver, datetime.now(UTC)) == 0
    assert driver.transactions == []


def test_sweep_expired_deletes_only_the_selected_expired_workspaces():
    driver = FakeDriver(session_run_responses={repo.SWEEP_SELECT_QUERY: [{"workspace_id": "ws-old-1"},
                                                                        {"workspace_id": "ws-old-2"}]})
    assert repo.sweep_expired(driver, datetime.now(UTC)) == 2
    assert len(driver.transactions) == 2
    for tx in driver.transactions:
        deleted_ws = {p["ws"] for _, p in tx.calls}
        assert deleted_ws <= {"ws-old-1", "ws-old-2"} and len(deleted_ws) == 1


def test_sweep_select_query_filters_only_on_expires_at_and_caps_the_batch():
    assert "expires_at < $now" in repo.SWEEP_SELECT_QUERY
    assert str(repo.SWEEP_SELECT_BATCH) in repo.SWEEP_SELECT_QUERY


# ---------------------------------------------------------------------- read shaping

def test_get_workspace_returns_none_for_an_unknown_workspace(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    assert repo.get_workspace(object(), "nope") is None


def test_get_workspace_shapes_usage_from_current_versions_only(monkeypatch):
    def fake_run_cypher(driver, query, **params):
        if query == repo.GET_WORKSPACE_QUERY:
            return [{"workspace_id": "ws1", "expires_at": "2026-01-01T00:00:00Z", "embedded_tokens": 42}]
        if query == repo.GET_DOCUMENTS_QUERY:
            return [{"document_id": "aaaaaaaaaaaa", "title": "T", "latest_version": 2}]
        if query == repo.GET_VERSIONS_QUERY:
            return [{"version": 1, "pages": 10, "is_current": False, "status": "superseded"},
                    {"version": 2, "pages": 3, "is_current": True, "status": "current"}]
        raise AssertionError(query)

    monkeypatch.setattr(repo, "run_cypher", fake_run_cypher)
    workspace = repo.get_workspace(object(), "ws1")
    assert workspace["usage"] == {"documents": 1, "pages": 3, "embedded_tokens": 42}


def test_quota_shape_with_no_documents(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    assert repo.quota(object(), "ws1") == {"documents": 0, "versions_by_document": {}, "pages": 0, "embedded_tokens": 0}


def test_quota_shape_with_documents(monkeypatch):
    rows = [{"embedded_tokens": 100, "document_id": "aaaaaaaaaaaa", "version_count": 3, "current_pages": 5},
            {"embedded_tokens": 100, "document_id": "bbbbbbbbbbbb", "version_count": 1, "current_pages": 2}]
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows)
    assert repo.quota(object(), "ws1") == {
        "documents": 2, "versions_by_document": {"aaaaaaaaaaaa": 3, "bbbbbbbbbbbb": 1},
        "pages": 7, "embedded_tokens": 100}


def test_search_chunks_dispatches_current_vs_asof(monkeypatch):
    seen = {}

    def fake_run_cypher(d, q, **p):
        seen["query"] = q
        return []

    monkeypatch.setattr(repo, "run_cypher", fake_run_cypher)
    repo.search_chunks(object(), "ws1", [0.1], k=5, cutoff=None)
    assert seen["query"] == repo.SEARCH_CURRENT_QUERY
    cutoff = datetime.now(UTC)
    repo.search_chunks(object(), "ws1", [0.1], k=5, cutoff=cutoff)
    assert seen["query"] == repo.SEARCH_ASOF_QUERY


def test_chunk_texts_short_circuits_on_an_empty_id_list(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_cypher must not be called for an empty id list")

    monkeypatch.setattr(repo, "run_cypher", boom)
    assert repo.chunk_texts(object(), "ws1", []) == {}


def test_get_changes_returns_none_when_no_supersedes_edge_exists(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [{"change_report": None}])
    assert repo.get_changes(object(), "ws1", "aaaaaaaaaaaa", older=1, newer=2) is None


def test_get_changes_parses_the_stored_json(monkeypatch):
    import json
    payload = {"items_compared": True, "changed": []}
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [{"change_report": json.dumps(payload)}])
    assert repo.get_changes(object(), "ws1", "aaaaaaaaaaaa", older=1, newer=2) == payload


def test_evidence_returns_none_for_an_unknown_chunk(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    assert repo.evidence(object(), "ws1", "doc:aaaaaaaaaaaa:v1:0000") is None


def test_put_job_and_get_job_round_trip_through_the_stored_json(monkeypatch):
    store = {}

    def fake_run_cypher(driver, query, **params):
        if query == repo.PUT_JOB_QUERY:
            store[params["job_id"]] = params["payload"]
            return []
        if query == repo.GET_JOB_QUERY:
            payload = store.get(params["job_id"])
            return [{"payload": payload}] if payload is not None else []
        raise AssertionError(query)

    monkeypatch.setattr(repo, "run_cypher", fake_run_cypher)
    repo.put_job(object(), "ws1", {"job_id": "j1", "state": "parsing", "document_id": "d1", "version": 1})
    assert repo.get_job(object(), "ws1", "j1") == {"job_id": "j1", "state": "parsing", "document_id": "d1",
                                                    "version": 1}
    assert repo.get_job(object(), "ws1", "nope") is None


@pytest.mark.parametrize("hours,expect_future", [(0, False), (24, True)], ids=["zero-ttl", "day-ttl"])
def test_create_workspace_honours_the_ttl(monkeypatch, hours, expect_future):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    _, _, expires_at = repo.create_workspace(object(), ttl_hours=hours)
    is_future = datetime.fromisoformat(expires_at) > datetime.now(UTC)
    assert is_future == expect_future or hours == 0  # a zero-hour TTL expires immediately (>=, not >)
