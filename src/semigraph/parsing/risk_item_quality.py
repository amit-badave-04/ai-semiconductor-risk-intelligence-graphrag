"""Per-filing quality of the risk-item detection, and the rule that decides whether two filings may be compared.

Item detection reports, per annual filing, its coverage of the section text and whether the section text itself looks
truncated or over-extended (``low_coverage``, ``section_suspect``). The item parquet carries only the item columns, so
these flags are kept in a small sidecar (``<TICKER>_risk_items_quality.json``) that alignment, the labelling tooling and
the graph loader all read. A pair with an untrustworthy side is NOT COMPARED: alignment would read text the parser lost as
removed risks, which is exactly the false-drop failure this layer exists to prevent. Coverage cannot vouch for filings
whose units tile the section by construction (paragraph units), which is why the suspect flag is a separate signal.
"""

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

QUALITY_SUFFIX = "_risk_items_quality.json"
_KEPT = ("accession_no", "form", "filing_date", "section_id", "n_items", "coverage", "method", "low_coverage",
         "section_suspect", "section_chars", "notes")


def quality_path_for(items_dir: Path, ticker: str) -> Path:
    return items_dir / f"{ticker}{QUALITY_SUFFIX}"


def write_quality(path: Path, ticker: str, filings: Sequence[Mapping]) -> None:
    """Write one ticker's per-filing quality records (atomically replacing any earlier file)."""
    payload = {"ticker": ticker, "filings": [{k: f.get(k) for k in _KEPT} for f in filings]}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_quality(items_dir: Path) -> dict[str, dict]:
    """accession_no -> its quality record (with the ticker), over every ticker's sidecar in ``items_dir``."""
    out: dict[str, dict] = {}
    for path in sorted(items_dir.glob(f"*{QUALITY_SUFFIX}")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for f in data["filings"]:
            out[f["accession_no"]] = {**f, "ticker": data["ticker"]}
    return out


def _side_problem(side: str, record: Mapping | None) -> str | None:
    if record is None:
        return f"{side} filing has no quality record"
    if record.get("low_coverage"):
        return f"{side} filing's risk items cover only {100 * float(record.get('coverage') or 0):.1f}% of its risk section text"
    if record.get("section_suspect"):
        return f"{side} filing's risk section text looks truncated or over-extended (length far from its neighbour's)"
    return None


def comparability(older_accession: str, newer_accession: str, quality: Mapping[str, Mapping]) -> tuple[bool, str | None]:
    """``(True, None)`` when both filings' items can be trusted; else ``(False, reason)`` naming the side and why."""
    for side, accession in (("older", older_accession), ("newer", newer_accession)):
        problem = _side_problem(side, quality.get(accession))
        if problem:
            return False, problem
    return True, None
