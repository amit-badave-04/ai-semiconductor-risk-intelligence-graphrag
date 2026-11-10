"""The S10 license gate of web/ (docs/specs/m5b-frontend.md S10 and R6): ``artifacts/m5b_licenses.json`` is generated from
``web/package-lock.json`` and a pytest check fails on any license outside the two allow-lists.

S10's two lists: the SHIPPED RUNTIME bundle allows MIT, ISC, BSD and Apache-2.0; build and test tools also allow MPL-2.0
(lightningcss, axe-core), CC-BY-4.0, 0BSD and, by the owner's decision of 2026-11-07, FSL-1.1-Apache-2.0, MIT-0, BlueOak-1.0.0
and CC0-1.0. A lockfile entry not flagged ``dev`` (this includes ``devOptional``) is counted as runtime, so the runtime list is
the superset of what could ship.

The owner's decision of 2026-11-07 amended S10 for build and test tooling only. FSL-1.1-Apache-2.0 is carried by ``sentry``
(pulled in by the Lighthouse CLI, never bundled) and MIT-0 by two ``@csstools`` packages (pulled in by ``jsdom``); neither is
allowed in the runtime list, so a shipped package with either license fails the gate. The gate is CLOSED: any license outside
its scope's list is red, and the report says OPEN until it is fixed or the owner decides again.

Regenerate the artifact with ``RECORD_LICENSES=1 uv run pytest tests/test_web_licenses.py``; the default mode compares, so a
lockfile change without a refreshed report is red.
"""

import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "web" / "package-lock.json"
REPORT = ROOT / "artifacts" / "m5b_licenses.json"
RECORD = os.environ.get("RECORD_LICENSES") == "1"

RUNTIME_ALLOWED = ("MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0")
BUILD_TEST_ALLOWED = (
    *RUNTIME_ALLOWED, "MPL-2.0", "CC-BY-4.0", "0BSD",
    # The owner's decision of 2026-11-07 (S10 amended, build and test tooling only): these four are NOT in RUNTIME_ALLOWED.
    "FSL-1.1-Apache-2.0", "MIT-0", "BlueOak-1.0.0", "CC0-1.0",
)
ALLOWED = {"runtime": RUNTIME_ALLOWED, "build_test": BUILD_TEST_ALLOWED}
OWNER_DECISION = {
    "date": "2026-11-07",
    "by": "the owner",
    "summary": "S10 amended for build and test tooling only: FSL-1.1-Apache-2.0 (sentry, pulled in by the Lighthouse CLI, never "
               "bundled) is allowed, and MIT-0, BlueOak-1.0.0 and CC0-1.0 are on the build/test allow-list. The runtime "
               "allow-list is unchanged.",
    "build_test_added": ["FSL-1.1-Apache-2.0", "MIT-0", "BlueOak-1.0.0", "CC0-1.0"],
    "runtime_allow_list": "unchanged",
}


# ---------------------------------------------------------------- SPDX expressions

TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9][A-Za-z0-9.+-]*)")


def tokens(expression: str) -> list[str]:
    out, pos = [], 0
    while pos < len(expression):
        match = TOKEN.match(expression, pos)
        if not match:
            if expression[pos:].strip():
                raise ValueError(f"cannot read {expression[pos:]!r}")
            break
        out.append(match.group(1))
        pos = match.end()
    return out


def is_allowed(expression: object, allow: tuple[str, ...]) -> bool:
    """Whether an SPDX license expression is acceptable: ``A OR B`` when either is, ``A AND B`` when both are. A missing or
    non-string license, ``SEE LICENSE IN ...``, ``UNLICENSED``, an exception clause (``WITH``), a syntax error and a name that
    only differs in case all fail (conservative: the owner decides, not the checker)."""
    if not isinstance(expression, str) or not expression.strip():
        return False
    try:
        words = tokens(expression)
    except ValueError:
        return False
    position = 0

    def parse_or() -> bool:
        nonlocal position
        result = parse_and()
        while position < len(words) and words[position] == "OR":
            position += 1
            result = parse_and() or result
        return result

    def parse_and() -> bool:
        nonlocal position
        result = parse_atom()
        while position < len(words) and words[position] == "AND":
            position += 1
            result = parse_atom() and result
        return result

    def parse_atom() -> bool:
        nonlocal position
        if position >= len(words):
            raise ValueError("expression ends early")
        word = words[position]
        position += 1
        if word == "(":
            result = parse_or()
            if position >= len(words) or words[position] != ")":
                raise ValueError("missing )")
            position += 1
            return result
        if word in (")", "AND", "OR", "WITH"):
            raise ValueError(f"unexpected {word}")
        if position < len(words) and words[position] == "WITH":
            position += 2                                                     # an exception clause is not modelled: never allowed
            return False
        return word in allow

    try:
        result = parse_or()
    except ValueError:
        return False
    return result and position == len(words)


# ---------------------------------------------------------------- the inventory

def package_name(key: str) -> str:
    return key.rsplit("node_modules/", 1)[-1]


def scope_of(entry: dict) -> str:
    return "build_test" if entry.get("dev") else "runtime"


def inventory(lock: dict) -> list[dict]:
    rows = [{"name": package_name(key), "version": entry.get("version"), "license": entry.get("license"), "scope": scope_of(entry)}
            for key, entry in lock["packages"].items() if key]
    return sorted(rows, key=lambda r: (r["name"], str(r["version"])))


def dependents(lock: dict) -> dict[str, list[str]]:
    """name -> the sorted names of the lockfile entries that list it as a dependency (the root counts as ``(root)``)."""
    found: dict[str, set[str]] = {}
    for key, entry in lock["packages"].items():
        owner = "(root)" if not key else package_name(key)
        for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name in entry.get(field, {}):
                found.setdefault(name, set()).add(owner)
    return {name: sorted(owners) for name, owners in found.items()}


def violations(rows: list[dict]) -> list[dict]:
    """Rows outside their own scope's allow-list: a tools-only license on a runtime row is one."""
    return [r for r in rows if not is_allowed(r["license"], ALLOWED[r["scope"]])]


def build_report(lock: dict) -> dict:
    rows = inventory(lock)
    outside = violations(rows)
    owners = dependents(lock)
    by_scope = {scope: Counter(str(r["license"]) for r in rows if r["scope"] == scope) for scope in ALLOWED}
    return {
        "spec": "docs/specs/m5b-frontend.md S10 (license report) and R6; generated from web/package-lock.json",
        "regenerate": "RECORD_LICENSES=1 uv run pytest tests/test_web_licenses.py",
        "allow_lists": {"runtime": list(RUNTIME_ALLOWED), "build_test": list(BUILD_TEST_ALLOWED)},
        "scope_rule": "an entry not flagged dev in the lockfile (devOptional included) is runtime; the rest is build and test tools",
        "totals": {"packages": len(rows), **{scope: sum(by_scope[scope].values()) for scope in ALLOWED}},
        "by_license": {scope: dict(sorted(counts.items())) for scope, counts in by_scope.items()},
        "owner_decision": OWNER_DECISION,
        "outside_allow_lists": [{**r, "required_by": owners.get(r["name"], []), "decision": "NOT ALLOWED"} for r in outside],
        "s10_license_gate": "OPEN: outside the allow-lists: " + ", ".join(sorted({str(r["license"]) for r in outside}))
        if outside else "CLOSED: every license is inside its allow-list (S10 as amended by the owner's decision of 2026-11-07)",
        "packages": rows,
    }


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads(LOCKFILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(lock) -> list[dict]:
    return inventory(lock)


# ---------------------------------------------------------------- the evaluator

@pytest.mark.parametrize("expression,allow,expected", [
    ("MIT", RUNTIME_ALLOWED, True),
    ("ISC", RUNTIME_ALLOWED, True),
    ("BSD-3-Clause", RUNTIME_ALLOWED, True),
    ("MPL-2.0", RUNTIME_ALLOWED, False),
    ("MPL-2.0", BUILD_TEST_ALLOWED, True),
    ("0BSD", RUNTIME_ALLOWED, False),
    ("0BSD", BUILD_TEST_ALLOWED, True),
    ("(MIT OR CC0-1.0)", RUNTIME_ALLOWED, True),                         # one allowed term is enough for OR
    ("GPL-3.0-only OR MIT", RUNTIME_ALLOWED, True),
    ("GPL-3.0-only OR AGPL-3.0-only", BUILD_TEST_ALLOWED, False),
    ("MIT AND Zlib", RUNTIME_ALLOWED, False),                            # every term for AND
    ("MIT AND ISC", RUNTIME_ALLOWED, True),
    ("(MIT AND (ISC OR GPL-3.0-only))", RUNTIME_ALLOWED, True),
    ("(MIT AND (GPL-3.0-only OR AGPL-3.0-only))", RUNTIME_ALLOWED, False),
    ("MIT-0", BUILD_TEST_ALLOWED, True),                                 # the owner's decision of 2026-11-07: tools only
    ("MIT-0", RUNTIME_ALLOWED, False),
    ("FSL-1.1-Apache-2.0", BUILD_TEST_ALLOWED, True),
    ("FSL-1.1-Apache-2.0", RUNTIME_ALLOWED, False),
    ("FSL-1.1-MIT", BUILD_TEST_ALLOWED, False),                          # only the Apache variant was decided
    ("BlueOak-1.0.0", BUILD_TEST_ALLOWED, True),
    ("BlueOak-1.0.0", RUNTIME_ALLOWED, False),
    ("CC0-1.0", BUILD_TEST_ALLOWED, True),
    ("CC0-1.0", RUNTIME_ALLOWED, False),
    ("MIT-0 AND MIT", RUNTIME_ALLOWED, False),                           # one tool-only term keeps an AND out of the runtime list
    ("MIT-0 OR MIT", RUNTIME_ALLOWED, True),                             # but an OR with an allowed term is fine
    ("Apache-2.0 WITH LLVM-exception", BUILD_TEST_ALLOWED, False),       # exceptions are not modelled
    ("mit", RUNTIME_ALLOWED, False),                                     # strict: the canonical SPDX spelling only
    ("MIT OR", RUNTIME_ALLOWED, False),
    ("(MIT", RUNTIME_ALLOWED, False),
    ("MIT)", RUNTIME_ALLOWED, False),
    ("MIT ISC", RUNTIME_ALLOWED, False),
    ("MIT; rm", RUNTIME_ALLOWED, False),
    ("SEE LICENSE IN LICENSE.md", BUILD_TEST_ALLOWED, False),
    ("UNLICENSED", BUILD_TEST_ALLOWED, False),
    ("", BUILD_TEST_ALLOWED, False),
    ("   ", BUILD_TEST_ALLOWED, False),
    (None, BUILD_TEST_ALLOWED, False),
    ({"type": "MIT"}, BUILD_TEST_ALLOWED, False),
    (["MIT"], BUILD_TEST_ALLOWED, False),
])
def test_the_spdx_evaluator(expression, allow, expected):
    assert is_allowed(expression, allow) is expected


def test_a_dev_entry_is_build_test_and_everything_else_including_dev_optional_is_runtime():
    assert scope_of({"dev": True}) == "build_test"
    assert scope_of({"dev": True, "optional": True}) == "build_test"
    assert scope_of({"devOptional": True}) == "runtime"
    assert scope_of({}) == "runtime"
    assert scope_of({"optional": True}) == "runtime"


def test_a_nested_package_is_named_by_its_own_name():
    assert package_name("node_modules/a/node_modules/@scope/b") == "@scope/b"
    assert package_name("node_modules/lru-cache") == "lru-cache"


def test_the_checker_catches_a_license_outside_both_lists_and_a_runtime_license_only_the_build_list_allows():
    synthetic = {"packages": {
        "": {"name": "x"},
        "node_modules/ok": {"version": "1.0.0", "license": "MIT"},
        "node_modules/gpl": {"version": "1.0.0", "license": "GPL-3.0-only"},
        "node_modules/none": {"version": "1.0.0"},
        "node_modules/tool": {"version": "1.0.0", "license": "MPL-2.0", "dev": True},
        "node_modules/shipped-mpl": {"version": "1.0.0", "license": "MPL-2.0"},
    }}
    bad = violations(inventory(synthetic))
    assert sorted(r["name"] for r in bad) == ["gpl", "none", "shipped-mpl"]
    report = build_report(synthetic)
    assert report["s10_license_gate"].startswith("OPEN") and report["totals"] == {"packages": 5, "runtime": 4, "build_test": 1}
    assert [r["decision"] for r in report["outside_allow_lists"]] == ["NOT ALLOWED"] * 3


# ---------------------------------------------------------------- the owner's decision of 2026-11-07

# Allowed for build and test tools only; the runtime list gains none of them. Written out here, apart from BUILD_TEST_ALLOWED, so
# that widening the runtime list (or dropping one of these from the tools list) is a visible edit to a test.
DECIDED_FOR_TOOLS_ONLY = ("FSL-1.1-Apache-2.0", "MIT-0", "BlueOak-1.0.0", "CC0-1.0")


def lock_with(license_id: str, **flags: bool) -> dict:
    return {"packages": {"": {"name": "x"}, "node_modules/pkg": {"version": "1.0.0", "license": license_id, **flags}}}


def test_the_owner_decision_widens_the_build_test_list_and_leaves_the_runtime_list_as_it_was():
    assert set(DECIDED_FOR_TOOLS_ONLY) <= set(BUILD_TEST_ALLOWED)
    assert set(DECIDED_FOR_TOOLS_ONLY).isdisjoint(RUNTIME_ALLOWED)
    assert RUNTIME_ALLOWED == ("MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0")
    assert set(RUNTIME_ALLOWED) < set(BUILD_TEST_ALLOWED)


@pytest.mark.parametrize("license_id", ["FSL-1.1-Apache-2.0", "MIT-0"])
@pytest.mark.parametrize("flags", [{}, {"devOptional": True}, {"optional": True}], ids=["runtime", "devOptional", "optional"])
def test_a_runtime_package_with_a_tools_only_license_fails_the_gate(license_id, flags):
    """A hypothetical shipped package (anything the lockfile does not flag ``dev``) with either license is refused."""
    synthetic = lock_with(license_id, **flags)
    assert [r["name"] for r in violations(inventory(synthetic))] == ["pkg"]
    report = build_report(synthetic)
    assert report["s10_license_gate"].startswith("OPEN") and license_id in report["s10_license_gate"]
    assert [(r["name"], r["scope"], r["decision"]) for r in report["outside_allow_lists"]] == [("pkg", "runtime", "NOT ALLOWED")]


@pytest.mark.parametrize("license_id", DECIDED_FOR_TOOLS_ONLY)
def test_a_dev_package_with_a_decided_license_passes_the_gate(license_id):
    synthetic = lock_with(license_id, dev=True)
    assert violations(inventory(synthetic)) == []
    report = build_report(synthetic)
    assert report["s10_license_gate"].startswith("CLOSED") and report["outside_allow_lists"] == []


# ---------------------------------------------------------------- the real lockfile

def test_the_lockfile_is_the_dependency_tree_the_report_covers(lock, rows):
    assert lock["lockfileVersion"] == 3 and len(rows) > 100
    assert all(r["version"] for r in rows)


def test_every_runtime_license_is_inside_the_runtime_allow_list_with_no_exception(rows):
    shipped = [r for r in rows if r["scope"] == "runtime"]
    assert len(shipped) >= 20
    assert [r for r in shipped if not is_allowed(r["license"], RUNTIME_ALLOWED)] == []


def test_s10_is_closed(rows):
    outside = [f"{r['name']}@{r['version']} {r['license']} ({r['scope']})" for r in violations(rows)]
    assert outside == [], "a license outside both allow-lists (the owner's decision of 2026-11-07 is the last amendment)"


# ---------------------------------------------------------------- the artifact

def test_the_report_is_what_the_lockfile_says(lock):
    expected = build_report(lock)
    if RECORD:
        REPORT.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    assert REPORT.is_file(), "artifacts/m5b_licenses.json is missing: RECORD_LICENSES=1 uv run pytest tests/test_web_licenses.py"
    assert json.loads(REPORT.read_text(encoding="utf-8")) == expected


def test_the_report_states_both_allow_lists_says_the_gate_is_closed_and_records_the_owner_decision(lock):
    report = build_report(lock)
    assert report["allow_lists"]["runtime"] == list(RUNTIME_ALLOWED)
    assert report["allow_lists"]["build_test"] == list(BUILD_TEST_ALLOWED)
    assert set(DECIDED_FOR_TOOLS_ONLY) <= set(report["allow_lists"]["build_test"])
    assert set(DECIDED_FOR_TOOLS_ONLY).isdisjoint(report["allow_lists"]["runtime"])
    assert report["s10_license_gate"].startswith("CLOSED")
    assert report["outside_allow_lists"] == []
    assert "PENDING" not in json.dumps(report) and "OPEN" not in report["s10_license_gate"]
    decision = report["owner_decision"]
    assert decision["date"] == "2026-11-07" and decision["by"] == "the owner"
    assert sorted(decision["build_test_added"]) == sorted(DECIDED_FOR_TOOLS_ONLY)
    assert decision["runtime_allow_list"] == "unchanged"
    assert report["totals"]["packages"] == len(report["packages"]) == sum(report["totals"][s] for s in ALLOWED)


# ---------------------------------------------------------------- vendored runtime assets (docs/specs/m5b-redesign.md RD2, SR5)
# Files shipped in the bundle that do not come from npm, so the lockfile gate above cannot see them. The owner's decision of
# 2026-11-10 (D-R9) allows OFL-1.1 for these font files only; RUNTIME_ALLOWED for npm packages is unchanged. Each file is the
# unmodified release file (the OFL reserves the name "Source" for unmodified fonts, so no subsetting), pinned by its sha256.

FONTS_DIR = ROOT / "web" / "src" / "assets" / "fonts"
LICENSES_DIR = ROOT / "web" / "public" / "licenses"
DIST_DIR = Path(os.environ.get("WEB_DIST") or ROOT / "web" / "dist")
SANS_ZIP = "https://github.com/adobe-fonts/source-sans/releases/download/3.052R/WOFF2-source-sans-3.052R.zip"
SERIF_ZIP = "https://github.com/adobe-fonts/source-serif/releases/download/4.005R/source-serif-4.005_WOFF2.zip"
VENDORED_FONTS = (
    {"file": "SourceSans3-Regular.ttf.woff2", "license": "OFL-1.1", "version": "3.052R", "source": SANS_ZIP,
     "sha256": "53492fb3a0def77354f166a55d09b63a10855e91c206c7620a81cf56e97f8ec3"},
    {"file": "SourceSans3-Semibold.ttf.woff2", "license": "OFL-1.1", "version": "3.052R", "source": SANS_ZIP,
     "sha256": "47b9b661b9f395fe7f0d0e119637fba5c8dad97bde3df60066fd24229c0792f4"},
    {"file": "SourceSans3-Bold.ttf.woff2", "license": "OFL-1.1", "version": "3.052R", "source": SANS_ZIP,
     "sha256": "8d35c6d40e750a4ee23bbbba2bd604ad098d172907369b43281507e16b8e2e7a"},
    {"file": "SourceSerif4-Regular.ttf.woff2", "license": "OFL-1.1", "version": "4.005R", "source": SERIF_ZIP,
     "sha256": "6b053e98f0838afe81f3e784727be4583a7c13bb42f198dc5202ecffee0aaee0"},
)
FONT_LICENSES = (
    {"file": "OFL-SourceSans3.txt", "source": "https://raw.githubusercontent.com/adobe-fonts/source-sans/3.052R/LICENSE.md",
     "sha256": "89ad2c4f66dd29127527493e729c31e731f111cf10faf5774c3db9275ed0c22c"},
    {"file": "OFL-SourceSerif4.txt", "source": "https://raw.githubusercontent.com/adobe-fonts/source-serif/4.005R/LICENSE.md",
     "sha256": "75784a295293a8992f5a8d99210566e0064a012e6dab6731305e3787f15896c7"},
)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_vendored_font_is_the_pinned_release_file_and_nothing_else_is_vendored():
    assert sorted(p.name for p in FONTS_DIR.glob("*.woff2")) == sorted(f["file"] for f in VENDORED_FONTS)
    for font in VENDORED_FONTS:
        assert _sha256(FONTS_DIR / font["file"]) == font["sha256"], font["file"]
        assert font["license"] == "OFL-1.1" and font["license"] not in RUNTIME_ALLOWED


def test_the_font_licenses_ship_verbatim_from_the_same_release_tags():
    for lic in FONT_LICENSES:
        path = LICENSES_DIR / lic["file"]
        assert _sha256(path) == lic["sha256"], lic["file"]
        assert "SIL Open Font License, Version 1.1" in path.read_text(encoding="utf-8")
    assert {lic["source"].split("/")[5] for lic in FONT_LICENSES} == {f["version"] for f in VENDORED_FONTS}


def test_the_fonts_readme_records_every_file_with_its_source_and_checksum():
    readme = (FONTS_DIR / "README.md").read_text(encoding="utf-8")
    for item in (*VENDORED_FONTS, *FONT_LICENSES):
        assert item["file"] in readme and item["sha256"] in readme and item["source"] in readme, item["file"]


def test_the_built_app_ships_only_the_vendored_fonts_unchanged_and_their_licenses():
    if not (DIST_DIR / "index.html").is_file():
        pytest.skip(f"no built app at {DIST_DIR}; the CI web job runs this after the build")
    pinned = {f["sha256"] for f in VENDORED_FONTS}
    shipped = list((DIST_DIR / "assets").glob("*.woff2"))
    assert shipped and all(_sha256(p) in pinned for p in shipped), [p.name for p in shipped]
    for lic in FONT_LICENSES:
        assert _sha256(DIST_DIR / "licenses" / lic["file"]) == lic["sha256"]
