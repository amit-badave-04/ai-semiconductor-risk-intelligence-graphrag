"""The S10 license gate of web/ (docs/specs/m5b-frontend.md S10 and R6): ``artifacts/m5b_licenses.json`` is generated from
``web/package-lock.json`` and a pytest check fails on any license outside the two allow-lists.

S10's two lists, read literally: the SHIPPED RUNTIME bundle allows MIT, ISC, BSD and Apache-2.0; build and test tools also allow
MPL-2.0 (lightningcss, axe-core), CC-BY-4.0, CC0-1.0, BlueOak-1.0.0 and 0BSD. A lockfile entry not flagged ``dev`` (this
includes ``devOptional``) is counted as runtime, so the runtime list is the superset of what could ship.

Two licenses are in the tree and in NEITHER list: FSL-1.1-Apache-2.0 (``sentry``, pulled in by ``lighthouse``, never bundled)
and MIT-0 (two ``@csstools`` packages, pulled in by ``jsdom``). Whether to amend S10 for them, or to drop the Lighthouse CLI,
is the owner's decision, so it is recorded here and NOT made: ``PENDING_OWNER_DECISION`` lists them exactly (a new license, or
one that stops being needed, is red) and the report says the gate is OPEN. ``test_s10_is_closed`` is a strict ``xfail`` that
turns red the day the set is empty, so the marker and the constant are removed together with the decision.

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
BUILD_TEST_ALLOWED = (*RUNTIME_ALLOWED, "MPL-2.0", "CC-BY-4.0", "CC0-1.0", "BlueOak-1.0.0", "0BSD")
ALLOWED = {"runtime": RUNTIME_ALLOWED, "build_test": BUILD_TEST_ALLOWED}
# (package name, license): not named by S10, awaiting the owner. Keyed without the version, so a patch bump does not matter.
PENDING_OWNER_DECISION = {
    ("sentry", "FSL-1.1-Apache-2.0"),
    ("@csstools/color-helpers", "MIT-0"),
    ("@csstools/css-syntax-patches-for-csstree", "MIT-0"),
}
PENDING_REASON = {
    "FSL-1.1-Apache-2.0": "not named by S10; dev tool only (never bundled); options: amend S10's build/test list, or drop the lighthouse dependency",
    "MIT-0": "not named by S10; permissive; dev tool only (never bundled); option: amend S10's build/test list",
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
    """Rows outside their scope's allow-list, whether or not the owner's pending decision covers them."""
    return [r for r in rows if not is_allowed(r["license"], ALLOWED[r["scope"]])]


def pending(rows: list[dict]) -> set[tuple[str, str]]:
    return {(r["name"], str(r["license"])) for r in violations(rows)}


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
        "outside_allow_lists": [{**r, "required_by": owners.get(r["name"], []),
                                 "decision": "PENDING_OWNER: " + PENDING_REASON.get(str(r["license"]), "no reason recorded")
                                 if (r["name"], str(r["license"])) in PENDING_OWNER_DECISION else "NOT ALLOWED"}
                                for r in outside],
        "s10_license_gate": "OPEN: awaiting the owner's decision on " + ", ".join(sorted({lic for _, lic in pending(rows)}))
        if outside else "PASS: every license is inside its allow-list",
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
    ("MIT-0", BUILD_TEST_ALLOWED, False),                                # not named by S10
    ("FSL-1.1-Apache-2.0", BUILD_TEST_ALLOWED, False),
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


# ---------------------------------------------------------------- the real lockfile

def test_the_lockfile_is_the_dependency_tree_the_report_covers(lock, rows):
    assert lock["lockfileVersion"] == 3 and len(rows) > 100
    assert all(r["version"] for r in rows)


def test_every_runtime_license_is_inside_the_runtime_allow_list_with_no_exception(rows):
    shipped = [r for r in rows if r["scope"] == "runtime"]
    assert len(shipped) >= 20
    assert [r for r in shipped if not is_allowed(r["license"], RUNTIME_ALLOWED)] == []


def test_every_license_is_inside_its_allow_list_or_is_pending_the_owner(rows):
    unexplained = [r for r in violations(rows) if (r["name"], str(r["license"])) not in PENDING_OWNER_DECISION]
    assert unexplained == [], "a license that S10 does not name and the owner has not been asked about"


def test_the_pending_set_is_exactly_what_the_tree_holds(rows):
    """A pending entry whose package left the tree (or whose license S10 now names) is stale and must go; a new one is the
    test above. So the constant can only shrink by a decision."""
    assert pending(rows) == PENDING_OWNER_DECISION


@pytest.mark.xfail(strict=True, reason="S10 license gate OPEN: FSL-1.1-Apache-2.0 (sentry) and MIT-0 (@csstools) are not in S10's "
                                       "allow-lists; the owner amends S10 or drops lighthouse, then this marker and the "
                                       "PENDING_OWNER_DECISION constant go")
def test_s10_is_closed(rows):
    assert pending(rows) == set()


# ---------------------------------------------------------------- the artifact

def test_the_report_is_what_the_lockfile_says(lock):
    expected = build_report(lock)
    if RECORD:
        REPORT.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    assert REPORT.is_file(), "artifacts/m5b_licenses.json is missing: RECORD_LICENSES=1 uv run pytest tests/test_web_licenses.py"
    assert json.loads(REPORT.read_text(encoding="utf-8")) == expected


def test_the_report_states_both_allow_lists_and_says_the_gate_is_open_while_a_decision_is_pending(lock):
    report = build_report(lock)
    assert report["allow_lists"]["runtime"] == list(RUNTIME_ALLOWED)
    assert set(RUNTIME_ALLOWED) < set(report["allow_lists"]["build_test"])
    assert report["s10_license_gate"].startswith("OPEN") and "MIT-0" in report["s10_license_gate"]
    assert {(r["name"], r["license"]) for r in report["outside_allow_lists"]} == PENDING_OWNER_DECISION
    assert all(r["decision"].startswith("PENDING_OWNER") and r["required_by"] for r in report["outside_allow_lists"])
    assert report["totals"]["packages"] == len(report["packages"]) == sum(report["totals"][s] for s in ALLOWED)
