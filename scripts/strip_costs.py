"""Strip spend from an eval report before it is committed (the owner keeps spend private).

Removes every key whose name contains ``cost`` or ``usd`` (any depth) and masks dollar figures inside strings (a gate's
``detail`` such as "$0.0158 per question vs ..."). The removed values are written to a git-ignored sidecar under
``data/processed/costs/``, keyed by JSON path, so nothing is lost locally.

Kept on purpose: gate thresholds and limits (any key containing limit / threshold / tolerance / ratio, or anything under one),
and gate NAMES under ``gates`` (``blended_cost`` stays; its ``detail`` loses the dollar figures but keeps "ratio 1.13, limit 2").

Run it on every report written by ``eval-agent`` (agent_eval.py is frozen and does not strip) BEFORE committing it:

    uv run python scripts/strip_costs.py artifacts/eval_report.agent.json

The script is idempotent: a stripped report is unchanged and an existing sidecar is merged, never clobbered.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIDECAR_DIR = ROOT / "data" / "processed" / "costs"
COST_MARKERS = ("cost", "usd")
KEEP_MARKERS = ("limit", "threshold", "tolerance", "ratio")
GATES_KEY = "gates"
DOLLARS = re.compile(r"\$\d[\d,]*(?:\.\d+)?")
MASK = "$[stripped]"


def _is_cost_key(key: str, ancestors: tuple[str, ...]) -> bool:
    low = key.lower()
    if not any(m in low for m in COST_MARKERS):
        return False
    if ancestors and ancestors[-1] == GATES_KEY:                     # a gate name, not a spend field
        return False
    return not any(m in k.lower() for k in (*ancestors, key) for m in KEEP_MARKERS)


def _walk(node, path: str, ancestors: tuple[str, ...], removed: dict):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            child = f"{path}/{key}"
            if _is_cost_key(key, ancestors):
                removed[child] = value
            else:
                out[key] = _walk(value, child, (*ancestors, key), removed)
        return out
    if isinstance(node, list):
        return [_walk(v, f"{path}/{i}", ancestors, removed) for i, v in enumerate(node)]
    if isinstance(node, str) and DOLLARS.search(node):
        removed[path] = node
        return DOLLARS.sub(MASK, node)
    return node


def strip_report(report: dict) -> tuple[dict, dict]:
    """(report without spend, {json path: removed value}). The input is not modified."""
    removed: dict = {}
    return _walk(copy.deepcopy(report), "", (), removed), removed


def strip_file(path: Path, sidecar: Path) -> dict:
    """Strip ``path`` in place; merge the removed values into ``sidecar``. Returns the removed values (empty when already stripped)."""
    report = json.loads(path.read_text(encoding="utf-8"))
    stripped, removed = strip_report(report)
    if removed:
        write_sidecar(sidecar, path.name, removed)
        path.write_text(json.dumps(stripped, indent=2) + "\n", encoding="utf-8", newline="\n")
    return removed


def write_sidecar(sidecar: Path, source: str, removed: dict) -> None:
    prior = json.loads(sidecar.read_text(encoding="utf-8"))["removed"] if sidecar.exists() else {}
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({"source": source, "removed": {**prior, **removed}}, indent=2) + "\n", encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("reports", nargs="+", type=Path, help="eval report JSON file(s), stripped in place")
    p.add_argument("--sidecar-dir", type=Path, default=DEFAULT_SIDECAR_DIR, help="git-ignored directory for the removed values")
    args = p.parse_args(argv)
    for path in args.reports:
        removed = strip_file(path, args.sidecar_dir / f"{path.stem}.costs.json")
        print(f"{path}: removed {len(removed)} cost field(s)" if removed else f"{path}: nothing to strip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
