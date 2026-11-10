"""Writes the client constants that must follow the server (docs/specs/m5b-frontend.md R5).

    uv run python -m scripts.export_ids [--check] [--root PATH]

Two TypeScript files come out of the Python sources of truth, so the browser and the server cannot disagree:

* ``web/src/lib/ids.generated.ts``: the four citation-id patterns of ``semigraph.retrieval.ids``, the scan pattern ``CITE_RE``
  and the anchored forms of the four ``*_RE`` objects, translated for JavaScript;
* ``web/src/lib/pages.generated.ts``: the page paths ``semigraph.serve.ui_v2.SPA_PAGES`` and the ``{ticker, name, annualForm, quarterly}`` list of
  ``semigraph.universe.FILERS`` (the 13 SEC filers; Samsung is in ``UNIVERSE`` only).

Without ``--check`` the files are (re)written. With ``--check`` nothing is written and the exit code is 1 when a file is
missing or differs from what the sources produce; ``tests/test_export_ids.py`` runs the same comparison, so a stale file is a
red test. Line endings are normalised when comparing (``\\r\\n`` equals ``\\n``): git with ``core.autocrlf`` rewrites a committed
LF file as CRLF on a Windows checkout, which is not a change of content. The generator itself always writes LF.

The translation is deliberately narrow. Python ends an anchored form with ``\\Z`` (end of the string and nothing else, so an
id followed by a newline is refused); JavaScript has no ``\\Z`` (without the ``u`` flag it is a literal ``Z``, with it a syntax
error), and its ``$`` without the ``m`` flag also means end of input only, so ``\\Z`` becomes ``$``. Everything else is copied
only when both engines read it the same way; syntax they read differently (``\\d``, ``\\w``, a Python ``$``, inline flags,
possessive quantifiers, ...) is refused with a ``ValueError``, not copied. Output is plain ASCII with no date, path or other
detail of the machine that ran it.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from semigraph import universe
from semigraph.retrieval import ids
from semigraph.serve import ui_v2

ROOT = Path(__file__).resolve().parent.parent
IDS_FILE = "web/src/lib/ids.generated.ts"
PAGES_FILE = "web/src/lib/pages.generated.ts"
COMMAND = "uv run python -m scripts.export_ids"

_QUANTIFIER = re.compile(r"\{[0-9]+(?:,[0-9]*)?\}")
# Unescaped inside a class these are literals in Python and in JavaScript without a flag, but a syntax error (or, doubled, a set
# operation) under the u or v flag, and a nested "[" is a nested class under v. A class is portable only without them.
_CLASS_UNPORTABLE = frozenset("[(){}/|")
_KINDS = ("CHUNK", "XBRL", "FR", "DOC")


# ---------------------------------------------------------------- the translation

def _escape_is_portable(char: str) -> bool:
    """A backslash before ASCII punctuation means that character in both engines; before a letter or digit it can mean a class
    (``\\d``), an assertion (``\\b``), a back-reference or something else the engines read differently."""
    return char.isascii() and not char.isalnum()


def _check_group_opening(pattern: str, index: int) -> None:
    if pattern.startswith("(?", index) and not pattern.startswith("(?:", index):
        raise ValueError(f"group syntax at {index} reads differently in JavaScript: {pattern!r}")


def translate_python_regex(pattern: str) -> str:
    """The JavaScript source of a Python pattern, or ``ValueError`` when the two engines would read it differently."""
    if not pattern.isascii():
        raise ValueError(f"non-ASCII pattern: {pattern!r}")
    out: list[str] = []
    depth, index, in_class, after_quantifier = 0, 0, False, False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                raise ValueError(f"trailing backslash: {pattern!r}")
            escaped = pattern[index + 1]
            if escaped == "Z" and not in_class and index + 2 == len(pattern):
                out.append("$")                      # \Z only ever as the very last token
            elif _escape_is_portable(escaped):
                out.append(pattern[index:index + 2])
            else:
                raise ValueError(f"escape \\{escaped} at {index} is not portable: {pattern!r}")
            index, after_quantifier = index + 2, False
            continue
        if in_class:
            first = out[-1] in ("[", "[^")
            if char in _CLASS_UNPORTABLE or (char == "]" and first) or (char in "&-" and pattern.startswith(char * 2, index)):
                raise ValueError(f"class syntax at {index} reads differently in JavaScript: {pattern!r}")
            in_class = char != "]"
            out.append(char)
            index, after_quantifier = index + 1, False
            continue
        if char == "[":
            in_class = True
            caret = pattern.startswith("[^", index)
            out.append("[^" if caret else "[")
            index, after_quantifier = index + (2 if caret else 1), False
        elif char == "{":
            match = _QUANTIFIER.match(pattern, index)
            if not match:
                raise ValueError(f"a brace at {index} that is no quantifier: {pattern!r}")
            out.append(match.group())
            index, after_quantifier = match.end(), True
        elif char in "*+?":
            if char == "+" and after_quantifier:
                raise ValueError(f"possessive quantifier at {index}: {pattern!r}")
            out.append(char)
            index, after_quantifier = index + 1, not (char == "?" and after_quantifier)
        elif char == "$":
            raise ValueError(f"a Python $ accepts a trailing newline, a JavaScript $ does not: {pattern!r}")
        elif char == ".":
            raise ValueError(f"a Python . matches \\r, U+2028 and U+2029, a JavaScript . does not: {pattern!r}")
        else:
            if char == "(":
                _check_group_opening(pattern, index)
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError(f"unbalanced group: {pattern!r}")
            out.append(char)
            index, after_quantifier = index + 1, False
    if in_class or depth:
        raise ValueError(f"unclosed class or group: {pattern!r}")
    return "".join(out)


def js_pattern(compiled: re.Pattern, *, anchored: bool) -> str:
    """The JavaScript source of a compiled pattern. Only the default flags are portable; an anchored form must be anchored at
    both ends once translated, an unanchored one at neither."""
    if compiled.flags & ~re.UNICODE:
        raise ValueError(f"flags {compiled.flags} are not portable: {compiled.pattern!r}")
    source = translate_python_regex(compiled.pattern)
    if anchored != (source.startswith("^") and source.endswith("$")):
        raise ValueError(f"expected an {'anchored' if anchored else 'unanchored'} form, got {source!r}")
    return source


# ---------------------------------------------------------------- the two files

def _literal(value: str) -> str:
    """A TypeScript string literal (JSON is one) of an ASCII value."""
    if not value.isascii():
        raise ValueError(f"non-ASCII value: {value!r}")
    return json.dumps(value)


def _header(sources: str) -> list[str]:
    return [
        "// GENERATED FILE - DO NOT EDIT.",
        f"// Written by scripts/export_ids.py from {sources}.",
        f"// Regenerate with: {COMMAND}",
        "// tests/test_export_ids.py fails while this file is stale.",
    ]


def render_ids() -> str:
    """``ids.generated.ts``. Reads the attributes of ``ids`` when called, so a changed source changes the output."""
    unanchored = {
        "CHUNK": ids.CHUNK_ID_PATTERN, "XBRL": ids.XBRL_ID_PATTERN, "FR": ids.FR_ID_PATTERN, "DOC": ids.DOC_ID_PATTERN,
    }
    anchored = {
        "CHUNK": ids.CHUNK_ID_RE, "XBRL": ids.XBRL_ID_RE, "FR": ids.FR_ID_RE, "DOC": ids.DOC_ID_RE,
    }
    lines = _header("src/semigraph/retrieval/ids.py") + [
        "//",
        "// The citation-id grammar of the server. The *_ID_PATTERN strings embed in larger expressions; the *_ID_ANCHORED",
        "// strings and the *_ID_RE objects validate a whole string. Python ends an anchored form with \\Z (end of the string and",
        "// nothing else); JavaScript has no \\Z, so the generator writes $, which means the same without the m flag: build these",
        "// without it. CHUNK_ID_ANCHORED is stricter than CHUNK_ID_PATTERN (10 to 30 characters before the first colon).",
        "// CITE_PATTERN is a source string, not a RegExp: a shared global RegExp keeps lastIndex between uses, so a caller makes",
        '// its own: new RegExp(CITE_PATTERN, "g"); group 1 of a match is the bare id.',
        "",
    ]
    for kind in _KINDS:
        source = js_pattern(re.compile(unanchored[kind]), anchored=False)
        lines.append(f"export const {kind}_ID_PATTERN = {_literal(source)};")
    lines.append(f"export const CITE_PATTERN = {_literal(js_pattern(ids.CITE_RE, anchored=False))};")
    for kind in _KINDS:
        lines.append(f"export const {kind}_ID_ANCHORED = {_literal(js_pattern(anchored[kind], anchored=True))};")
    for kind in _KINDS:
        lines.append(f"export const {kind}_ID_RE = new RegExp({kind}_ID_ANCHORED);")
    return "\n".join(lines) + "\n"


def render_pages() -> str:
    """``pages.generated.ts``: the SPA page paths and the 13 SEC filers, both in source order."""
    lines = _header("SPA_PAGES in src/semigraph/serve/ui_v2.py and FILERS in src/semigraph/universe.py") + [
        "",
        "// The paths of the app's pages; {ticker} is the one path parameter.",
        "export const SPA_PAGES = [",
    ]
    lines += [f"  {_literal(page)}," for page in ui_v2.SPA_PAGES]
    lines += [
        "] as const;",
        "",
        "// The 13 SEC filers (a company page exists for each). Samsung has no SEC filings and is not here. annualForm is the",
        "// annual report's form; quarterly is false for a filer with no quarterly 10-Q (TSMC and ASML file a 20-F only).",
        "export const FILERS = [",
    ]
    lines += [
        f"  {{ ticker: {_literal(ticker)}, name: {_literal(spec[0])}, annualForm: {_literal(spec[1])}, "
        f"quarterly: {'true' if spec[2] is not None else 'false'} }},"
        for ticker, spec in universe.FILERS.items()
    ]
    lines += ["] as const;", "", 'export type Ticker = (typeof FILERS)[number]["ticker"];']
    return "\n".join(lines) + "\n"


def generate() -> dict[str, str]:
    """Every output file as ``{repo-relative POSIX path: text}``."""
    return {IDS_FILE: render_ids(), PAGES_FILE: render_pages()}


# ---------------------------------------------------------------- write and check

def _normalised(raw: bytes) -> str | None:
    """The file text with CRLF read as LF, or ``None`` when it is not UTF-8."""
    try:
        return raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return None


def check(root: Path) -> list[str]:
    """The repo-relative paths under ``root`` that are missing or differ from what the sources produce."""
    stale = []
    for rel, text in generate().items():
        path = root / rel
        if not path.is_file() or _normalised(path.read_bytes()) != text:
            stale.append(rel)
    return stale


def write(root: Path) -> list[str]:
    """Write the files that are missing or stale under ``root`` (LF); returns the paths written."""
    stale = set(check(root))
    outputs = generate()
    for rel in sorted(stale):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(outputs[rel].encode("utf-8"))
    return sorted(stale)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the client constants generated from the Python sources of truth.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when a file is missing or stale")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root the files live under (default: this one)")
    args = parser.parse_args(argv)
    try:
        stale = check(args.root) if args.check else write(args.root)
    except ValueError as error:
        print(f"error: {error}")
        return 2
    if args.check and stale:
        print("stale or missing:")
        for rel in stale:
            print(f"  {rel}")
        print(f"regenerate with: {COMMAND}")
        return 1
    if stale:
        print("wrote:")
        for rel in stale:
            print(f"  {rel}")
    else:
        print("up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
