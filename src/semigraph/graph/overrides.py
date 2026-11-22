"""Curated, cited edge overrides (M2a R10): ``artifacts/edge_overrides.json``.

``reverse`` swaps source and target of EVERY loaded row with that edge key, from every filer, before the MERGE (the MERGE
accumulates evidence ids across filers, so swapping one chunk's row would leave the wrong edge in place). ``inactive``
marks the edge 'Historical' in a post-load pass (the MERGE hard-codes 'Active' on create). An override whose edge is
absent fails the build loudly. Pure apart from the one Cypher pass.
"""

import json
import re
from collections.abc import Mapping
from importlib.resources import files

from neo4j import Driver

STATUS_HISTORICAL = "Historical"  # same value as graph.temporal.STATUS_HISTORICAL (not imported: keeps this module leaf)
_ACTIONS = ("reverse", "inactive")
_REL_TYPE = re.compile(r"^[A-Z_]+$")

_INACTIVE_CYPHER = """MATCH (a:Company {{cik: $src}})-[r:{rel_type}]->(t:Company {{cik: $tgt}})
    SET r.status = $status RETURN count(r) AS n"""


class EdgeOverrideError(RuntimeError):
    """An override names an edge that is not in the graph being built."""


def load_edge_overrides() -> list[dict]:
    """The packaged overrides, validated."""
    entries = json.loads(files("semigraph.artifacts").joinpath("edge_overrides.json").read_text(encoding="utf-8"))
    for e in entries:
        missing = {"source", "type", "target", "action", "chunk_id", "reason"} - set(e)
        if missing or e["action"] not in _ACTIONS or not _REL_TYPE.match(e["type"]):
            raise ValueError(f"bad edge override {e!r} (missing {sorted(missing)}, actions {_ACTIONS})")
    return entries


def _ids(entry: Mapping, canonical: Mapping) -> tuple[int, int]:
    return canonical[entry["source"]]["entity_id"], canonical[entry["target"]]["entity_id"]


def apply_reverse_overrides(rel_rows: list[dict], overrides: list[dict], canonical: Mapping
                            ) -> tuple[list[dict], dict[int, int]]:
    """(rows with every ``reverse`` edge swapped, ``{override index: rows swapped}``). Input rows are not mutated."""
    keys = {(*_ids(e, canonical), e["type"]): i for i, e in enumerate(overrides) if e["action"] == "reverse"}
    hits = {i: 0 for i in keys.values()}
    out = []
    for row in rel_rows:
        i = keys.get((row["src"], row["tgt"], row["type"]))
        if i is None:
            out.append(row)
            continue
        hits[i] += 1
        out.append({**row, "src": row["tgt"], "tgt": row["src"]})
    return out, hits


def require_reverse_hits(overrides: list[dict], hits: Mapping[int, int]) -> None:
    """Fail loudly when a ``reverse`` override matched no loaded row."""
    absent = [overrides[i] for i, n in hits.items() if n == 0]
    if absent:
        raise EdgeOverrideError("edge override(s) matched no extracted relation: "
                                + "; ".join(f"{e['source']} {e['type']} {e['target']}" for e in absent))


def apply_inactive_overrides(driver: Driver, overrides: list[dict], canonical: Mapping) -> dict[str, int]:
    """Post-load pass: set status 'Historical' on each ``inactive`` edge. Raises when an edge is absent."""
    done = {}
    for e in overrides:
        if e["action"] != "inactive":
            continue
        src, tgt = _ids(e, canonical)
        with driver.session() as session:
            n = session.run(_INACTIVE_CYPHER.format(rel_type=e["type"]), src=src, tgt=tgt,
                            status=STATUS_HISTORICAL).single()["n"]
        label = f"{e['source']} {e['type']} {e['target']}"
        if n == 0:
            raise EdgeOverrideError(f"edge override absent from the graph: {label}")
        done[label] = n
    return done
