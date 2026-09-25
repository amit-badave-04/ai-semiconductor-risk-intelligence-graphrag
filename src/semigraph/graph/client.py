"""Neo4j connection + query helpers (ported from notebooks 05 and 13).

The notebooks' prerequisite guard becomes ``get_driver`` (fail fast with a
setup hint if the local DBMS isn't running); notebook 13's ``run_cypher``
convenience is the SDK-wide way to get plain ``list[dict]`` results.

``get_driver`` returns a :class:`DatabaseDriver`: a thin wrapper that pins
every session to ``Settings.neo4j_database`` so a build can target a scratch
database (e.g. ``sgtest``) without touching the ``neo4j`` database that holds
a live graph.
"""

import logging

from neo4j import Driver, GraphDatabase

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.graph.client")

# neo4j's Driver.execute_query(query_, parameters_, routing_, database_, ...):
# database_ is the third positional argument after the query.
_EXECUTE_QUERY_DATABASE_POSITION = 2


class DatabaseDriver:
    """A driver that targets one database unless a call names another.

    Sessions and ``execute_query`` calls that do not pass a database get the
    configured one; an explicitly passed database (even ``"system"`` or
    ``None``) is never overridden. An empty database name disables the
    injection and leaves the choice to the server's home database. Every
    other attribute (``verify_connectivity``, ``get_server_info``, ``close``
    ...) is delegated to the wrapped driver.
    """

    def __init__(self, driver: Driver, database: str):
        self._driver = driver
        self._database = database

    @property
    def database(self) -> str:
        return self._database

    @property
    def wrapped(self) -> Driver:
        """The underlying driver (for callers that need its exact type)."""
        return self._driver

    def session(self, **config):
        if self._database and "database" not in config:
            config = {**config, "database": self._database}
        return self._driver.session(**config)

    def execute_query(self, query, *args, **kwargs):
        if (self._database and "database_" not in kwargs
                and len(args) <= _EXECUTE_QUERY_DATABASE_POSITION):
            kwargs = {**kwargs, "database_": self._database}
        return self._driver.execute_query(query, *args, **kwargs)

    def __enter__(self) -> "DatabaseDriver":
        return self

    def __exit__(self, *exc_info) -> None:
        self._driver.close()

    def __getattr__(self, name: str):
        if name in ("_driver", "_database"):  # not initialised yet (copy/pickle): avoid recursion
            raise AttributeError(name)
        return getattr(self._driver, name)


def get_driver(settings: Settings | None = None) -> DatabaseDriver:
    """Open a Neo4j driver and verify connectivity (ported from notebook 05).

    Raises RuntimeError with setup guidance when the DBMS is unreachable,
    exactly like the notebooks' prerequisite guard cell.
    """
    settings = settings or get_settings()
    driver = None
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        driver.verify_connectivity()
    except Exception as e:
        if driver is not None:
            driver.close()
        raise RuntimeError(
            "Neo4j is not reachable. Install Neo4j Desktop (https://neo4j.com/download/), "
            "create & START a local DBMS (5.x), and put its password in .env as "
            "NEO4J_PASSWORD. See README 'Setup'."
        ) from e
    logger.info("Neo4j reachable — %s (database %s)", driver.get_server_info().agent,
                settings.neo4j_database or "<home>")
    return DatabaseDriver(driver, settings.neo4j_database)


def run_cypher(driver: Driver, query: str, **params) -> list[dict]:
    """Run one Cypher query and return rows as dicts (ported from notebook 13)."""
    with driver.session() as s:
        return [dict(r) for r in s.run(query, **params)]
