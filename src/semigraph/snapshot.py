"""Snapshot identity — a stable id for everything that defines the served corpus.

A snapshot id changes whenever the as-of date, the filing manifest, any
chunk or section-text file (they decide which filings count as parsed and so
which sections are corrected), any risk-item file, quality sidecar or risk-alignment table (what
changed between two annual filings is read from them), either checkpoint of recorded adjudication
verdicts (item level and passage layer) or the alignment provenance sidecar,
any extraction record, the Federal Register
snapshot, the curated XBRL metrics, the entity dictionary, the code that builds
the graph, or the code version changes. It is stamped on every graph node the loaders write
and is part of the answer-cache key, so a data refresh can never serve an
answer computed from older data.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

from . import __version__
from .config import Settings

_CHUNK = 1 << 20


def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _as_of_date(as_of: date | str | None) -> date | None:
    if as_of is None or isinstance(as_of, date):
        return as_of
    return date.fromisoformat(as_of)


_PACKAGE = Path(__file__).resolve().parent
# Everything that decides what the graph builder writes: a change here changes the graph.
_CODE_FILES = (
    *sorted((_PACKAGE / "graph").glob("*.py")),
    _PACKAGE / "versions.py", _PACKAGE / "hashing.py", _PACKAGE / "universe.py",
    _PACKAGE / "artifacts" / "schema.cypher",
)
_ENTITIES = _PACKAGE / "artifacts" / "canonical_entities.json"
# next to the alignment parquets: the two checkpoints (graph/adjudicate, graph/passage_adjudicate) and the provenance sidecar
_ALIGNMENT_SIDE_FILES = ("adjudications.jsonl", "passage_adjudications.jsonl", "alignment_provenance.json")


def code_fingerprint(paths=_CODE_FILES) -> str:
    """sha1 over the content of the graph-building code (missing files are skipped)."""
    digest = hashlib.sha1()
    for path in paths:
        path = Path(path)
        if path.exists():
            digest.update(f"{path.name}:{_file_sha1(path)}|".encode())
    return digest.hexdigest()


def _hashes(directory: Path, pattern: str) -> dict[str, str]:
    return {p.name: _file_sha1(p) for p in sorted(directory.glob(pattern))} if directory.exists() else {}


def snapshot_inputs(settings: Settings) -> dict:
    """Content hashes of every input that defines the corpus (None/empty when absent)."""
    manifest = settings.raw_dir / "edgar" / "manifest_universe.json"
    fr = settings.raw_dir / "federal_register_bis_rules.json"
    return {
        "manifest": _file_sha1(manifest) if manifest.exists() else None,
        "federal_register": _file_sha1(fr) if fr.exists() else None,
        "extractions": _hashes(settings.extractions_dir, "*_extractions.jsonl"),
        "xbrl_metrics": _hashes(settings.processed_dir / "xbrl", "*_key_metrics.parquet"),
        "chunks": _hashes(settings.chunks_dir, "*.parquet"),
        "section_texts": _hashes(settings.interim_dir / "section_texts", "*.parquet"),
        # the item parquets AND the quality sidecars (they decide which pairs are compared at all)
        "risk_items": {**_hashes(settings.interim_dir / "risk_items", "*.parquet"),
                       **_hashes(settings.interim_dir / "risk_items", "*_risk_items_quality.json")},
        # `align-items` output (pairs / decisions / passages), both checkpoints of recorded model verdicts (item level and passage
        # layer: every run replays them) and the provenance sidecar that says which answers the tables were built with
        "risk_alignment": {**_hashes(settings.interim_dir / "risk_alignment", "*.parquet"),
                           **{name: sha for pattern in _ALIGNMENT_SIDE_FILES
                              for name, sha in _hashes(settings.interim_dir / "risk_alignment", pattern).items()}},
        "entities": _file_sha1(_ENTITIES) if _ENTITIES.exists() else None,
        "code": code_fingerprint(),
    }


def compute_snapshot_id(settings: Settings, as_of: date | str | None = None, *,
                        code_version: str = __version__) -> str:
    """``snap-<YYYYMMDD>-<10 hex>``; the date part is the as-of date (or ``00000000``)."""
    as_of_d = _as_of_date(as_of)
    inputs = snapshot_inputs(settings)
    digest = hashlib.sha1()
    digest.update(f"{as_of_d}|{code_version}|{inputs['manifest']}|{inputs['federal_register']}"
                  f"|{inputs['entities']}|{inputs['code']}".encode())
    for section in ("extractions", "xbrl_metrics", "chunks", "section_texts", "risk_items", "risk_alignment"):
        for name, sha in sorted(inputs[section].items()):
            digest.update(f"|{section}:{name}:{sha}".encode())
    stamp = as_of_d.strftime("%Y%m%d") if as_of_d else "00000000"
    return f"snap-{stamp}-{digest.hexdigest()[:10]}"


def _dates(values) -> list[date]:
    out = []
    for v in values:
        try:
            out.append(date.fromisoformat(str(v)[:10]))
        except ValueError:
            continue
    return out


def newest_lake_date(settings: Settings) -> date | None:
    """The newest filing date in the manifest or publication date in the BIS rule
    cache; ``None`` for an empty lake. Used to validate a declared ``--as-of``."""
    dates: list[date] = []
    manifest = settings.raw_dir / "edgar" / "manifest_universe.json"
    if manifest.exists():
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        dates += _dates(r.get("filing_date") for filings in rows.values() for r in filings)
    fr = settings.raw_dir / "federal_register_bis_rules.json"
    if fr.exists():
        dates += _dates(r.get("publication_date") for r in json.loads(fr.read_text(encoding="utf-8")).get("results", []))
    return max(dates) if dates else None
