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
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from semigraph.graph import client
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


# The documented, cross-workspace-by-design exceptions to "every query binds $ws" (module docstring): each one's
# own DELETE/UPDATE still runs the normal per-workspace, $ws-bound queries, one workspace/job at a time.
_WS_UNSCOPED_QUERIES = {"SWEEP_SELECT_QUERY", "SWEEP_ORPHANS_SELECT_QUERY", "FAIL_INTERRUPTED_JOBS_SELECT_QUERY"}


def test_every_query_binds_ws_except_the_documented_sweep_exceptions():
    for name, text in _query_constants().items():
        if name in _WS_UNSCOPED_QUERIES:
            assert "$ws" not in text, f"{name} is documented as workspace-unscoped but binds $ws anyway"
            continue
        assert "$ws" in text, f"{name} does not bind $ws"


def test_sweep_select_query_still_filters_on_expires_at():
    assert "expires_at" in repo.SWEEP_SELECT_QUERY


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


# Post-G10 fix 2 (owner's live G10 test): put_version -> _merge_document runs SET_DOCUMENT_TITLE_QUERY on every
# version, so UserDocument.title is always the LATEST upload's file name — every chunk-level read must instead
# show the CHUNK'S OWN version's title, falling back to the document title only for a version an older build wrote
# with no title of its own. Every added MATCH stays scoped by workspace_id: $ws (the G2 leak harness depends on
# every User* read being workspace-scoped).
_VERSION_TITLE_SCOPE = "UserVersion {workspace_id: $ws, document_id: c.document_id, version: c.version}"


@pytest.mark.parametrize("query_name", ["CHUNK_TEXTS_QUERY", "SEARCH_CURRENT_QUERY", "SEARCH_ASOF_QUERY",
                                        "EVIDENCE_QUERY"])
def test_chunk_level_reads_return_the_chunks_own_version_title_falling_back_to_the_document_title(query_name):
    text = getattr(repo, query_name)
    assert "coalesce(v.title, d.title) AS title" in text, f"{query_name} does not coalesce the version's own title"
    assert _VERSION_TITLE_SCOPE in text, f"{query_name}'s version lookup is not scoped by $ws and c.version"
    assert "d.title AS title" not in text, f"{query_name} still returns the document title unconditionally"


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
    def __init__(self, driver, config):
        self._driver = driver
        self._config = config

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run(self, query, **params):
        self._driver.session_run_calls.append((query, params))
        self._driver.session_run_configs.append(self._config)
        rows = self._driver.session_run_responses.get(query, [])
        return FakeResult(rows if not callable(rows) else rows(params))

    def execute_write(self, fn, *args, **kwargs):
        tx = FakeTx(self._driver.tx_responses)
        self._driver.transactions.append(tx)   # recorded BEFORE calling fn, so a raised exception still leaves the
                                                # attempted calls inspectable (a real transaction that aborts mid-way
                                                # has still sent those statements to the server before rolling back)
        return fn(tx, *args, **kwargs)


#: The default response for LOCK_WORKSPACE_QUERY: a live workspace. Every test whose point is NOT the lock/exists
#: check gets this for free (via FakeDriver's default below); a test that DOES care overrides just this one key.
_LOCK_OK = [{"workspace_id": "ws-exists"}]


class FakeDriver:
    def __init__(self, tx_responses=None, session_run_responses=None):
        self.transactions: list[FakeTx] = []
        self.session_run_calls: list[tuple[str, dict]] = []
        self.session_run_configs: list[dict] = []   # the session config each session_run_calls entry ran under
        self.session_run_responses = session_run_responses or {}
        self.tx_responses = {repo.LOCK_WORKSPACE_QUERY: _LOCK_OK, **(tx_responses or {})}

    def session(self, **kwargs):
        return FakeSession(self, kwargs)


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


def test_put_version_locks_the_workspace_before_any_other_statement():
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    assert driver.transactions[0].calls[0][0] == repo.LOCK_WORKSPACE_QUERY


def test_put_version_raises_workspace_gone_and_writes_nothing_when_the_workspace_no_longer_exists():
    """findings 2/13/24: a job that reaches put_version after the workspace was deleted (or TTL-swept) must create
    NOTHING — not the document, not the version, not a single chunk."""
    driver = FakeDriver(tx_responses={repo.LOCK_WORKSPACE_QUERY: []})
    with pytest.raises(repo.WorkspaceGone):
        repo.put_version(driver, "ws-gone", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                         method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                         chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                         suspicious=False, now=datetime.now(UTC))
    calls = driver.transactions[0].calls
    assert calls == [(repo.LOCK_WORKSPACE_QUERY, {"ws": "ws-gone"})], "no write beyond the lock check itself"


def test_create_version_query_carries_a_job_id_property():
    """Round-4 review 3, finding 27 residual 2: the committing job's id is stamped on the UserVersion node inside
    put_version's OWN transaction, so a later recovery check can tell WHICH job actually committed a version."""
    assert "job_id: $job_id" in repo.CREATE_VERSION_QUERY


def test_create_version_query_stores_the_versions_own_title():
    """Post-G10 fix 2 (owner's live G10 test): each UserVersion stores the file name IT was uploaded under, the
    same ``title`` put_version already receives — separately from UserDocument.title, which stays the LATEST
    version's file name (the document list and "New version of <title>" rely on that; unchanged by this fix)."""
    # Round-7 verification: a caller that omits the title freezes the document's title as it was at upload time,
    # rather than storing nothing and later showing whatever file name a newer version brings.
    assert "title: coalesce($title, d.title)" in repo.CREATE_VERSION_QUERY


def test_job_version_exists_query_requires_the_recovering_jobs_own_job_id():
    """The other half of the same fix: recovery must never match a version some OTHER job committed for the same
    (document_id, version) — only WHERE v.job_id = $job_id, the candidate job's own id."""
    assert "WHERE v.job_id = $job_id" in repo.JOB_VERSION_EXISTS_QUERY


def test_put_version_stores_the_caller_supplied_job_id_on_the_new_version():
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC), job_id="job-abc")
    params = next(p for q, p in driver.transactions[0].calls if q == repo.CREATE_VERSION_QUERY)
    assert params["job_id"] == "job-abc"


def test_put_version_stores_a_null_job_id_when_the_caller_does_not_pass_one():
    """job_id is keyword-only with a safe None default (every existing caller keeps working unchanged) — a version
    written with no job_id simply never matches a later job_id-based recovery check."""
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="T", version=1, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    params = next(p for q, p in driver.transactions[0].calls if q == repo.CREATE_VERSION_QUERY)
    assert params["job_id"] is None


def test_put_version_stores_the_given_title_on_the_new_version():
    """Post-G10 fix 2: the version's OWN title (the file name it was uploaded under) is stamped on the UserVersion
    node itself, not just merged onto UserDocument.title (which a LATER version would overwrite)."""
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title="g10_v2.pdf", version=2, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    params = next(p for q, p in driver.transactions[0].calls if q == repo.CREATE_VERSION_QUERY)
    assert params["title"] == "g10_v2.pdf"


def test_put_version_stores_a_null_title_on_the_new_version_when_the_caller_passes_none():
    """A caller that omits the title passes None through; CREATE_VERSION_QUERY then stores the document's title as it
    was at upload time (coalesce($title, d.title), round-7 verification; exercised live in the integration suite)."""
    driver = FakeDriver()
    repo.put_version(driver, "ws1", document_id="aaaaaaaaaaaa", title=None, version=2, content_hash="h",
                     method="text", pages=1, chars=5, chars_per_page=5.0, text="hello", units=[_unit()],
                     chunks=[_chunk()], change_report={"items_compared": True, "not_compared_reason": None},
                     suspicious=False, now=datetime.now(UTC))
    params = next(p for q, p in driver.transactions[0].calls if q == repo.CREATE_VERSION_QUERY)
    assert params["title"] is None


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
    driver = FakeDriver()   # default tx_responses already answers LOCK_WORKSPACE_QUERY with a live workspace
    assert repo.delete_workspace(driver, "ws1") is True
    assert len(driver.transactions) == 1
    formatted = {q for q, _ in driver.transactions[0].calls}
    assert driver.transactions[0].calls[0][0] == repo.LOCK_WORKSPACE_QUERY, "the lock must be taken FIRST"
    for label in repo._USER_LABELS:
        assert repo._DELETE_LABEL_TEMPLATE.format(label=label) in formatted


def test_delete_workspace_reports_false_for_an_unknown_workspace():
    driver = FakeDriver(tx_responses={repo.LOCK_WORKSPACE_QUERY: []})
    assert repo.delete_workspace(driver, "ws-nope") is False
    # The delete statements still run even when the lock finds nothing (idempotent: nothing to actually delete).


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


def test_sweep_expired_locks_each_workspace_before_deleting_it():
    driver = FakeDriver(session_run_responses={repo.SWEEP_SELECT_QUERY: [{"workspace_id": "ws-old-1"}]})
    assert repo.sweep_expired(driver, datetime.now(UTC)) == 1
    assert driver.transactions[0].calls[0][0] == repo.LOCK_WORKSPACE_QUERY


def test_sweep_expired_skips_a_workspace_already_deleted_by_a_concurrent_delete():
    """findings 2/13/24: the lock is what makes sweep_expired and delete_workspace/put_version race-safe — a
    workspace the lock no longer finds is skipped, never double-deleted or errored."""
    driver = FakeDriver(session_run_responses={repo.SWEEP_SELECT_QUERY: [{"workspace_id": "ws-already-gone"}]},
                        tx_responses={repo.LOCK_WORKSPACE_QUERY: []})
    assert repo.sweep_expired(driver, datetime.now(UTC)) == 0
    # The lock ran, but nothing else did (no DETACH DELETE against a workspace the lock didn't confirm).
    assert driver.transactions[0].calls == [(repo.LOCK_WORKSPACE_QUERY, {"ws": "ws-already-gone"})]


# ---------------------------------------------------------------------- sweep_orphans (findings 2/13/24)

def test_sweep_orphans_query_uses_a_literal_label_per_union_member_never_an_allnodesscan_form():
    """Each UNION member matches a LITERAL label (a NodeByLabelScan) rather than an unlabelled `MATCH (n)` filtered
    with `label IN labels(n)` (an AllNodesScan over the WHOLE graph, every public label included — confirmed live
    with EXPLAIN against the throwaway instance) — critical now that the sweeper always runs (finding 26)."""
    query = repo.SWEEP_ORPHANS_SELECT_QUERY
    assert "MATCH (n)" not in query and "labels(n)" not in query
    for label in repo._USER_LABELS:
        if label == "UserWorkspace":
            assert f"MATCH (n:{label})" not in query
        else:
            assert f"MATCH (n:{label})" in query


def test_sweep_orphans_selects_every_user_label_except_userworkspace_itself():
    driver = FakeDriver(session_run_responses={repo.SWEEP_ORPHANS_SELECT_QUERY: []})
    repo.sweep_orphans(driver, datetime.now(UTC))
    assert driver.session_run_calls[0][0] == repo.SWEEP_ORPHANS_SELECT_QUERY


def test_sweep_orphans_deletes_a_workspace_id_the_select_reports_as_orphaned():
    driver = FakeDriver(session_run_responses={repo.SWEEP_ORPHANS_SELECT_QUERY: [{"workspace_id": "ws-orphan-1"}]},
                        tx_responses={repo.WORKSPACE_EXISTS_QUERY: [{"n": 0}]})
    assert repo.sweep_orphans(driver, datetime.now(UTC)) == 1
    calls = driver.transactions[0].calls
    assert calls[0][0] == repo.WORKSPACE_EXISTS_QUERY, "re-verified INSIDE the delete transaction"
    deleted_labels = {q for q, _ in calls[1:]}
    for label in repo._USER_LABELS:
        formatted = repo._DELETE_LABEL_TEMPLATE.format(label=label)
        if label == "UserWorkspace":
            assert formatted not in deleted_labels, "sweep_orphans must never delete a UserWorkspace node"
        else:
            assert formatted in deleted_labels


def test_sweep_orphans_skips_a_workspace_id_that_turns_out_not_to_be_orphaned_after_all():
    driver = FakeDriver(session_run_responses={repo.SWEEP_ORPHANS_SELECT_QUERY: [{"workspace_id": "ws-live"}]},
                        tx_responses={repo.WORKSPACE_EXISTS_QUERY: [{"n": 1}]})
    assert repo.sweep_orphans(driver, datetime.now(UTC)) == 0
    assert driver.transactions[0].calls == [(repo.WORKSPACE_EXISTS_QUERY, {"ws": "ws-live"})]


def test_sweep_orphans_rejects_a_naive_now():
    with pytest.raises(TypeError):
        repo.sweep_orphans(FakeDriver(), datetime(2026, 1, 1))


# ---------------------------------------------------------------------- round-4 review, finding R4: scoped CALL

def test_sweep_orphans_select_query_uses_the_scoped_call_subquery_form():
    """Neo4j 2026.07.1 deprecates a subquery `CALL { ... }` with no variable-scope clause and logs a DEPRECATION
    notification every time it runs — the sweeper always runs this query, on every 15-minute cycle (finding 26)."""
    assert "CALL () {" in repo.SWEEP_ORPHANS_SELECT_QUERY


def test_no_query_ever_uses_the_deprecated_unscoped_call_subquery_form():
    for name, text in _query_constants().items():
        assert not re.search(r"CALL\s*\{", text), f"{name} uses the deprecated unscoped CALL {{ ... }} form"


# ---------------------------------------------------------------------- fail_interrupted_jobs (finding 27 seam)

def test_fail_interrupted_jobs_marks_a_stale_non_terminal_job_failed_with_a_rewritten_payload():
    stale_payload = json.dumps({"job_id": "j1", "state": "embedding", "document_id": "d1", "version": 1,
                                "progress": {"done": 3, "total": 10}})
    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "j1", "payload": stale_payload}],
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "j1"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (1, 0)
    query, params = [c for c in driver.session_run_calls if c[0] == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY][0]
    assert params["ws"] == "ws1" and params["job_id"] == "j1"
    rewritten = json.loads(params["payload"])
    assert rewritten["state"] == "failed"
    assert rewritten["error"] == {"code": "interrupted", "message": repo._INTERRUPTED_ERROR_MESSAGE}
    # Every other field of the ORIGINAL payload survives the rewrite — a client replaying it still sees which
    # document/version/progress the interrupted job had reached.
    assert rewritten["document_id"] == "d1" and rewritten["version"] == 1
    assert rewritten["progress"] == {"done": 3, "total": 10}


def test_fail_interrupted_jobs_selects_in_a_quiet_session_and_follows_up_in_a_default_one():
    """``UserJob``'s ``state`` / ``payload`` keys do not exist until the first upload, and the sweeper runs this SELECT
    at boot and every cycle: only that cross-workspace read opts out of UNRECOGNIZED notifications (01N52). The
    per-job recovery check and UPDATE keep the default session, so a typo in either is still reported."""
    stale_payload = json.dumps({"job_id": "j1", "state": "embedding", "document_id": "d1", "version": 1})
    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "j1", "payload": stale_payload}],
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "j1"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (1, 0)
    config_by_query = {query: config for (query, _), config in zip(driver.session_run_calls,
                                                                    driver.session_run_configs)}
    assert config_by_query == {repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: dict(client.NO_UNRECOGNIZED_NOTIFICATIONS),
                               repo.JOB_VERSION_EXISTS_QUERY: {},
                               repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: {}}


def test_fail_interrupted_jobs_uses_a_threshold_derived_from_now_and_the_module_constant():
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    driver = FakeDriver(session_run_responses={repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: []})
    repo.fail_interrupted_jobs(driver, now)
    _, params = driver.session_run_calls[0]
    assert params["threshold"] == now - timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S)
    assert set(params["terminal_states"]) == {"ready", "failed"}


def test_fail_interrupted_jobs_defaults_now_to_the_current_instant():
    driver = FakeDriver(session_run_responses={repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: []})
    before = datetime.now(UTC)
    assert repo.fail_interrupted_jobs(driver) == (0, 0)
    _, params = driver.session_run_calls[0]
    implied_now = params["threshold"] + timedelta(seconds=repo.FAIL_INTERRUPTED_AFTER_S)
    assert before <= implied_now <= datetime.now(UTC)


def test_fail_interrupted_jobs_skips_a_job_the_update_no_longer_matches():
    """The per-job UPDATE re-checks the state; a job that raced to ready/failed between the select and here must
    not be double-counted (0 rows back from the UPDATE)."""
    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "j1",
                                                    "payload": json.dumps({"job_id": "j1", "state": "embedding"})}],
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: []})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (0, 0)


def test_fail_interrupted_jobs_never_touches_an_excluded_job_and_honours_a_caller_threshold():
    """The upload sweeper passes the jobs its own process still runs (``exclude``) and a threshold derived from the job
    budgets (``older_than_s``): an excluded job is never updated however stale its stored age looks."""
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    rows = [{"workspace_id": "ws1", "job_id": jid, "payload": json.dumps({"job_id": jid, "state": "embedding"})}
            for jid in ("live", "dead")]
    driver = FakeDriver(session_run_responses={repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: rows,
                                               repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "dead"}]})
    assert repo.fail_interrupted_jobs(driver, now, older_than_s=1890, exclude={("ws1", "live")}) == (1, 0)
    select_params = driver.session_run_calls[0][1]
    assert select_params["threshold"] == now - timedelta(seconds=1890)
    updated = [params["job_id"] for query, params in driver.session_run_calls
               if query == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY]
    assert updated == ["dead"]


def test_fail_interrupted_jobs_recovers_a_job_to_ready_when_its_own_version_already_committed():
    """Round-4 review, finding 27 residual 1: the job's terminal 'ready' write kept failing, but put_version had
    already committed the version underneath it — fail_interrupted_jobs must recover the job to 'ready', never
    mark it 'failed'/'interrupted' over a version that actually succeeded. Round-4 review 3, finding 27 residual 2:
    recovery must key off THIS job's own job_id, not merely (document_id, version) — the fake JOB_VERSION_EXISTS_QUERY
    response here is a callable that only answers a row when the query is run with the candidate's OWN job_id, so
    this test also pins that put_job_id is actually passed through, not merely accepted and ignored."""
    stale_payload = json.dumps({"job_id": "j1", "state": "indexing", "document_id": "d1", "version": 2,
                                "progress": {"done": 40, "total": 40}})
    version_row = {"chunks": 40, "units": 12, "items_compared": True, "not_compared_reason": None,
                  "suspicious": False}

    def version_exists_response(params):
        return [version_row] if params.get("job_id") == "j1" else []

    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "j1", "payload": stale_payload}],
        repo.JOB_VERSION_EXISTS_QUERY: version_exists_response,
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "j1"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (0, 1)
    query, params = [c for c in driver.session_run_calls if c[0] == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY][0]
    assert params["state"] == "ready"
    rewritten = json.loads(params["payload"])
    assert rewritten["state"] == "ready"
    assert rewritten["chunks"] == 40 and rewritten["units"] == 12
    assert "error" not in rewritten
    assert rewritten["document_id"] == "d1" and rewritten["version"] == 2
    version_exists_call = [c for c in driver.session_run_calls if c[0] == repo.JOB_VERSION_EXISTS_QUERY][0]
    assert version_exists_call[1] == {"ws": "ws1", "document_id": "d1", "version": 2, "job_id": "j1"}


def test_fail_interrupted_jobs_still_marks_failed_when_no_committed_version_exists():
    """The other half: a job with a document_id/version but no committed UserVersion yet is genuinely interrupted,
    exactly as before this fix."""
    stale_payload = json.dumps({"job_id": "j1", "state": "embedding", "document_id": "d1", "version": 1})
    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "j1", "payload": stale_payload}],
        repo.JOB_VERSION_EXISTS_QUERY: [],
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "j1"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (1, 0)
    query, params = [c for c in driver.session_run_calls if c[0] == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY][0]
    assert params["state"] == "failed"
    assert json.loads(params["payload"])["error"] == {"code": "interrupted", "message": repo._INTERRUPTED_ERROR_MESSAGE}


def test_fail_interrupted_jobs_never_recovers_a_failed_job_from_a_different_jobs_committed_version():
    """Round-4 review 3, finding 27 residual 2 (secRel LOW repo.py:635 / corUi LOW repo.py:130): job A failed for
    real, but its own terminal 'failed' write also exhausted its retries, so its persisted state stayed
    non-terminal. The user re-uploaded, and job B independently computed and committed the SAME (document_id,
    version) — before this fix, matching only on (document_id, version) would "recover" job A to ready using job
    B's stats, contradicting what job A's own live watchers saw (failed). The fake JOB_VERSION_EXISTS_QUERY response
    only answers a row for job B's id, proving job A's own recovery check (job_id='job-a') finds nothing and falls
    through to interrupted."""
    stale_payload = json.dumps({"job_id": "job-a", "state": "embedding", "document_id": "d1", "version": 2})
    committed_by_job_b = {"chunks": 3, "units": 1, "items_compared": True, "not_compared_reason": None,
                          "suspicious": False}

    def version_exists_response(params):
        return [committed_by_job_b] if params.get("job_id") == "job-b" else []

    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "job-a",
                                                    "payload": stale_payload}],
        repo.JOB_VERSION_EXISTS_QUERY: version_exists_response,
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "job-a"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (1, 0), \
        "job A must fall through to interrupted, never be recovered off job B's committed version"
    query, params = [c for c in driver.session_run_calls if c[0] == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY][0]
    assert params["state"] == "failed"
    rewritten = json.loads(params["payload"])
    assert rewritten["error"] == {"code": "interrupted", "message": repo._INTERRUPTED_ERROR_MESSAGE}
    version_exists_call = [c for c in driver.session_run_calls if c[0] == repo.JOB_VERSION_EXISTS_QUERY][0]
    assert version_exists_call[1]["job_id"] == "job-a", "the recovery check must use the CANDIDATE's own job_id"


def test_fail_interrupted_jobs_never_recovers_a_job_whose_own_version_never_committed_even_though_a_later_job_did():
    """The second reviewer scenario, phrased the other way round: job A's own version NEVER committed (its
    put_version never even ran, or aborted) — there is no UserVersion with job_id='job-a' at all — while a LATER
    job (job B) independently committed the same (document_id, version). Job A must still fall through to
    interrupted: a version existing at all for that (document_id, version) must never be conflated with THIS job
    having produced it."""
    stale_payload = json.dumps({"job_id": "job-a", "state": "indexing", "document_id": "d1", "version": 5})

    def version_exists_response(params):
        # Only job-b's own version is findable; job-a's put_version genuinely never committed anything.
        if params.get("job_id") == "job-b":
            return [{"chunks": 2, "units": 1, "items_compared": True, "not_compared_reason": None,
                    "suspicious": False}]
        return []

    driver = FakeDriver(session_run_responses={
        repo.FAIL_INTERRUPTED_JOBS_SELECT_QUERY: [{"workspace_id": "ws1", "job_id": "job-a",
                                                    "payload": stale_payload}],
        repo.JOB_VERSION_EXISTS_QUERY: version_exists_response,
        repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY: [{"job_id": "job-a"}]})
    assert repo.fail_interrupted_jobs(driver, datetime.now(UTC)) == (1, 0)
    query, params = [c for c in driver.session_run_calls if c[0] == repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY][0]
    assert params["state"] == "failed"


def test_the_default_threshold_sits_above_the_default_parse_and_embed_budgets():
    from semigraph.config import Settings

    s = Settings(_env_file=None)
    assert repo.FAIL_INTERRUPTED_AFTER_S > s.upload_parse_timeout_s + s.upload_embed_timeout_s


def test_fail_interrupted_jobs_rejects_a_naive_now():
    with pytest.raises(TypeError):
        repo.fail_interrupted_jobs(FakeDriver(), datetime(2026, 1, 1))


def test_fail_interrupted_job_update_query_re_checks_the_state_before_overwriting():
    assert "WHERE NOT j.state IN $terminal_states" in repo.FAIL_INTERRUPTED_JOB_UPDATE_QUERY


def test_interrupted_error_message_stays_in_sync_with_uploads_jobs():
    """repo.py deliberately never imports uploads.jobs (it must stay strictly below the job layer), so the fixed
    "interrupted" message is duplicated by hand in both places; this test is what actually keeps them in sync."""
    from semigraph.uploads import jobs

    assert repo._INTERRUPTED_ERROR_MESSAGE == jobs.JOB_ERROR_MESSAGES["interrupted"]
    assert set(repo._JOB_TERMINAL_STATES) == set(jobs.TERMINAL_STATES)


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
    assert repo.quota(object(), "ws1") == {"documents": 0, "versions_by_document": {}, "pages": 0,
                                           "pages_by_document": {}, "embedded_tokens": 0}


def test_quota_shape_with_documents(monkeypatch):
    rows = [{"embedded_tokens": 100, "document_id": "aaaaaaaaaaaa", "version_count": 3, "current_pages": 5},
            {"embedded_tokens": 100, "document_id": "bbbbbbbbbbbb", "version_count": 1, "current_pages": 2}]
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows)
    assert repo.quota(object(), "ws1") == {
        "documents": 2, "versions_by_document": {"aaaaaaaaaaaa": 3, "bbbbbbbbbbbb": 1},
        "pages": 7, "pages_by_document": {"aaaaaaaaaaaa": 5, "bbbbbbbbbbbb": 2}, "embedded_tokens": 100}


def test_quota_pages_by_document_lets_a_same_size_reversion_at_the_cap_be_distinguished_from_a_new_document(
        monkeypatch):
    """C3 item 5 (docs/v2/M4_PLAN.md 15.8): the workspace-page-cap check (``uploads.jobs``) needs the PER-DOCUMENT
    current page count, not just the workspace total, to accept a same-size new version of a document already at
    the cap while still refusing a brand-new document that would push the total over."""
    rows = [{"embedded_tokens": 0, "document_id": "aaaaaaaaaaaa", "version_count": 1, "current_pages": 120}]
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: rows)
    quota = repo.quota(object(), "ws1")
    assert quota["pages"] == 120
    assert quota["pages_by_document"] == {"aaaaaaaaaaaa": 120}


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
            return [{"job_id": params["job_id"]}]   # a live workspace: the leading MATCH found a row
        if query == repo.GET_JOB_QUERY:
            payload = store.get(params["job_id"])
            return [{"payload": payload}] if payload is not None else []
        raise AssertionError(query)

    monkeypatch.setattr(repo, "run_cypher", fake_run_cypher)
    repo.put_job(object(), "ws1", {"job_id": "j1", "state": "parsing", "document_id": "d1", "version": 1})
    assert repo.get_job(object(), "ws1", "j1") == {"job_id": "j1", "state": "parsing", "document_id": "d1",
                                                    "version": 1}
    assert repo.get_job(object(), "ws1", "nope") is None


def test_put_job_raises_workspace_gone_when_the_workspace_no_longer_exists(monkeypatch):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])   # the leading MATCH found no row
    with pytest.raises(repo.WorkspaceGone):
        repo.put_job(object(), "ws-gone", {"job_id": "j1", "state": "parsing", "document_id": "d1", "version": 1})


def test_put_job_query_takes_the_workspace_lock_before_merging_the_job():
    """A plain MATCH does not wait on delete_workspace's in-flight transaction (Neo4j reads see only committed
    data): put_job must take the SAME write lock delete_workspace/put_version take, and it must come BEFORE the
    MERGE, or a job finishing mid-delete can still commit an orphaned UserJob."""
    lock_pos = repo.PUT_JOB_QUERY.index("SET w._lock = true")
    merge_pos = repo.PUT_JOB_QUERY.index("MERGE (j:UserJob")
    assert lock_pos < merge_pos


@pytest.mark.parametrize("hours,expect_future", [(0, False), (24, True)], ids=["zero-ttl", "day-ttl"])
def test_create_workspace_honours_the_ttl(monkeypatch, hours, expect_future):
    monkeypatch.setattr(repo, "run_cypher", lambda d, q, **p: [])
    _, _, expires_at = repo.create_workspace(object(), ttl_hours=hours)
    is_future = datetime.fromisoformat(expires_at) > datetime.now(UTC)
    assert is_future == expect_future or hours == 0  # a zero-hour TTL expires immediately (>=, not >)
