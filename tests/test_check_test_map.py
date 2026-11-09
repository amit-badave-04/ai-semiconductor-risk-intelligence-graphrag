"""scripts/check_test_map.py: the M5b test-classification checker (spec R29, promotion gate 1).

The checker reads docs/v2/M5B_TEST_CLASSIFICATION.md and the live collection of the legacy UI tests, and exits 1 when a live
test has no row, a row is UNCLEAR (or has no recognised class), or an OBSOLETE row has no written reason or is a [SEC] row.
Collection is injected here (``--node-ids`` and ``--pytest-ids`` files), so these tests run neither node nor pytest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_test_map.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_test_map", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ctm():
    return _load()


HEADER = "| test | contract | class | target | reason |\n|---|---|---|---|---|\n"


def doc(node_rows: str = "", py_rows: str = "") -> str:
    return (
        "# Classification\n\n"
        "### 3.1 `tests/ui/a.test.mjs` (2 tests, 2 rows)\n\n" + HEADER + node_rows + "\n"
        "### 4.1 `tests/test_x.py` (2 items, 1 row)\n\n" + HEADER + py_rows + "\n"
        "## 5. Something else\n\n| 1. not a test row | x | y | z | w |\n"
    )


NODE_OK = (
    "| 1. first thing works | c | PORT | unit | r |\n"
    "| 2. the `#answer` chip opens | c | REWRITE | E2E | r |\n"
)
PY_OK = "| 1. `test_param[...]` (2 cases) | c | KEEP | pytest | r |\n"
NODE_IDS = ["tests/ui/a.test.mjs\tfirst thing works", "tests/ui/a.test.mjs\tthe #answer chip opens"]
PY_IDS = ["tests/test_x.py::test_param[one]", "tests/test_x.py::test_param[two]"]


def run(ctm, tmp_path, text, node_ids=NODE_IDS, py_ids=PY_IDS):
    d = tmp_path / "doc.md"
    d.write_text(text, encoding="utf-8")
    n = tmp_path / "node.txt"
    n.write_text("\n".join(node_ids) + "\n", encoding="utf-8")
    p = tmp_path / "py.txt"
    p.write_text("\n".join(py_ids) + "\n", encoding="utf-8")
    return ctm.main(["--doc", str(d), "--node-ids", str(n), "--pytest-ids", str(p)])


def test_exits_zero_when_every_live_test_is_mapped(ctm, tmp_path, capsys):
    code = run(ctm, tmp_path, doc(NODE_OK, PY_OK))
    out = capsys.readouterr().out
    assert code == 0
    assert "OK" in out and "3 live" not in out    # counts come from the collection: 2 node + 2 pytest items
    assert "2 node" in out and "2 pytest" in out


def test_exits_one_and_names_a_live_test_with_no_row(ctm, tmp_path, capsys):
    ids = NODE_IDS + ["tests/ui/a.test.mjs\ta brand new test"]
    code = run(ctm, tmp_path, doc(NODE_OK, PY_OK), node_ids=ids)
    out = capsys.readouterr().out
    assert code == 1
    assert "UNMAPPED" in out and "a brand new test" in out and "tests/ui/a.test.mjs" in out


def test_exits_one_and_names_an_unmapped_pytest_function(ctm, tmp_path, capsys):
    code = run(ctm, tmp_path, doc(NODE_OK, PY_OK), py_ids=PY_IDS + ["tests/test_x.py::test_other"])
    out = capsys.readouterr().out
    assert code == 1 and "UNMAPPED" in out and "test_other" in out


def test_exits_one_on_an_unclear_row(ctm, tmp_path, capsys):
    unclear = NODE_OK.replace("| REWRITE |", "| UNCLEAR |")
    code = run(ctm, tmp_path, doc(unclear, PY_OK))
    out = capsys.readouterr().out
    assert code == 1 and "UNCLEAR" in out and "the `#answer` chip opens".replace("`", "") in out


def test_exits_one_on_an_unrecognised_class(ctm, tmp_path, capsys):
    code = run(ctm, tmp_path, doc(NODE_OK.replace("| PORT |", "| MAYBE |"), PY_OK))
    assert code == 1 and "BAD CLASS" in capsys.readouterr().out


def test_obsolete_needs_a_written_reason(ctm, tmp_path, capsys):
    rows = NODE_OK.replace("| PORT | unit | r |", "| OBSOLETE | none |  |")
    code = run(ctm, tmp_path, doc(rows, PY_OK))
    assert code == 1 and "OBSOLETE WITHOUT REASON" in capsys.readouterr().out


def test_obsolete_with_a_reason_passes_but_a_sec_row_is_never_obsolete(ctm, tmp_path, capsys):
    ok = NODE_OK.replace("| PORT | unit | r |", "| OBSOLETE | none | dropped by decision 9 |")
    code = run(ctm, tmp_path, doc(ok, PY_OK))
    assert code == 0
    capsys.readouterr()
    sec = ok.replace("| c |", "| [SEC] c |", 1)
    code = run(ctm, tmp_path, doc(sec, PY_OK))
    assert code == 1 and "SEC ROW OBSOLETE" in capsys.readouterr().out


def test_one_row_with_a_key_placeholder_covers_every_case_of_a_loop(ctm, tmp_path, capsys):
    rows = "| 1. `<key>` on the zone does nothing (3 cases: tab, escape, a letter) | c | REWRITE | RTL | r |\n"
    ids = [f"tests/ui/a.test.mjs\t{k} on the zone does nothing" for k in ("Tab", "Escape", "a letter")]
    code = run(ctm, tmp_path, doc(rows, PY_OK), node_ids=ids)
    assert code == 0, capsys.readouterr().out


def test_titles_match_regardless_of_backticks_case_and_spacing(ctm, tmp_path):
    rows = "| 1. The  `Foo` thing | c | PORT | unit | r |\n"
    code = run(ctm, tmp_path, doc(rows, PY_OK), node_ids=["tests/ui/a.test.mjs\tthe foo thing"])
    assert code == 0


def test_a_row_with_no_live_test_is_reported_as_stale_but_does_not_fail(ctm, tmp_path, capsys):
    rows = NODE_OK + "| 3. a test that was deleted | c | PORT | unit | r |\n"
    code = run(ctm, tmp_path, doc(rows, PY_OK))
    assert code == 0 and "STALE" in capsys.readouterr().out


def test_a_test_mapped_only_under_another_file_is_unmapped(ctm, tmp_path, capsys):
    ids = ["tests/ui/b.test.mjs\tfirst thing works"] + NODE_IDS[1:]
    code = run(ctm, tmp_path, doc(NODE_OK, PY_OK), node_ids=ids)
    assert code == 1 and "tests/ui/b.test.mjs" in capsys.readouterr().out


def test_an_empty_collection_is_an_error_not_a_pass(ctm, tmp_path, capsys):
    code = run(ctm, tmp_path, doc(NODE_OK, PY_OK), node_ids=[], py_ids=[])
    assert code == 2 and "collected nothing" in capsys.readouterr().err


def test_a_full_pytest_id_in_a_row_maps_a_test_from_another_file(ctm, tmp_path):
    py_rows = PY_OK + "| 2. `tests/test_other.py::test_adjacent` | c | REWRITE | pytest | r |\n"
    code = run(ctm, tmp_path, doc(NODE_OK, py_rows), py_ids=PY_IDS + ["tests/test_other.py::test_adjacent"])
    assert code == 0


def test_parse_tap_takes_top_level_results_only_and_unescapes(ctm):
    tap = (
        "TAP version 13\n"
        "# Subtest: a\nok 1 - plain title\n  ---\n  duration_ms: 1\n  ...\n"
        "# Subtest: b\n    ok 1 - nested subtest\nok 2 - title with \\#hash # SKIP not on windows\n"
        "not ok 3 - a failing one\n1..3\n# tests 3\n"
    )
    assert ctm.parse_tap(tap) == ["plain title", "title with #hash", "a failing one"]


def test_parse_pytest_collection_keeps_node_ids_only(ctm):
    out = (
        "tests/test_a.py::test_one\n"
        "tests/test_a.py::test_p[x-y]\n"
        "tests/test_a.py::TestC::test_m\n"
        "\n49 tests collected in 9.04s\n"
        "warnings summary ... tests/test_a.py:3 not an id\n"
    )
    assert ctm.parse_pytest_ids(out) == [
        ("tests/test_a.py", "test_one"), ("tests/test_a.py", "test_p"), ("tests/test_a.py", "test_m"),
    ]
