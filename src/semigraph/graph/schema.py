"""Graph schema DDL application (ported from notebook 05).

The DDL lives in the packaged artifact ``semigraph/artifacts/schema.cypher``
(notebook 05 emitted it to ``artifacts/schema.cypher``; the SDK ships the same
file inside the wheel). Every statement is idempotent (``IF NOT EXISTS``) so
re-applying is always safe.

Schema-locked invariant: the two vector indexes (``evidence_embedding``,
``risk_embedding``) are 1024-dim cosine — matching Qwen3-Embedding-0.6B.
Do NOT change dimensions without re-embedding the whole graph.
"""

import logging

from neo4j import Driver

from ..artifacts import read_schema_cypher

logger = logging.getLogger("semigraph.graph.schema")


def apply_schema(driver: Driver) -> int:
    """Apply the packaged schema DDL statement-by-statement (ported from notebook 05).

    Splits ``schema.cypher`` on ';', skipping blanks. Returns the number of
    statements applied. Idempotent — re-running never errors or duplicates.
    """
    statements = [s.strip() for s in read_schema_cypher().split(";") if s.strip()]
    with driver.session() as session:
        for stmt in statements:
            session.run(stmt)
    logger.info("schema applied (idempotent) — %d statements", len(statements))
    return len(statements)
