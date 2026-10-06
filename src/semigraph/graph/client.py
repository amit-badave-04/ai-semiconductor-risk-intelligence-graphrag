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
# GraphDatabase.driver accepts max_connection_pool_size, connection_acquisition_timeout, connection_timeout,
# max_transaction_retry_time, initial_retry_delay and liveness_check_timeout (an unknown keyword is a
# ConfigurationError at construction).
#
# The bound is built into the driver's settings, not added by a caller. A managed transaction (reserve, settle, renew)
# always makes at least TWO attempts: the driver starts its retry timer after the first failure and checks it only
# after the second. So each attempt may take half of what the operation budget leaves after the retry delay, and the
# retry window itself is short. A closed or silent server is then given up on inside the budget (0.47 s for a read,
# 1.0 s for a managed transaction; see ``serve/state/backend.py`` for the measurements).
#
# A connection that is already in the pool is a different case: once its handshake is done the driver reads from it
# with no deadline of ours, only the server's own receive-timeout hint (120 s). Measured: with the server gone silent
# the first operation on a pooled connection blocked for as long as the test allowed. A liveness check of 0 makes every
# acquire prove the connection alive first (one RESET round trip), inside the attempt timeout, and drop it if it does
# not answer. An operation that is already in flight when the server goes silent is not covered.
STATE_POOL_SIZE = 8
STATE_RETRY_WINDOW_S = 0.2            # max_transaction_retry_time: a transient failure is retried only this long
STATE_RETRY_DELAY_S = 0.05            # initial_retry_delay (the driver's default of 1 s alone would spend the budget)
STATE_LIVENESS_CHECK_S = 0            # liveness_check_timeout: a pooled connection is checked on every acquire
RETRY_DELAY_JITTER = 1.2              # the driver varies each delay by up to 20%
MIN_ATTEMPT_S = 0.05                  # the floor of one connection attempt, for a tiny operation budget
STATE_ACQUISITION_DEFAULT_S = 0.5      # Settings.state_connection_acquisition_s, until config.py has the field
STATE_OP_TIMEOUT_DEFAULT_S = 1.0       # Settings.state_op_timeout_s, likewise


def state_attempt_timeout_s(op_timeout_s: float, acquisition_s: float) -> float:
    """What ONE connection attempt (pool acquisition, TCP connect and handshake) may take: the configured acquisition
    timeout, but never more than half of the operation budget left after the longest retry delay. The configured value
    stays a ceiling; with the defaults (1 s budget, 0.5 s acquisition) an attempt gets 0.47 s, so the two attempts of a
    managed transaction and the delay between them add up to 1 s."""
    derived = (op_timeout_s - STATE_RETRY_DELAY_S * RETRY_DELAY_JITTER) / 2
    return max(MIN_ATTEMPT_S, min(acquisition_s, derived))


def make_state_driver(settings: Settings) -> DatabaseDriver:
    """A SECOND driver for the state operations of ``serve/state``: a pool of 8; every connection attempt bounded by
    :func:`state_attempt_timeout_s` (pool acquisition and TCP connect alike); a transaction retry window of 0.2 s with
    a first retry after 0.05 s, so the operation budget ``state_op_timeout_s`` (1 s) is not spent on the driver's own
    waiting; and a liveness check on every acquire of a pooled connection. Pinned to the configured database like
    :func:`get_driver`. No connectivity check here: a database that is down at boot must surface as
    ``StateUnavailable`` from the first state call (which fails closed), not as a crash."""
    op_timeout_s = getattr(settings, "state_op_timeout_s", STATE_OP_TIMEOUT_DEFAULT_S)
    attempt_s = state_attempt_timeout_s(
        op_timeout_s, getattr(settings, "state_connection_acquisition_s", STATE_ACQUISITION_DEFAULT_S))
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
        max_connection_pool_size=STATE_POOL_SIZE,
        connection_acquisition_timeout=attempt_s, connection_timeout=attempt_s,
        max_transaction_retry_time=min(STATE_RETRY_WINDOW_S, op_timeout_s),
        initial_retry_delay=STATE_RETRY_DELAY_S, liveness_check_timeout=STATE_LIVENESS_CHECK_S)
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
