"""Opt-in Neo4j integration fixtures.

Enabled with ``RUN_NEO4J_TESTS=1`` (and a reachable server); skipped otherwise.
Credentials come from ``.env`` through Settings — run pytest from the repo root.

SAFETY: every test database is named ``sg<...>`` and created/dropped here. The
``neo4j`` database (the live v1 graph) and ``system`` are never written to by
tests: sessions go through ``get_driver`` whose wrapper pins the database, and
the fixture verifies the pinned database name before yielding.
"""

import os
import re
from contextlib import contextmanager

import pytest

from semigraph.config import get_settings
from semigraph.graph.client import get_driver

TEST_DATABASE_NAME = re.compile(r"^sg[a-z0-9.-]*$")


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
    """``with scratch_database("sgtest") as (driver, settings):`` — a fresh empty database, dropped afterwards."""

    @contextmanager
    def open_database(name: str):
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

    return open_database
