"""artifacts/m5b_lighthouse.json and artifacts/m5b_axe.json: the recorded M5b a11y and quality runs (spec Acceptance, B3, P3).

The thresholds are the spec's: Lighthouse accessibility >= 95 and best-practices >= 90 on five pages x mobile and desktop
(ten runs; performance and SEO are recorded, never gated); axe (WCAG 2.2 AA tags) zero serious or critical violations on the
five pages in both colour schemes. Each artifact records the commit it was produced at, and that commit must be the current
HEAD or one of its ancestors, so a stale artifact cannot be mistaken for a run of newer code.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIGHTHOUSE = ROOT / "artifacts" / "m5b_lighthouse.json"
AXE = ROOT / "artifacts" / "m5b_axe.json"

PAGES = ("/v2/", "/v2/company/NVDA/", "/v2/workspace/", "/v2/freshness/", "/v2/method/")
FORM_FACTORS = ("mobile", "desktop")
SCHEMES = ("light", "dark")
MIN_ACCESSIBILITY = 0.95
MIN_BEST_PRACTICES = 0.90
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
HEAD_KEYS = ("head_sha", "date", "node", "os")


def _load(path: Path) -> dict:
    assert path.is_file(), f"{path.name} has not been recorded"
    return json.loads(path.read_text(encoding="utf-8"))


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def lighthouse() -> dict:
    return _load(LIGHTHOUSE)


@pytest.fixture(scope="module")
def axe() -> dict:
    return _load(AXE)


@pytest.mark.parametrize("name", ["lighthouse", "axe"])
def test_provenance_fields_are_recorded(name, request):
    data = request.getfixturevalue(name)
    for key in HEAD_KEYS:
        assert data.get(key), f"{name}: {key} is missing"
    assert re.fullmatch(r"[0-9a-f]{40}", data["head_sha"]), f"{name}: head_sha is not a full sha"


@pytest.mark.parametrize("name", ["lighthouse", "axe"])
def test_recorded_head_is_an_ancestor_of_or_equal_to_the_repo_head(name, request):
    data = request.getfixturevalue(name)
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout")
    if _git("cat-file", "-e", f"{data['head_sha']}^{{commit}}").returncode != 0:
        pytest.skip("recorded commit is not in this (shallow) checkout")
    result = _git("merge-base", "--is-ancestor", data["head_sha"], "HEAD")
    assert result.returncode == 0, f"{name}: {data['head_sha']} is not an ancestor of HEAD"


def test_lighthouse_covers_five_pages_by_two_form_factors(lighthouse):
    runs = lighthouse["runs"]
    assert len(runs) == 10
    assert {(r["page"], r["formFactor"]) for r in runs} == {(p, f) for p in PAGES for f in FORM_FACTORS}


def test_lighthouse_meets_the_gated_scores(lighthouse):
    assert lighthouse["gates"] == {"accessibility": MIN_ACCESSIBILITY, "best-practices": MIN_BEST_PRACTICES}
    for run in lighthouse["runs"]:
        label = f"{run['page']} {run['formFactor']}"
        assert run["scores"]["accessibility"] >= MIN_ACCESSIBILITY, label
        assert run["scores"]["best-practices"] >= MIN_BEST_PRACTICES, label


def test_lighthouse_records_performance_and_seo_without_gating_them(lighthouse):
    for run in lighthouse["runs"]:
        assert isinstance(run["scores"]["performance"], (int, float))
        assert isinstance(run["scores"]["seo"], (int, float))
    assert "performance" not in lighthouse["gates"] and "seo" not in lighthouse["gates"]


def test_axe_covers_five_pages_in_both_schemes(axe):
    assert axe["tags"] == AXE_TAGS
    assert {(r["page"], r["scheme"]) for r in axe["runs"]} >= {(p, s) for p in PAGES for s in SCHEMES}


def test_axe_has_no_serious_or_critical_violation(axe):
    for run in axe["runs"]:
        blocking = [v for v in run["violations"] if v["impact"] in ("serious", "critical")]
        assert blocking == [], f"{run['page']} {run['scheme']}: {blocking}"
        assert run["blocking"] == 0
