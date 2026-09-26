"""Repo hygiene that a redeploy depends on: production models come from the repo, CI covers the shipped dependency set."""

import glob
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fly() -> dict:
    return tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))


def test_fly_env_pins_the_production_answering_models(fly):
    env = fly["env"]
    assert env["ANSWER_MODEL"] == "openai/gpt-6-luna"
    assert env["ESCALATION_MODEL"] == "anthropic/claude-sonnet-5"


def test_the_code_default_answer_model_stays_sonnet_so_local_runs_do_not_change():
    from semigraph.config import Settings

    assert Settings.model_fields["answer_model"].default == "anthropic/claude-sonnet-5"
    assert Settings.model_fields["escalation_model"].default == ""


def test_env_example_documents_the_model_roles():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ANSWER_MODEL" in text and "ESCALATION_MODEL" in text
    assert "EXTRACTION and the eval JUDGES only" in text


def test_ci_runs_on_pushes_to_v2(workflow):
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert {"master", "main", "v2"} <= set(triggers["push"]["branches"])


def test_ci_tests_the_shipped_dependency_set(workflow):
    job = workflow["jobs"]["serve-shipped"]
    steps = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "pip install -r deploy/requirements-serve.txt" in steps
    assert "pip install --no-deps -e ." in steps
    assert "pytest" in steps
    assert "security-scan" in workflow["jobs"], "the secret-hygiene job must stay"
    gitleaks = [s for s in workflow["jobs"]["security-scan"]["steps"] if "gitleaks" in str(s.get("uses", ""))]
    assert gitleaks


def test_every_serving_test_pattern_in_ci_matches_a_file(workflow):
    run = next(s["run"] for s in workflow["jobs"]["serve-shipped"]["steps"] if "pytest -q" in str(s.get("run", "")))
    patterns = [t for t in run.split() if t.startswith("tests/")]
    assert patterns
    for pattern in patterns:
        assert glob.glob(str(ROOT / pattern)), f"CI pattern matches nothing: {pattern}"


def test_shipped_requirements_pin_the_litellm_the_image_runs():
    text = (ROOT / "deploy" / "requirements-serve.txt").read_text(encoding="utf-8")
    assert "litellm==1.100.0" in text
