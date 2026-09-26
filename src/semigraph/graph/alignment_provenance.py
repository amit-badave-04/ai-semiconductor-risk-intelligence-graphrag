"""Provenance of the ``align-items`` tables: which recorded model answers they were built with.

The tables (``<T>_pairs / _decisions / _passages.parquet``) are a function of the risk items, the lake and the two checkpoints of
recorded model answers (``adjudications.jsonl`` for the item level, ``passage_adjudications.jsonl`` for the passage layer). A
checkpoint grows while a paid run is in progress, so tables built earlier silently lack the newer answers (aligner-only "Removed"
items, band sentences that stay ``reworded``) and a graph built from them regresses. To make that visible, every run that writes
tables also writes ``alignment_provenance.json`` next to them: per ticker the flags of the run, the adjudication model, the prompt
versions replayed, whether the below zone was on, how many verdicts were applied, the sha256 (and record count) of each checkpoint
file exactly as it was read, and the sha256 of every input file. It carries no timestamp and no absolute path, so a run with the same
inputs writes the same bytes (the ``flags`` say what the run was permitted to BUY, so two runs that bought differently differ there
and only there). The sidecar is written after the tables (a crash between the two leaves an OLD sidecar, which is the safe side) and
merged per ticker, so a ``--ticker`` run leaves the other tickers' entries alone.

``stale_checkpoints`` is what ``items.require_alignment`` (and so ``build-graph``) asks: for each ticker, which checkpoint files
differ now from what its tables were built with. A missing sidecar counts as "built with nothing", which is fine only while no
checkpoint file exists on disk.
"""

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import adjudicate as adj
from . import passage_adjudicate as pad

PROVENANCE_NAME = "alignment_provenance.json"
SCHEMA_VERSION = 1
CHECKPOINT_NAMES = (adj.CHECKPOINT_NAME, pad.CHECKPOINT_NAME)
_BLOCK = 1 << 20


def file_sha256(path: Path) -> str | None:
    """sha256 of a file's bytes, None when it does not exist."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_digest(checkpoint: adj.Checkpoint) -> dict[str, Any] | None:
    """``{"sha256", "records"}`` of the bytes ``checkpoint`` was read from (None: there was no file when it was read, even if one has
    appeared since). A checkpoint that was appended to after it was read no longer describes one file state: read a fresh one."""
    if checkpoint.stale:
        raise ValueError(f"{checkpoint.path.name} changed after it was read: read it again before recording its digest")
    if checkpoint.sha256 is None:
        return None
    return {"sha256": checkpoint.sha256, "records": len(checkpoint.records)}


def ticker_entry(*, flags: Mapping[str, bool], model: str, item_checkpoint: adj.Checkpoint, passage_checkpoint: adj.Checkpoint,
                 passage_version: str | None, below_zone: bool, item_verdicts: int, passage_verdicts: int,
                 inputs: Mapping[str, Path]) -> dict[str, Any]:
    """The provenance record of one ticker's tables (see the module doc)."""
    return {
        "flags": dict(flags),
        "model": model,
        "item_adjudication": {"prompt_version": adj.PROMPT_VERSION, "verdicts_applied": item_verdicts},
        "passage_adjudication": {"prompt_version": passage_version, "below_zone": below_zone, "verdicts_applied": passage_verdicts},
        "checkpoints": {adj.CHECKPOINT_NAME: checkpoint_digest(item_checkpoint),
                        pad.CHECKPOINT_NAME: checkpoint_digest(passage_checkpoint)},
        "inputs": {name: file_sha256(path) for name, path in sorted(inputs.items())},
    }


def read_provenance(directory: Path) -> dict[str, dict]:
    """The per-ticker entries of the sidecar in ``directory`` ({} when it is missing, unreadable or of another schema)."""
    path = directory / PROVENANCE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION or not isinstance(data.get("tickers"), dict):
        return {}
    return {t: e for t, e in data["tickers"].items() if isinstance(e, dict)}


def write_provenance(directory: Path, entries: Mapping[str, dict]) -> Path:
    """Merge ``entries`` (ticker -> record) into the sidecar of ``directory`` and write it atomically (sorted keys, no timestamp)."""
    merged = {**read_provenance(directory), **entries}
    path = directory / PROVENANCE_NAME
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"schema": SCHEMA_VERSION, "tickers": merged}, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                       newline="\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def stale_checkpoints(directory: Path, tickers: Sequence[str]) -> dict[str, list[str]]:
    """``{ticker: [checkpoint files]}`` for every ticker whose tables were built with a different state of a checkpoint file than the
    one now on disk (more or fewer answers, or no provenance at all while the file exists). Empty when every ticker is up to date."""
    recorded = read_provenance(directory)
    now = {name: file_sha256(directory / name) for name in CHECKPOINT_NAMES}
    stale: dict[str, list[str]] = {}
    for ticker in tickers:
        built = (recorded.get(ticker) or {}).get("checkpoints") or {}
        changed = [name for name, sha in now.items() if sha != (built.get(name) or {}).get("sha256")]
        if changed:
            stale[ticker] = changed
    return stale
