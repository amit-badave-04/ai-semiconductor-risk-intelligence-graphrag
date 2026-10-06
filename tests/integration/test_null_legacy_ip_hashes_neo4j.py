"""The legacy IP-hash null against a REAL Neo4j instance (M5a I3: ``serve.store.null_legacy_ip_hashes`` and its dry run
``count_legacy_ip_hashes``; docs/v2/M5_DECISIONS.md decision 12).

The unit tests (tests/test_serve_guard_pepper.py) pin the Cypher TEXT with a fake driver. This proves the text runs:
that ``MATCH ... CALL (q) { SET ... } IN TRANSACTIONS OF n ROWS`` is accepted as an auto-commit statement, that it
nulls every row it is meant to and no other, and that a batch smaller than the table skips no row (the MATCH filters on
the very property the subquery writes). It does NOT show where the commit boundaries fall: a batch of 1 over 7 rows
must null all 7, which one big transaction would do too.

Opt-in (``RUN_NEO4J_TESTS=1`` and ``SEMIGRAPH_ALLOW_WIPE=1``) and hard-pinned to the THROWAWAY test instance ONLY
(``bolt://localhost:7898``), never 7699 (the real local graph), never 7687 (Neo4j Desktop), never production, whatever
``NEO4J_URI`` is set to: the null is IRREVERSIBLE and acts on EVERY legacy ``SvcQuery`` row of the database it is
pointed at, so the connection is hard-coded here and not read from ``.env``. ``conftest.scratch_database`` is
deliberately not used (its Community path wipes the whole server database, under a concurrent worker's fixtures). A
test whose instance already holds a legacy ledger row of someone else is SKIPPED before it seeds anything: the null
would destroy that row. Every row it creates carries a unique tag (as ``day``, or as ``strategy`` for rows written
through ``log_query``) and is deleted afterwards, whatever the outcome.
"""

import os
import uuid
from collections import defaultdict

import pytest

pytest.importorskip("neo4j")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import get_driver  # noqa: E402
from semigraph.serve import store  # noqa: E402

THROWAWAY_URI = "bolt://localhost:7898"
THROWAWAY_USER = "neo4j"
THROWAWAY_PASSWORD = "itest-throwaway-only"  # gitleaks:allow
LEGACY_HASH, PEPPERED_HASH = "0123456789abcdef", "fedcba9876543210"
PEPPER_VERSION = 2
LEGACY, PEPPERED, NO_HASH, NULLED = 7, 4, 2, 3          # rows seeded of each kind: a batch of 3 runs three batches

SEED = """UNWIND range(1, $n) AS i
          CREATE (q:SvcQuery {id: $tag + ':' + $kind + ':' + toString(i), day: $tag, kind: $kind, strategy: 'hybrid',
                              cached: false, cost_usd: 0.01})
          SET q += $props"""
READ_BACK = """MATCH (q:SvcQuery {day: $tag})
               RETURN q.kind AS kind, q.ip_hash AS ip_hash, q.ip_hash_v AS v, q.strategy AS strategy,
                      q.cost_usd AS cost"""
CLEAN_UP = "MATCH (q:SvcQuery) WHERE q.day = $tag OR q.strategy = $tag DETACH DELETE q"


@pytest.fixture(scope="module")
def driver():
    if os.environ.get("RUN_NEO4J_TESTS") != "1":
        pytest.skip("Neo4j integration tests are opt-in: set RUN_NEO4J_TESTS=1")
    if os.environ.get("SEMIGRAPH_ALLOW_WIPE") != "1":
        pytest.skip("this suite nulls and deletes ledger rows: set SEMIGRAPH_ALLOW_WIPE=1 too")
    settings = Settings(neo4j_uri=THROWAWAY_URI, neo4j_user=THROWAWAY_USER, neo4j_password=THROWAWAY_PASSWORD,
                        neo4j_database="")
    try:
        d = get_driver(settings)
    except RuntimeError as exc:
        pytest.skip(f"the throwaway Neo4j instance ({THROWAWAY_URI}) is not reachable: {exc}")
    yield d
    d.close()


def _seed(driver, tag: str, kind: str, n: int, **props) -> None:
    with driver.session() as session:
        session.run(SEED, tag=tag, kind=kind, n=n, props=props).consume()


@pytest.fixture
def ledger(driver):
    """The tag of four kinds of synthetic ledger row: legacy (an unsalted hash, no version: what the null is for),
    peppered (version 2), no hash at all, and already nulled (version 0, no hash)."""
    foreign = store.count_legacy_ip_hashes(driver)
    if foreign:
        pytest.skip(f"the instance holds {foreign} legacy ledger row(s) that are not this test's: "
                    "the null would erase them")
    tag = f"itest-null-{uuid.uuid4().hex[:10]}"
    try:
        _seed(driver, tag, "legacy", LEGACY, ip_hash=LEGACY_HASH)
        _seed(driver, tag, "peppered", PEPPERED, ip_hash=PEPPERED_HASH, ip_hash_v=PEPPER_VERSION)
        _seed(driver, tag, "no_hash", NO_HASH)
        _seed(driver, tag, "nulled", NULLED, ip_hash_v=0)
        yield tag
    finally:
        with driver.session() as session:
            session.run(CLEAN_UP, tag=tag).consume()


def _rows_by_kind(driver, tag: str) -> dict[str, list[dict]]:
    with driver.session() as session:
        rows = [dict(r) for r in session.run(READ_BACK, tag=tag)]
    by_kind = defaultdict(list)
    for row in rows:
        by_kind[row["kind"]].append(row)
    return by_kind


def _assert_only_the_legacy_rows_were_nulled(driver, tag: str) -> None:
    by_kind = _rows_by_kind(driver, tag)
    assert len(by_kind["legacy"]) == LEGACY
    assert all(r["ip_hash"] is None and r["v"] == 0 for r in by_kind["legacy"])
    assert len(by_kind["peppered"]) == PEPPERED
    assert all(r["ip_hash"] == PEPPERED_HASH and r["v"] == PEPPER_VERSION for r in by_kind["peppered"])
    assert len(by_kind["no_hash"]) == NO_HASH
    # nothing to null on these: they must not gain a version either
    assert all(r["ip_hash"] is None and r["v"] is None for r in by_kind["no_hash"])
    assert len(by_kind["nulled"]) == NULLED
    assert all(r["ip_hash"] is None and r["v"] == 0 for r in by_kind["nulled"])
    everything = [r for rows in by_kind.values() for r in rows]
    # only the two hash columns changed
    assert all(r["strategy"] == "hybrid" and r["cost"] == 0.01 for r in everything)


def test_the_dry_run_counts_only_rows_with_a_hash_and_no_version(driver, ledger):
    assert store.count_legacy_ip_hashes(driver) == LEGACY


@pytest.mark.parametrize("batch", [1, 3, 5000], ids=["one-row-batches", "three-batches", "one-batch"])
def test_the_null_touches_only_the_legacy_rows_whatever_the_batch_size(driver, ledger, batch):
    assert store.null_legacy_ip_hashes(driver, batch=batch) == LEGACY
    _assert_only_the_legacy_rows_were_nulled(driver, ledger)
    assert store.count_legacy_ip_hashes(driver) == 0


def test_running_the_null_again_nulls_nothing(driver, ledger):
    store.null_legacy_ip_hashes(driver, batch=3)
    assert store.null_legacy_ip_hashes(driver, batch=3) == 0
    _assert_only_the_legacy_rows_were_nulled(driver, ledger)


def test_a_row_logged_with_a_version_survives_the_null_and_one_logged_without_is_nulled(driver, ledger):
    """The real ``log_query`` Cypher: the ``SET q.ip_hash_v`` it appends is valid, and the version protects the row."""
    store.log_query(driver, ip_hash=PEPPERED_HASH, strategy=ledger, cached=False, ip_hash_v=PEPPER_VERSION)
    store.log_query(driver, ip_hash=LEGACY_HASH, strategy=ledger, cached=True)
    assert store.count_legacy_ip_hashes(driver) == LEGACY + 1
    assert store.null_legacy_ip_hashes(driver, batch=3) == LEGACY + 1
    with driver.session() as session:
        logged = {r["cached"]: dict(r) for r in session.run(
            "MATCH (q:SvcQuery {strategy: $tag}) RETURN q.cached AS cached, q.ip_hash AS ip_hash, q.ip_hash_v AS v",
            tag=ledger)}
    assert logged[False] == {"cached": False, "ip_hash": PEPPERED_HASH, "v": PEPPER_VERSION}
    assert logged[True] == {"cached": True, "ip_hash": None, "v": 0}
