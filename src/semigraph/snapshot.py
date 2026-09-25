"""Snapshot identity — a stable id for everything that defines the served corpus.

A snapshot id changes whenever the as-of date, the filing manifest, any
extraction record, the Federal Register snapshot, the curated XBRL metrics or
the code version changes. It is stamped on every graph node the loaders write
and is part of the answer-cache key, so a data refresh can never serve an
answer computed from older data.
"""

import hashlib
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


def snapshot_inputs(settings: Settings) -> dict:
    """Content hashes of every input that defines the corpus (None/empty when absent)."""
    manifest = settings.raw_dir / "edgar" / "manifest_universe.json"
    fr = settings.raw_dir / "federal_register_bis_rules.json"
    extractions = (sorted(settings.extractions_dir.glob("*_extractions.jsonl"))
                   if settings.extractions_dir.exists() else [])
    xbrl_dir = settings.processed_dir / "xbrl"
    metrics = sorted(xbrl_dir.glob("*_key_metrics.parquet")) if xbrl_dir.exists() else []
    return {
        "manifest": _file_sha1(manifest) if manifest.exists() else None,
        "federal_register": _file_sha1(fr) if fr.exists() else None,
        "extractions": {p.name: _file_sha1(p) for p in extractions},
        "xbrl_metrics": {p.name: _file_sha1(p) for p in metrics},
    }


def compute_snapshot_id(settings: Settings, as_of: date | str | None = None, *,
                        code_version: str = __version__) -> str:
    """``snap-<YYYYMMDD>-<10 hex>``; the date part is the as-of date (or ``00000000``)."""
    as_of_d = _as_of_date(as_of)
    inputs = snapshot_inputs(settings)
    digest = hashlib.sha1()
    digest.update(f"{as_of_d}|{code_version}|{inputs['manifest']}|{inputs['federal_register']}".encode())
    for section in ("extractions", "xbrl_metrics"):
        for name, sha in sorted(inputs[section].items()):
            digest.update(f"|{section}:{name}:{sha}".encode())
    stamp = as_of_d.strftime("%Y%m%d") if as_of_d else "00000000"
    return f"snap-{stamp}-{digest.hexdigest()[:10]}"
