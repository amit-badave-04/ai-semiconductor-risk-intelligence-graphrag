"""Neo4j connection + query helpers (ported from notebooks 05 and 13).

The notebooks' prerequisite guard becomes ``get_driver`` (fail fast with a
setup hint if the local DBMS isn't running); notebook 13's ``run_cypher``
convenience is the SDK-wide way to get plain ``list[dict]`` results.
"""

import logging

from neo4j import Driver, GraphDatabase

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.graph.client")


def get_driver(settings: Settings | None = None) -> Driver:
    """Open a Neo4j driver and verify connectivity (ported from notebook 05).

    Raises RuntimeError with setup guidance when the DBMS is unreachable,
    exactly like the notebooks' prerequisite guard cell.
    """
    settings = settings or get_settings()
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        driver.verify_connectivity()
    except Exception as e:
        raise RuntimeError(
            "Neo4j is not reachable. Install Neo4j Desktop (https://neo4j.com/download/), "
            "create & START a local DBMS (5.x), and put its password in .env as "
            "NEO4J_PASSWORD. See README 'Setup'."
        ) from e
    logger.info("Neo4j reachable — %s", driver.get_server_info().agent)
    return driver


def run_cypher(driver: Driver, query: str, **params) -> list[dict]:
    """Run one Cypher query and return rows as dicts (ported from notebook 13)."""
    with driver.session() as s:
        return [dict(r) for r in s.run(query, **params)]
