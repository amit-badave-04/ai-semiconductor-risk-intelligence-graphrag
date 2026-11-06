"""scripts/export_ids.py: the generated client constants of M5b (docs/specs/m5b-frontend.md R5, I1 part 2).

The generator writes ``web/src/lib/ids.generated.ts`` (the citation-id grammar of ``retrieval/ids.py``) and
``web/src/lib/pages.generated.ts`` (``ui_v2.SPA_PAGES`` and the 13 ``universe.FILERS``) so the browser and the server cannot
disagree. These tests pin four things:

* the translation of a Python pattern to a JavaScript one (``\\Z`` becomes ``$``; anything the two engines read differently is
  refused instead of copied);
* what the generator emits, and that the committed files equal it (a stale file is red), in BOTH directions: a tampered file
  is reported, and a changed source (``FILERS``, ``SPA_PAGES``, an id pattern) makes the committed file stale;
* that a checkout which converted line endings (``core.autocrlf``) is not reported stale;
* the generated patterns under V8 against ``tests/data/citation_corpus.json``, whose expectations are hand-written: Python and
  JavaScript must each equal the label, so a mistake the two engines share cannot pass.

The JavaScript leg here reads the GENERATED CONSTANTS only (``tests/data/citation_corpus_eval.mjs``). R5's other cross-engine
leg, the shipped TypeScript tokenizer and classifier against the same corpus, cannot exist before the Ask page does (I3).
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from semigraph import universe
from semigraph.retrieval import ids
from semigraph.serve import ui_v2

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests" / "data" / "citation_corpus.json"
EVAL_HELPER = ROOT / "tests" / "data" / "citation_corpus_eval.mjs"
IDS_FILE = "web/src/lib/ids.generated.ts"
PAGES_FILE = "web/src/lib/pages.generated.ts"
KINDS = ("chunk", "xbrl", "fr", "doc")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export_ids = _load("export_ids", ROOT / "scripts" / "export_ids.py")


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


@pytest.fixture
def written(tmp_path) -> Path:
    """A tmp repo root holding freshly generated outputs."""
    export_ids.write(tmp_path)
    return tmp_path


# ---------------------------------------------------------------- reading the generated TypeScript back (Python side)

_STRING_CONST = re.compile(r'^export const ([A-Z_]+) = ("(?:[^"\\]|\\.)*");$', re.MULTILINE)


def _strings(text: str) -> dict[str, str]:
    """Every ``export const NAME = "literal";`` of a generated file, the literal decoded."""
    return {name: json.loads(literal) for name, literal in _STRING_CONST.findall(text)}


def _spa_pages(text: str) -> list[str]:
    block = re.search(r"export const SPA_PAGES = \[\n(.*?)\n\] as const;", text, re.DOTALL)
    assert block, "SPA_PAGES array not found"
    return [json.loads(line.strip().rstrip(",")) for line in block.group(1).splitlines()]


def _filers(text: str) -> list[tuple[str, str]]:
    pairs = re.findall(r'^  \{ ticker: ("[^"]*"), name: ("[^"]*") \},?$', text, re.MULTILINE)
    return [(json.loads(ticker), json.loads(name)) for ticker, name in pairs]


# ---------------------------------------------------------------- the translation

def test_translate_turns_the_trailing_python_end_anchor_into_a_js_dollar():
    assert export_ids.translate_python_regex(r"^abc[0-9]{4}\Z") == "^abc[0-9]{4}$"


def test_translate_leaves_a_pattern_both_engines_read_alike_unchanged():
    for pattern in (ids.CHUNK_ID_PATTERN, ids.XBRL_ID_PATTERN, ids.FR_ID_PATTERN, ids.DOC_ID_PATTERN, ids.CITE_RE.pattern):
        assert export_ids.translate_python_regex(pattern) == pattern


@pytest.mark.parametrize("pattern, why", [
    (r"abc\Zdef", r"\Z in the middle"),
    (r"[a\Z]", r"\Z inside a class"),
    (r"a\Z\Z", r"two \Z"),
    (r"\Aabc", r"\A does not exist in JavaScript"),
    (r"abc$", r"a Python $ accepts a trailing newline, a JS $ does not"),
    (r"a\d", r"\d is unicode-aware in Python and ASCII in JavaScript"),
    (r"\w+", r"\w differs between the engines"),
    (r"a\s", r"\s differs between the engines"),
    (r"\bfoo", r"\b differs between the engines"),
    (r"(?P<name>a)", "named groups are written differently"),
    (r"(?i)abc", "inline flags"),
    (r"(?#note)abc", "comment groups"),
    (r"(?>a+)b", "atomic groups"),
    (r"(?<=a)b", "lookbehind"),
    (r"a++", "possessive quantifiers"),
    (r"a{2,3}+", "possessive counted quantifiers"),
    (r"a{x}", "a brace that is no quantifier"),
    (r"[]a]", "a closing bracket first in a class is a literal only in Python"),
    (r"[[a]", "a nested opening bracket in a class"),
    (r"(abc", "unclosed group"),
    (r"abc)", "unbalanced group"),
    (r"[abc", "unclosed class"),
    ("abc\\", "trailing backslash"),
    ("caf" + chr(0xE9), "non-ASCII character"),
    (r"\1(a)", "back-reference"),
    (r"a.b", "a Python . matches \\r and U+2028 and U+2029, a JS . matches none of them"),
    (r"[a(b]", "an unescaped ( in a class is a syntax error under the v flag"),
    (r"[a)b]", "an unescaped ) in a class is a syntax error under the v flag"),
    (r"[a{b]", "an unescaped { in a class is a syntax error under the v flag"),
    (r"[a}b]", "an unescaped } in a class is a syntax error under the v flag"),
    (r"[a/b]", "an unescaped / in a class is a syntax error under the v flag"),
    (r"[a|b]", "an unescaped | in a class is a syntax error under the v flag"),
    (r"[a&&b]", "a doubled & in a class is a set operation under the v flag"),
    (r"[a--b]", "a doubled - in a class is a set operation under the v flag"),
])
def test_translate_refuses_syntax_the_two_engines_read_differently(pattern, why):
    with pytest.raises(ValueError):
        export_ids.translate_python_regex(pattern)


@pytest.mark.parametrize("flags", [re.IGNORECASE, re.MULTILINE, re.DOTALL, re.VERBOSE, re.ASCII])
def test_a_compiled_pattern_with_flags_is_refused(flags):
    with pytest.raises(ValueError):
        export_ids.js_pattern(re.compile(r"^a\Z", flags), anchored=True)


def test_an_anchored_form_that_is_not_anchored_at_both_ends_is_refused():
    with pytest.raises(ValueError):
        export_ids.js_pattern(re.compile(r"abc\Z"), anchored=True)
    with pytest.raises(ValueError):
        export_ids.js_pattern(re.compile(r"^abc"), anchored=True)


# ---------------------------------------------------------------- what is emitted

def test_generate_returns_exactly_the_two_files():
    assert set(export_ids.generate()) == {IDS_FILE, PAGES_FILE}


def test_the_ids_file_holds_the_four_patterns_and_the_scan_pattern_verbatim():
    consts = _strings(export_ids.generate()[IDS_FILE])
    assert consts["CHUNK_ID_PATTERN"] == ids.CHUNK_ID_PATTERN
    assert consts["XBRL_ID_PATTERN"] == ids.XBRL_ID_PATTERN
    assert consts["FR_ID_PATTERN"] == ids.FR_ID_PATTERN
    assert consts["DOC_ID_PATTERN"] == ids.DOC_ID_PATTERN
    assert consts["CITE_PATTERN"] == ids.CITE_RE.pattern


def test_the_anchored_forms_end_in_a_dollar_never_in_a_python_z():
    consts = _strings(export_ids.generate()[IDS_FILE])
    for kind in ("CHUNK", "XBRL", "FR", "DOC"):
        anchored = consts[f"{kind}_ID_ANCHORED"]
        assert anchored.startswith("^") and anchored.endswith("$"), kind
        assert "\\Z" not in anchored, kind


def test_the_anchored_chunk_form_keeps_its_stricter_prefix_bound():
    """CHUNK_ID_RE has {10,30} before the first colon; CHUNK_ID_PATTERN has + (ids.py). The anchored form is built from the
    compiled _RE object, so a 9- or 31-character prefix is refused by classify and still found by the scan."""
    consts = _strings(export_ids.generate()[IDS_FILE])
    assert "{10,30}" in consts["CHUNK_ID_ANCHORED"]
    assert "{10,30}" not in consts["CHUNK_ID_PATTERN"]


def test_the_exported_regexps_are_built_without_flags():
    """No m flag: with it a JS $ would match before a line break and an id plus a newline would classify as an id."""
    text = export_ids.generate()[IDS_FILE]
    for kind in ("CHUNK", "XBRL", "FR", "DOC"):
        assert f"export const {kind}_ID_RE = new RegExp({kind}_ID_ANCHORED);" in text


def test_the_pages_file_lists_spa_pages_in_source_order():
    assert _spa_pages(export_ids.generate()[PAGES_FILE]) == list(ui_v2.SPA_PAGES)


def test_the_pages_file_lists_the_13_sec_filers_in_source_order_and_not_samsung():
    filers = _filers(export_ids.generate()[PAGES_FILE])
    assert filers == [(ticker, spec[0]) for ticker, spec in universe.FILERS.items()]
    assert len(filers) == 13
    assert "SSNLF" not in {ticker for ticker, _ in filers}


def test_the_pages_file_exports_a_ticker_type():
    assert 'export type Ticker = (typeof FILERS)[number]["ticker"];' in export_ids.generate()[PAGES_FILE]


def test_output_is_deterministic_ascii_lf_with_one_final_newline_and_no_machine_detail():
    first, second = export_ids.generate(), export_ids.generate()
    assert first == second
    assert set(first) == {IDS_FILE, PAGES_FILE}, "the loop below must have something to check"
    for rel, text in first.items():
        assert text.isascii() and "\r" not in text, rel
        assert text.endswith("\n") and not text.endswith("\n\n"), rel
        assert "GENERATED FILE - DO NOT EDIT" in text.splitlines()[0], rel
        assert "uv run python -m scripts.export_ids" in text, rel
        for detail in (str(ROOT), ROOT.name, "Users", "\\"[:0] + ":\\"):
            assert detail not in text, (rel, detail)
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text), (rel, "a date")


# ---------------------------------------------------------------- stale in both directions

def test_the_committed_outputs_are_current():
    generated = export_ids.generate()
    assert set(generated) == {IDS_FILE, PAGES_FILE}
    for rel in generated:
        assert (ROOT / rel).is_file(), f"{rel} is not committed. Regenerate: uv run python -m scripts.export_ids"
    stale = export_ids.check(ROOT)
    assert stale == [], f"stale or missing: {stale}. Regenerate: uv run python -m scripts.export_ids"


def test_check_is_empty_right_after_a_write(written):
    assert (written / IDS_FILE).is_file() and (written / PAGES_FILE).is_file()
    assert export_ids.check(written) == []


def test_a_tampered_file_is_reported_stale(written):
    target = written / IDS_FILE
    target.write_bytes(target.read_bytes().replace(b"CHUNK_ID_PATTERN", b"CHUNK_ID_PATTERNX", 1))
    assert export_ids.check(written) == [IDS_FILE]


def test_a_missing_file_is_reported_stale(written):
    (written / PAGES_FILE).unlink()
    assert export_ids.check(written) == [PAGES_FILE]


def test_a_crlf_checkout_is_not_stale(written):
    """git with core.autocrlf rewrites an LF file as CRLF on checkout; the content is the same."""
    for rel in (IDS_FILE, PAGES_FILE):
        path = written / rel
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert export_ids.check(written) == []
    assert export_ids.main(["--check", "--root", str(written)]) == 0


def test_a_lone_cr_or_a_changed_character_is_still_stale(written):
    path = written / PAGES_FILE
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r", 1))
    assert export_ids.check(written) == [PAGES_FILE]


def test_a_changed_filers_source_makes_the_committed_file_stale(monkeypatch):
    changed = {**universe.FILERS, "ZZZZ": ("Zed", "10-K", "10-Q")}
    monkeypatch.setattr(universe, "FILERS", changed)
    assert export_ids.check(ROOT) == [PAGES_FILE]


def test_a_renamed_filer_makes_the_committed_file_stale(monkeypatch):
    changed = {**universe.FILERS, "NVDA": ("Nvidia Corporation", "10-K", "10-Q")}
    monkeypatch.setattr(universe, "FILERS", changed)
    assert export_ids.check(ROOT) == [PAGES_FILE]


def test_a_changed_spa_pages_source_makes_the_committed_file_stale(monkeypatch):
    monkeypatch.setattr(ui_v2, "SPA_PAGES", (*ui_v2.SPA_PAGES, "/extra/"))
    assert export_ids.check(ROOT) == [PAGES_FILE]


def test_a_changed_unanchored_pattern_makes_the_committed_file_stale(monkeypatch):
    monkeypatch.setattr(ids, "FR_ID_PATTERN", ids.FR_ID_PATTERN + "x?")
    assert export_ids.check(ROOT) == [IDS_FILE]


def test_a_changed_anchored_object_makes_the_committed_file_stale(monkeypatch):
    monkeypatch.setattr(ids, "CHUNK_ID_RE", re.compile(r"^[0-9\-]{10,29}:[IVX]+\.[0-9A-Z]+:[0-9]{4}\Z"))
    assert export_ids.check(ROOT) == [IDS_FILE]


def test_an_anchored_object_with_a_python_dollar_stops_the_generator(monkeypatch):
    monkeypatch.setattr(ids, "XBRL_ID_RE", re.compile(rf"^{ids.XBRL_ID_PATTERN}$"))
    with pytest.raises(ValueError):
        export_ids.generate()


def test_a_file_that_is_not_utf8_is_stale_not_a_crash(written):
    (written / IDS_FILE).write_bytes(b"\xff\xfe not utf-8")
    assert export_ids.check(written) == [IDS_FILE]


def test_a_non_ascii_company_name_stops_the_generator(monkeypatch):
    """Output is ASCII by contract (so no encoding of the machine matters); a name outside it must not be written silently."""
    monkeypatch.setattr(universe, "FILERS", {**universe.FILERS, "ZZZZ": ("Caf" + chr(0xE9), "10-K", "10-Q")})
    with pytest.raises(ValueError):
        export_ids.generate()


def test_main_reports_a_grammar_it_cannot_translate_and_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ids, "DOC_ID_RE", re.compile(r"^doc:\d+\Z"))
    assert export_ids.main(["--check", "--root", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().out
    assert not (tmp_path / "web").exists()


# ---------------------------------------------------------------- the command line

def test_main_writes_lf_files_and_a_second_run_changes_nothing(tmp_path, capsys):
    assert export_ids.main(["--root", str(tmp_path)]) == 0
    for rel in (IDS_FILE, PAGES_FILE):
        raw = (tmp_path / rel).read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
    before = {rel: (tmp_path / rel).stat().st_mtime_ns for rel in (IDS_FILE, PAGES_FILE)}
    capsys.readouterr()
    assert export_ids.main(["--root", str(tmp_path)]) == 0
    assert "up to date" in capsys.readouterr().out
    assert {rel: (tmp_path / rel).stat().st_mtime_ns for rel in (IDS_FILE, PAGES_FILE)} == before


def test_main_check_fails_naming_the_stale_file_and_the_command(tmp_path, capsys):
    assert export_ids.main(["--check", "--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert IDS_FILE in out and PAGES_FILE in out and "uv run python -m scripts.export_ids" in out
    assert not (tmp_path / "web").exists(), "--check must not write"


def test_the_documented_command_runs_from_the_repo_root():
    proc = subprocess.run([sys.executable, "-m", "scripts.export_ids", "--check"], cwd=ROOT, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up to date" in proc.stdout


# ---------------------------------------------------------------- the shared corpus (Python)

def test_the_corpus_labels_hold_in_python(corpus):
    assert [ids.classify_id(entry["id"]) for entry in corpus["classify"]] == [entry["kind"] for entry in corpus["classify"]]
    assert [ids.CITE_RE.findall(entry["text"]) for entry in corpus["text"]] == [entry["ids"] for entry in corpus["text"]]


def test_the_corpus_covers_what_r5_names(corpus):
    classify_ids = [entry["id"] for entry in corpus["classify"]]
    texts = [entry["text"] for entry in corpus["text"]]
    kinds = {entry["kind"] for entry in corpus["classify"]}
    assert kinds == {*KINDS, None}
    assert any(i.endswith("\n") for i in classify_ids), "a trailing newline"
    assert any(i.endswith("%0A") for i in classify_ids), "%0A"
    chunk_shaped = re.compile(r"([0-9\-]+):[IVX]+\.[0-9A-Z]+:[0-9]{4}")
    classify_prefixes = {len(m.group(1)) for m in map(chunk_shaped.fullmatch, classify_ids) if m}
    assert {9, 10, 30, 31} <= classify_prefixes, "chunk prefixes on both sides of the {10,30} bound (classify)"
    scan_prefixes = {len(prefix) for t in texts for prefix in re.findall(r"\[([0-9\-]+):[IVX]+\.", t)}
    assert {9, 31} <= scan_prefixes, "9- and 31-character chunk prefixes (scan)"
    assert any("<img" in t or "javascript:" in t for t in texts + classify_ids), "adversarial strings"
    assert any(not i.isascii() for i in classify_ids), "non-ASCII near-misses"
    for kind in KINDS:
        assert sum(1 for entry in corpus["classify"] if entry["kind"] == kind) >= 2, f"valid {kind} ids"


def test_the_two_engines_differ_by_design_on_a_short_chunk_prefix(corpus):
    """The difference the classification must record: CITE_RE finds a 9-character prefix, classify_id refuses it."""
    short = ("1" * 9) + ":I.1A:0001"
    assert ids.CITE_RE.findall(f"[{short}]") == [short]
    assert ids.classify_id(short) is None


# ---------------------------------------------------------------- the shared corpus (JavaScript, generated constants)

def _run_node(root: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the generated patterns are run under V8 with tests/data/citation_corpus_eval.mjs")
    proc = subprocess.run(
        [node, str(EVAL_HELPER), str(root / IDS_FILE), str(root / PAGES_FILE), str(CORPUS)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"node exited {proc.returncode}:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(proc.stdout)


@pytest.fixture
def node_result(written) -> dict:
    return _run_node(written)


def test_the_helper_processed_every_corpus_entry(node_result, corpus):
    """Not vacuous: a helper that returns nothing, or stops early, fails here."""
    assert node_result["processed"] == {"classify": len(corpus["classify"]), "text": len(corpus["text"])}
    assert len(node_result["classify"]) == len(corpus["classify"]) > 0
    assert len(node_result["text"]) == len(corpus["text"]) > 0


def test_javascript_classifies_every_corpus_id_as_labelled(node_result, corpus):
    got = node_result["classify"]
    for entry, kind in zip(corpus["classify"], got, strict=True):
        assert kind == entry["kind"], (entry["id"], entry["note"])


def test_javascript_scans_every_corpus_text_as_labelled(node_result, corpus):
    got = node_result["text"]
    for entry, found in zip(corpus["text"], got, strict=True):
        assert found == entry["ids"], (entry["text"], entry["note"])


def test_the_generated_patterns_compile_under_the_unicode_flags_too(node_result):
    """\\- outside a class and \\Z are errors under u or v; both are absent from what is emitted."""
    names = set(node_result["strings"])
    assert {"CHUNK_ID_PATTERN", "CITE_PATTERN", "DOC_ID_ANCHORED"} <= names
    for flags in ("none", "u", "v"):
        assert node_result["compile"][flags] == {name: True for name in names}, flags


def test_the_exported_regexps_carry_no_flags_under_v8(node_result):
    assert node_result["regexpFlags"] == {"CHUNK_ID_RE": "", "XBRL_ID_RE": "", "FR_ID_RE": "", "DOC_ID_RE": ""}


def test_javascript_reads_the_pages_file_as_python_wrote_it(node_result):
    assert node_result["pages"]["SPA_PAGES"] == list(ui_v2.SPA_PAGES)
    assert node_result["pages"]["FILERS"] == [{"ticker": t, "name": spec[0]} for t, spec in universe.FILERS.items()]
