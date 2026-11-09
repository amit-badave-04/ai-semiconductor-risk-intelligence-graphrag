"""Check that every legacy UI test is classified in docs/v2/M5B_TEST_CLASSIFICATION.md (spec R29, promotion gate 1).

    python scripts/check_test_map.py                      # collect live (needs node on PATH), read the default document
    python scripts/check_test_map.py --doc <file> [--node-ids <file>] [--pytest-ids <file>]

The live collection is counted here, never read from prose:
  * node: ``node --test --test-reporter=tap <file>`` for each ``tests/ui/*.test.mjs`` (node has no list-only mode, so the
    suite runs; a failing test is still collected), top-level results only;
  * pytest: ``pytest --collect-only -q`` over the legacy UI Python tests (parametrized cases fold into their function).

Each live test needs a row in a table of the document, under a heading that names its file in backticks. A node row starts
``N. <title>`` (a ``<key>`` placeholder in the title covers every case of a loop); a pytest row starts ``N. `test_name```
(or the full ``path::test_name`` for a test of another file). Row classes: PORT, REWRITE, KEEP (a test of something that is
not the legacy page and stays as it is), OBSOLETE (needs a written reason in the last column; never on a [SEC] row), UNCLEAR.

Exit 0: all mapped, none UNCLEAR. Exit 1: an unmapped test, an UNCLEAR row, a bad class, or a bad OBSOLETE row.
Exit 2: the collection could not be made or came back empty. A row whose test no longer exists is reported as STALE only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = ROOT / "docs" / "v2" / "M5B_TEST_CLASSIFICATION.md"
NODE_GLOB = "tests/ui/*.test.mjs"
LEGACY_PYTEST = (
    "tests/test_static_ui.py",
    "tests/test_serve_agent_ui.py",
    "tests/test_serve_api.py::test_index_sets_security_headers",
)
CLASSES = ("PORT", "REWRITE", "KEEP", "OBSOLETE", "UNCLEAR")

Row = dict  # keys: file, kind ("node" | "py"), key, label, cls, reason, sec, pattern


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()


def parse_tap(tap: str) -> list[str]:
    """Titles of the top-level ``ok`` / ``not ok`` lines (nested subtests are indented and skipped)."""
    titles = []
    for line in tap.splitlines():
        m = re.match(r"^(?:not )?ok \d+ - (.*)$", line)
        if m:
            title = re.sub(r" # (?:SKIP|TODO)\b.*$", "", m.group(1))
            titles.append(title.replace("\\#", "#"))
    return titles


def parse_pytest_ids(out: str) -> list[tuple[str, str]]:
    """(file, function) for each ``path::...::name[params]`` line of ``--collect-only -q`` output."""
    ids = []
    for line in out.splitlines():
        m = re.match(r"^(\S+?\.py)::(?:\w+::)*(\w+)(?:\[.*\])?\s*$", line.strip())
        if m:
            ids.append((m.group(1), m.group(2)))
    return ids


def _row_pattern(title: str) -> re.Pattern | None:
    """A row title with a ``<placeholder>`` stands for every case of a loop; a trailing ``(N cases ...)`` is prose."""
    if not re.search(r"<[^>]+>", title):
        return None
    body = re.sub(r"\s*\(\d+ cases?\b[^)]*\)\s*$", "", title)
    parts = [re.escape(p) for p in re.split(r"<[^>]+>", norm(body))]
    return re.compile("^" + ".+".join(parts) + "$")


def parse_doc(text: str) -> list[Row]:
    rows: list[Row] = []
    section = None
    for line in text.splitlines():
        head = re.match(r"^#{2,4} .*", line)
        if head:
            m = re.search(r"`([^`]+\.(?:mjs|py))`", line)
            section = m.group(1) if m else None
            continue
        if section is None or not re.match(r"^\| \d+\. ", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 5:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
        first = re.sub(r"^\d+\. ", "", cells[0])
        cls_cell = cells[2].strip().strip("*`").split()[0] if cells[2].strip() else ""
        base = {"cls": cls_cell, "reason": cells[4] if len(cells) > 4 else "", "sec": "[SEC]" in cells[1],
                "label": first, "file": section}
        if section.endswith(".mjs"):
            rows.append({**base, "kind": "node", "key": norm(first), "pattern": _row_pattern(first)})
        else:
            m = re.match(r"`(?:([^`:]+\.py)::)?(\w+)", first)
            if m:
                rows.append({**base, "kind": "py", "file": m.group(1) or section, "key": m.group(2), "pattern": None})
    return rows


def _match(row: Row, kind: str, file: str, key: str) -> bool:
    if row["kind"] != kind or row["file"] != file:
        return False
    if row["pattern"] is not None:
        return bool(row["pattern"].match(key))
    return row["key"] == key


def check(rows: list[Row], node_ids: list[tuple[str, str]], py_ids: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(problems, stale warnings)."""
    problems, used = [], set()
    live = [("node", f, norm(t), t) for f, t in node_ids] + [("py", f, fn, fn) for f, fn in dict.fromkeys(py_ids)]
    for kind, file, key, shown in live:
        hits = [i for i, r in enumerate(rows) if _match(r, kind, file, key)]
        if not hits:
            problems.append(f"UNMAPPED  {file}: {shown}")
        used.update(hits)
    for r in rows:
        where = f"{r['file']}: {re.sub(chr(96), '', r['label'])}"
        if r["cls"] not in CLASSES:
            problems.append(f"BAD CLASS  {where} (class '{r['cls']}')")
        elif r["cls"] == "UNCLEAR":
            problems.append(f"UNCLEAR  {where}")
        elif r["cls"] == "OBSOLETE":
            if not r["reason"].strip():
                problems.append(f"OBSOLETE WITHOUT REASON  {where}")
            if r["sec"]:
                problems.append(f"SEC ROW OBSOLETE  {where}")
    stale = [f"STALE  {r['file']}: {re.sub(chr(96), '', r['label'])}" for i, r in enumerate(rows) if i not in used]
    return problems, stale


def collect_node(root: Path) -> list[tuple[str, str]]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is not on PATH; pass --node-ids or install Node 24")
    ids = []
    for f in sorted(root.glob(NODE_GLOB)):
        rel = f.relative_to(root).as_posix()
        proc = subprocess.run([node, "--test", "--test-reporter=tap", rel], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        ids += [(rel, t) for t in parse_tap(proc.stdout)]
    return ids


def collect_pytest(root: Path) -> list[tuple[str, str]]:
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *LEGACY_PYTEST],
                          cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    return parse_pytest_ids(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    ap.add_argument("--node-ids", type=Path, help="file of '<path>TAB<title>' lines instead of running node")
    ap.add_argument("--pytest-ids", type=Path, help="file of pytest --collect-only -q output instead of running pytest")
    args = ap.parse_args(argv)
    try:
        rows = parse_doc(args.doc.read_text(encoding="utf-8"))
        if args.node_ids:
            node_ids = [tuple(ln.split("\t", 1)) for ln in args.node_ids.read_text(encoding="utf-8").splitlines() if "\t" in ln]
        else:
            node_ids = collect_node(ROOT)
        py_ids = (parse_pytest_ids(args.pytest_ids.read_text(encoding="utf-8")) if args.pytest_ids else collect_pytest(ROOT))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not node_ids or not py_ids:
        print(f"error: collected nothing ({len(node_ids)} node, {len(py_ids)} pytest ids)", file=sys.stderr)
        return 2
    problems, stale = check(rows, node_ids, py_ids)
    for line in problems + stale:
        print(line)
    classes = Counter(r["cls"] for r in rows)
    print(f"{len(node_ids)} node tests + {len(py_ids)} pytest items ({len(set(py_ids))} functions) live; "
          f"{len(rows)} rows: " + ", ".join(f"{c} {classes.get(c, 0)}" for c in CLASSES))
    if problems:
        print(f"FAIL: {len(problems)} problem(s), {len(stale)} stale row(s)")
        return 1
    print(f"OK: every live test is mapped, none UNCLEAR ({len(stale)} stale row(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
