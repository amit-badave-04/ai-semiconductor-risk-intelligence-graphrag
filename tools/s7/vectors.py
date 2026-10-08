"""The pre-embedded question vectors of the S7 replay (stdlib only).

S7 measures the database, not the embedder, and the staging tools machine has no embedding model: every live ask hands its
question's vector to ``hybrid_retrieve(..., query_vec=...)``. The vectors are computed beforehand, on the developer's machine, with
the production embedder, for the 300 questions of ``tools/loadtest/pool.json`` and read from a JSON file shipped in the image.
A live ask is the pool question plus a salt (``replay.salted``): the salt only changes the answer-cache key, so the pool
question's vector stands for it.

The file records the sha256 of the pool it was built from; ``load`` refuses a file built for another pool, a wrong dimension and a
vector with a non-finite number, and ``check_covers`` refuses a pool question that has no vector.

The embedder is passed in (``encode_query(text) -> sequence of floats``); the command line that builds the real file is
``python -m tools.s7.replay vectors`` (it is the part that imports the service).
"""

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_DIM = 1024                         # Qwen3-Embedding-0.6B, the model the graph's vector indexes were built with
VERSION = 1


def _vector(values: Sequence[float], dim: int, where: str) -> list[float]:
    out = [float(v) for v in values]
    if len(out) != dim:
        raise ValueError(f"{where}: a vector of {len(out)} numbers, expected {dim}")
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"{where}: the vector holds a number that is not finite")
    return out


def build(pool: Mapping, out: Path, embedder, *, dim: int = DEFAULT_DIM) -> dict:
    """Embed every ``pool["live"]`` question with ``embedder.encode_query`` and write the file; returns its document.

    Raises ValueError for a vector of the wrong size or with a non-finite number (nothing is written then)."""
    items = list(pool["live"])
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("the pool holds a question id twice")
    vectors = {item["id"]: _vector(embedder.encode_query(item["q"]), dim, item["id"]) for item in items}
    doc = {"version": VERSION, "pool_sha256": pool.get("sha256"), "dim": dim, "count": len(vectors), "vectors": vectors}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    return doc


def load(path: Path, *, dim: int = DEFAULT_DIM, pool_sha256: str | None = None) -> dict[str, list[float]]:
    """``{question id: vector}``. ``pool_sha256``, when given, must be the one the file was built from."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("version") != VERSION:
        raise ValueError(f"unknown vectors file version {doc.get('version')!r}")
    if doc.get("dim") != dim:
        raise ValueError(f"the vectors file holds {doc.get('dim')!r}-dimensional vectors, expected {dim}")
    if pool_sha256 is not None and doc.get("pool_sha256") != pool_sha256:
        raise ValueError("the vectors file was built from another pool: rebuild it (python -m tools.s7.replay vectors)")
    raw = doc.get("vectors")
    if not isinstance(raw, dict) or doc.get("count") != len(raw):
        raise ValueError("the vectors file is incomplete: its count does not match its vectors")
    return {key: _vector(values, dim, key) for key, values in raw.items()}


def check_covers(vectors: Mapping[str, Sequence[float]], items: Sequence[Mapping]) -> None:
    """Raise ValueError naming (up to five of) the pool questions that have no vector."""
    missing = [item["id"] for item in items if item["id"] not in vectors]
    if missing:
        raise ValueError(f"{len(missing)} pool question(s) have no vector, e.g. {missing[:5]}")
