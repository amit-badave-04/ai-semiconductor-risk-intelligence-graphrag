"""Opt-in Neo4j integration fixtures.

Enabled with ``RUN_NEO4J_TESTS=1`` (and a reachable server); skipped otherwise.
Credentials come from ``.env`` through Settings — run pytest from the repo root.

SAFETY
- Enterprise (e.g. Neo4j Desktop): every test database is named ``sg<...>`` and
  created/dropped here. The ``neo4j`` database (the live v1 graph) and ``system``
  are never written to by tests: sessions go through ``get_driver`` whose wrapper
  pins the database, and the fixture verifies the pinned database name before yielding.
- Community (the production edition; ``CREATE DATABASE`` is unsupported): the suite
  runs against the server's ONE default database, so it wipes it before and after each
  use. That is only allowed with ``SEMIGRAPH_ALLOW_WIPE=1`` AND when the graph is a
  throwaway — it must hold no REAL snapshot (id shaped ``snap-YYYYMMDD-<10 hex>``) —
  otherwise the test is skipped and nothing is touched.
"""

import os
import re
from contextlib import contextmanager

import pytest

from semigraph.config import get_settings
from semigraph.graph.client import get_driver

TEST_DATABASE_NAME = re.compile(r"^sg[a-z0-9.-]*$")
# what `semigraph.snapshot.compute_snapshot_id` produces; the suite's synthetic ids never look like this
REAL_SNAPSHOT_ID = r"^snap-[0-9]{8}-[0-9a-f]{10}$"


def server_edition(settings) -> str:
    """``community`` / ``enterprise`` from ``dbms.components()`` (system database)."""
    admin = get_driver(settings.model_copy(update={"neo4j_database": "system"}))
    try:
        with admin.session() as session:
            rows = session.run("CALL dbms.components() YIELD name, edition RETURN name, edition").data()
        return next((str(r["edition"]).lower() for r in rows if r["name"] == "Neo4j Kernel"), "unknown")
    finally:
        admin.close()


def wipe_guard(driver) -> str | None:
    """None when the graph is a throwaway; otherwise the reason wiping it is refused.

    Only REAL snapshots protect a graph (ids shaped like ``snap-20260925-1a2b3c4d5e``): the
    suite's own synthetic snapshots left by an earlier test in the same run are fair game."""
    with driver.session() as session:
        snapshots = session.run("MATCH (s:Snapshot) WHERE s.id =~ $re RETURN count(s) AS n", re=REAL_SNAPSHOT_ID).single()["n"]
        stamped = session.run("MATCH (n) WHERE n.snapshot_id =~ $re RETURN count(n) AS n", re=REAL_SNAPSHOT_ID).single()["n"]
    if snapshots or stamped:
        return f"the target graph holds a real snapshot ({snapshots} Snapshot node(s), {stamped} stamped node(s))"
    return None


@pytest.fixture(scope="session")
def neo4j_base_settings():
    if os.environ.get("RUN_NEO4J_TESTS") != "1":
        pytest.skip("Neo4j integration tests are opt-in: set RUN_NEO4J_TESTS=1")
    settings = get_settings()
    try:
        get_driver(settings.model_copy(update={"neo4j_database": "system"})).close()
    except RuntimeError as exc:
        pytest.skip(f"Neo4j is not reachable: {exc}")
    return settings


@pytest.fixture(scope="session")
def scratch_database(neo4j_base_settings):
    """``with scratch_database("sgtest") as (driver, settings):`` — a fresh empty graph, wiped afterwards.

    Enterprise: a dedicated database. Community: the single default database, wiped (guarded)."""
    edition = server_edition(neo4j_base_settings)

    @contextmanager
    def open_enterprise(name: str):
        assert TEST_DATABASE_NAME.match(name), f"refusing to touch database {name!r}: test databases start with 'sg'"
        admin = get_driver(neo4j_base_settings.model_copy(update={"neo4j_database": "system"}))
        try:
            with admin.session() as session:
                session.run(f"DROP DATABASE {name} IF EXISTS WAIT").consume()
                session.run(f"CREATE DATABASE {name} WAIT").consume()
            settings = neo4j_base_settings.model_copy(update={"neo4j_database": name})
            driver = get_driver(settings)
            try:
                with driver.session() as session:  # prove the wrapper really pins the test database
                    pinned = session.run("CALL db.info() YIELD name RETURN name").single()["name"]
                assert pinned == name, f"session is on {pinned!r}, expected {name!r} — aborting before any write"
                yield driver, settings
            finally:
                driver.close()
        finally:
            with admin.session() as session:
                session.run(f"DROP DATABASE {name} IF EXISTS WAIT").consume()
            admin.close()

    @contextmanager
    def open_community(name: str):
        from semigraph.graph.schema import reset_graph

        if os.environ.get("SEMIGRAPH_ALLOW_WIPE") != "1":
            pytest.skip("Community mode wipes the server's only database: set SEMIGRAPH_ALLOW_WIPE=1 "
                        "and point NEO4J_URI at a THROWAWAY instance")
        driver = get_driver(neo4j_base_settings)
        try:
            refusal = wipe_guard(driver)
            if refusal:
                pytest.skip(f"refusing to wipe: {refusal}")
            reset_graph(driver, keep_service_state=False)
            try:
                yield driver, neo4j_base_settings
            finally:
                reset_graph(driver, keep_service_state=False)
        finally:
            driver.close()

    return open_community if edition == "community" else open_enterprise
