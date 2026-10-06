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
from collections.abc import Mapping
from types import MappingProxyType

from neo4j import Driver, GraphDatabase, NotificationClassification

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.graph.client")

# neo4j's Driver.execute_query(query_, parameters_, routing_, database_, ...):
# database_ is the third positional argument after the query.
_EXECUTE_QUERY_DATABASE_POSITION = 2

# Session options for a read that names a property key the database may never
# have seen yet (one written only on failure, or a label's keys before its
# first node): the server then sends no UNRECOGNIZED notification (01N50,
# 01N51, 01N52: "... does not exist") for that session. No severity floor, so
# every other notification (deprecation, performance, ...) still reaches the
# ``neo4j.notifications`` logger. Scope it to one query at a time, never the
# driver: ``run_cypher(..., session_config_=NO_UNRECOGNIZED_NOTIFICATIONS)``
# or ``driver.session(**NO_UNRECOGNIZED_NOTIFICATIONS)``.
NO_UNRECOGNIZED_NOTIFICATIONS = MappingProxyType(
    {"notifications_disabled_classifications": (NotificationClassification.UNRECOGNIZED,)})


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


# The state operations (``serve/state``) get a driver of their own so a stalled database can never starve the read
# path of its connections, and every wait is short. Keyword names verified against neo4j 6.2.0 and 6.3.0 (2026-10-06):
# GraphDatabase.driver accepts max_connection_pool_size, connection_acquisition_timeout, connection_timeout and
# max_transaction_retry_time (an unknown keyword is a ConfigurationError at construction).
STATE_POOL_SIZE = 8
STATE_CONNECT_TIMEOUT_S = 1.0
STATE_ACQUISITION_DEFAULT_S = 0.5      # Settings.state_connection_acquisition_s, until config.py has the field
STATE_OP_TIMEOUT_DEFAULT_S = 1.0       # Settings.state_op_timeout_s, likewise


def make_state_driver(settings: Settings) -> DatabaseDriver:
    """A SECOND driver for the state operations of ``serve/state``: a pool of 8, a pool-acquisition timeout of
    ``state_connection_acquisition_s`` (0.5 s), a TCP connect timeout of 1 s and a transaction retry window of
    ``state_op_timeout_s`` (1 s), so a managed transaction stops retrying when the operation's budget is spent. Pinned to
    the configured database like :func:`get_driver`. No connectivity check here: a database that is down at boot must
    surface as ``StateUnavailable`` from the first state call (which fails closed), not as a crash."""
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
        max_connection_pool_size=STATE_POOL_SIZE,
        connection_acquisition_timeout=getattr(settings, "state_connection_acquisition_s", STATE_ACQUISITION_DEFAULT_S),
        connection_timeout=STATE_CONNECT_TIMEOUT_S,
        max_transaction_retry_time=getattr(settings, "state_op_timeout_s", STATE_OP_TIMEOUT_DEFAULT_S))
    return DatabaseDriver(driver, settings.neo4j_database)


def run_cypher(driver: Driver, query: str, *,
               session_config_: Mapping[str, object] | None = None,
               **params) -> list[dict]:
    """Run one Cypher query and return rows as dicts (ported from notebook 13).

    ``session_config_`` (e.g. :data:`NO_UNRECOGNIZED_NOTIFICATIONS`) goes to
    ``driver.session``; the trailing underscore is the driver's own
    ``execute_query`` convention, so it never collides with a query parameter.
    """
    with driver.session(**(session_config_ or {})) as s:
        return [dict(r) for r in s.run(query, **params)]
