"""Graph schema DDL application (ported from notebook 05).

The DDL lives in the packaged artifact ``semigraph/artifacts/schema.cypher``
(notebook 05 emitted it to ``artifacts/schema.cypher``; the SDK ships the same
file inside the wheel, and the repo-root copy must stay byte-identical). Every
statement is idempotent (``IF NOT EXISTS``) so re-applying is always safe.

Schema-locked invariants:

- the two vector indexes (``evidence_embedding``, ``risk_embedding``) are
  1024-dim cosine — matching Qwen3-Embedding-0.6B. Do NOT change dimensions
  without re-embedding the whole graph.
- they are FILTERED vector indexes (``WITH [...]`` lists the properties a
  ``SEARCH ... WHERE`` may filter on). Filter properties can only be declared
  at creation time, and ``IF NOT EXISTS`` would silently keep an old unfiltered
  index of the same name — so :func:`apply_schema` refuses to run against one
  and tells the operator to rebuild.
"""

import logging
import re

from neo4j import Driver

from ..artifacts import read_schema_cypher

logger = logging.getLogger("semigraph.graph.schema")

# Named indexes a rebuild must recreate: reset_graph drops them, apply_schema
# creates them again (vector indexes cannot change their filter properties in place).
REBUILD_INDEXES = ("evidence_embedding", "risk_embedding", "evidence_text_ft", "risk_summary_ft")

# Nodes labelled Svc* are live service state (answer cache, spend ledger,
# kill switch) and survive a graph rebuild.
SERVICE_LABEL_PREFIX = "Svc"

# Private state: Svc* (service) and User* (M4 upload workspaces, docs/v2/M4_PLAN.md 4.4). Private nodes never appear in
# public counts, survive a rebuild, and are never linked to a public node. Each private node carries exactly one label.
PRIVATE_LABEL_PREFIXES = (SERVICE_LABEL_PREFIX, "User")


def is_private_label(label: str) -> bool:
    return label.startswith(PRIVATE_LABEL_PREFIXES)


def private_label_predicate(var: str = "n") -> str:
    """Cypher that is true when node ``var`` carries a private label (``var`` is a Cypher identifier chosen by the caller)."""
    if not var.isidentifier():
        raise ValueError(f"not a Cypher variable: {var!r}")
    return f"any(l IN labels({var}) WHERE " + " OR ".join(f"l STARTS WITH '{p}'" for p in PRIVATE_LABEL_PREFIXES) + ")"

DELETE_BATCH_ROWS = 1000

_VECTOR_DDL = re.compile(
    r"CREATE\s+VECTOR\s+INDEX\s+(?P<name>\w+)\b.*?"
    r"\(\s*(?P<var>\w+)\s*:\s*\w+\s*\)\s*ON\s*\(\s*(?P=var)\.embedding\s*\)\s*"
    r"WITH\s*\[(?P<props>[^\]]*)\]",
    re.IGNORECASE | re.DOTALL,
)


def split_statements(ddl: str) -> list[str]:
    """Split the DDL file on ';' (it carries no comments or string literals with one)."""
    return [s.strip() for s in ddl.split(";") if s.strip()]


def expected_vector_filters(ddl: str) -> dict[str, list[str]]:
    """Vector index name -> its declared filter properties, parsed from the DDL.

    The DDL is the single source of truth, so the staleness check can never
    drift from what ``schema.cypher`` creates.
    """
    filters: dict[str, list[str]] = {}
    for m in _VECTOR_DDL.finditer(ddl):
        props = [p.strip().split(".", 1)[-1] for p in m.group("props").split(",") if p.strip()]
        filters[m.group("name")] = props
    return filters


def find_stale_vector_indexes(existing: list[dict], expected: dict[str, list[str]]) -> dict[str, list[str]]:
    """Existing vector indexes that lack declared filter properties.

    ``existing`` are ``SHOW INDEXES`` rows (name, type, properties). A filtered
    index reports ``['embedding', *filter_properties]``. Returns
    ``{index name: [missing filter properties]}``.
    """
    stale: dict[str, list[str]] = {}
    for row in existing:
        want = expected.get(row["name"])
        if want is None or str(row["type"]).upper() != "VECTOR":
            continue
        missing = [p for p in want if p not in (row["properties"] or [])]
        if missing:
            stale[row["name"]] = missing
    return stale


def _existing_vector_indexes(session) -> list[dict]:
    result = session.run(
        "SHOW INDEXES YIELD name, type, properties WHERE type = 'VECTOR' "
        "RETURN name, type, properties"
    )
    return [dict(r) for r in result]


def _check_vector_indexes_current(session, expected: dict[str, list[str]]) -> None:
    stale = find_stale_vector_indexes(_existing_vector_indexes(session), expected)
    if not stale:
        return
    detail = "; ".join(f"{name} lacks filter properties {missing}" for name, missing in stale.items())
    raise RuntimeError(
        f"Existing vector index is out of date: {detail}. Filter properties cannot be added to "
        "an existing index and 'IF NOT EXISTS' would silently keep the old one. Rebuild the graph "
        "with `semigraph build-graph --rebuild` (drops and recreates the indexes; the data is "
        "reloaded from the data lake)."
    )


def apply_schema(driver: Driver) -> int:
    """Apply the packaged schema DDL statement-by-statement (ported from notebook 05).

    Returns the number of statements applied. Idempotent — re-running never
    errors or duplicates. Raises RuntimeError, before applying anything, when
    an existing vector index predates the filter properties.
    """
    ddl = read_schema_cypher()
    statements = split_statements(ddl)
    with driver.session() as session:
        _check_vector_indexes_current(session, expected_vector_filters(ddl))
        for stmt in statements:
            session.run(stmt)
    logger.info("schema applied (idempotent) — %d statements", len(statements))
    return len(statements)


def reset_graph(driver: Driver, *, keep_service_state: bool = True) -> dict[str, int]:
    """Empty the graph so a full rebuild starts clean.

    Drops :data:`REBUILD_INDEXES` first (cheaper than maintaining the vector
    indexes through millions of deletes, and required for the rebuild to
    recreate them with fresh filter properties), then deletes every node in
    batches of :data:`DELETE_BATCH_ROWS` — except private state (``Svc*`` service
    state and ``User*`` upload workspaces, :data:`PRIVATE_LABEL_PREFIXES`) when
    ``keep_service_state``. Constraints, range indexes and the workspace vector
    index are untouched. Uses auto-commit ``session.run`` because
    ``CALL {...} IN TRANSACTIONS`` cannot run inside an explicit transaction.
    Returns ``{"deleted_nodes": n}``.
    """
    node_filter = f"WHERE NOT {private_label_predicate('n')} " if keep_service_state else ""
    with driver.session() as session:
        for name in REBUILD_INDEXES:
            session.run(f"DROP INDEX {name} IF EXISTS")
        count = session.run(f"MATCH (n) {node_filter}RETURN count(n) AS n")
        deleted = next(iter(count), {"n": 0})["n"]
        session.run(f"MATCH (n) {node_filter}"
                    f"CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF {DELETE_BATCH_ROWS} ROWS")
    logger.info("graph reset: %d nodes deleted, %d indexes dropped (service state %s)",
                deleted, len(REBUILD_INDEXES), "kept" if keep_service_state else "deleted")
    return {"deleted_nodes": deleted}
