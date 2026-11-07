"""Every GitHub Actions workflow is pinned and least-privilege (I1 security review: S10, S13; docs/specs/m5b-frontend.md R14).

``tests/test_web_pins.py`` pins these properties for ``tests.yml`` together with the ``web`` job's shape. This file holds the same
three rules for EVERY file in ``.github/workflows/``, so a workflow added or edited later (the freshness heartbeat was the one
that had slipped through) cannot hold an action on a movable tag, an implicit token scope or a token left in ``.git/config``
(service images, such as the CI Neo4j, are pinned by version tag and are not covered here):

1. every ``uses:`` is a full 40-hex commit SHA with a trailing ``# vX.Y.Z`` comment (a tag is a pointer the action's owner, or
   whoever compromises it, can move), and one action carries the same pin in every file;
2. the workflow has its own ``permissions:`` block, read-only at workflow level; a job that needs a write scope states it in
   ``JOBS_WITH_WRITE_PERMISSIONS`` below, with the reason;
3. every ``actions/checkout`` sets ``persist-credentials: false``; a job that truly needs the token to push states it in
   ``CHECKOUTS_THAT_KEEP_CREDENTIALS`` below, with the reason.

The write and credential tables are empty today: the tests workflow only reads, and the heartbeat is a public read-only GET;
the security-scan job also reads pull requests (``JOBS_WITH_EXTRA_READ_SCOPES``). The workflow
files are read as data (this cannot run Actions); the text scan is cross-checked against the YAML parse because the parser drops
the trailing ``# vX.Y.Z`` comment this needs.
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
KNOWN_WORKFLOWS = {"tests.yml", "freshness-heartbeat.yml"}      # guards a wrong directory or glob from making every test below vacuous

# (workflow file, job id) -> why that job holds a write scope. A job that needs one keeps exactly that scope at job level.
JOBS_WITH_WRITE_PERMISSIONS: dict[tuple[str, str], str] = {}
# (workflow file, job id) -> why that job's checkout keeps the token in .git/config (it pushes, or fetches a private submodule).
CHECKOUTS_THAT_KEEP_CREDENTIALS: dict[tuple[str, str], str] = {}
# (workflow file, job id) -> why that job reads more than `contents`. Its job-level block REPLACES the workflow's, so it restates
# `contents: read`; each extra scope is read-only.
JOBS_WITH_EXTRA_READ_SCOPES: dict[tuple[str, str], tuple[dict[str, str], str]] = {
    ("tests.yml", "security-scan"): ({"contents": "read", "pull-requests": "read"},
                                     "gitleaks-action lists a pull request's commits (GET /pulls/{n}/commits) on pull_request events"),
}

USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d+\.\d+\.\d+\s*$")
LOCAL_ACTION = "./"      # an action or reusable workflow in this repository is versioned with the commit itself: nothing to pin


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


WORKFLOW_IDS = [p.name for p in workflow_files()]
EACH_WORKFLOW = pytest.mark.parametrize("path", workflow_files(), ids=WORKFLOW_IDS)


def load(path: Path) -> dict:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict) and isinstance(parsed.get("jobs"), dict) and parsed["jobs"], f"{path.name}: no jobs"
    return parsed


def parsed_refs(workflow: dict) -> list[str]:
    """Every ``uses:`` of the YAML: a job that calls a reusable workflow, then each step of a normal job, in file order."""
    refs: list[str] = []
    for job in workflow["jobs"].values():
        if "uses" in job:
            refs.append(str(job["uses"]))
        refs.extend(str(step["uses"]) for step in job.get("steps", []) if "uses" in step)
    return refs


def uses_lines(path: Path) -> list[re.Match[str]]:
    return [m for line in path.read_text(encoding="utf-8").splitlines() if (m := USES_LINE.match(line))]


def checkouts(workflow: dict) -> list[tuple[str, dict]]:
    return [(job_id, step) for job_id, job in workflow["jobs"].items() for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout@")]


def grants_write(permissions: object) -> bool:
    return "write" in str(permissions)      # a mapping with a write scope, or the shorthand `write-all`


# ---------------------------------------------------------------- the directory itself

def test_the_workflow_directory_is_not_empty_and_holds_the_known_workflows():
    assert KNOWN_WORKFLOWS <= set(WORKFLOW_IDS), WORKFLOW_IDS


@EACH_WORKFLOW
def test_the_workflow_parses_as_yaml_with_jobs(path):
    assert load(path)["jobs"]


# ---------------------------------------------------------------- 1. pinned actions

@EACH_WORKFLOW
def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(path):
    found = [m for m in uses_lines(path) if not m["ref"].startswith(LOCAL_ACTION)]
    parsed = [ref for ref in parsed_refs(load(path)) if not ref.startswith(LOCAL_ACTION)]
    assert [m["ref"] for m in found] == parsed, "a `uses:` line the text scan and the YAML parse disagree on"
    for m in found:
        assert SHA_PIN.match(m["ref"]), f"{path.name}: {m['ref']}: pin it to a full 40-hex commit SHA, not a tag or branch"
        assert VERSION_COMMENT.match(m["rest"]), f"{path.name}: {m['ref']}: add a trailing `# vX.Y.Z` comment, got {m['rest']!r}"


def test_one_action_has_the_same_pin_in_every_workflow():
    pins: dict[str, set[tuple[str, str]]] = {}
    for path in workflow_files():
        for m in uses_lines(path):
            action, sha = m["ref"].split("@")
            pins.setdefault(action, set()).add((sha, m["rest"].strip()))
    assert pins, "no `uses:` line found in any workflow"
    assert {action: sorted(p) for action, p in pins.items() if len(p) > 1} == {}


# ---------------------------------------------------------------- 2. least-privilege token

@EACH_WORKFLOW
def test_the_workflow_states_a_read_only_token_of_its_own(path):
    workflow = load(path)
    assert "permissions" in workflow, f"{path.name}: no workflow-level `permissions:` (the token defaults to the repository's setting)"
    permissions = workflow["permissions"]
    assert isinstance(permissions, dict), f"{path.name}: permissions must be a mapping of scope to read/none, got {permissions!r}"
    assert not grants_write(permissions), f"{path.name}: a write scope belongs on the one job that needs it: {permissions}"
    assert permissions.get("contents") == "read", f"{path.name}: the checkout needs `contents: read`: {permissions}"


@EACH_WORKFLOW
def test_a_job_with_a_write_scope_is_listed_with_its_reason(path):
    widened = {job_id for job_id, job in load(path)["jobs"].items() if grants_write(job.get("permissions", {}))}
    unexplained = {job_id for job_id in widened if not JOBS_WITH_WRITE_PERMISSIONS.get((path.name, job_id), "").strip()}
    assert unexplained == set(), f"{path.name}: name each job and the reason in JOBS_WITH_WRITE_PERMISSIONS: {sorted(unexplained)}"


@EACH_WORKFLOW
def test_a_job_level_token_is_either_a_listed_write_or_exactly_its_listed_read_scopes(path):
    for job_id, job in load(path)["jobs"].items():
        if "permissions" not in job or (path.name, job_id) in JOBS_WITH_WRITE_PERMISSIONS:
            continue
        expected, reason = JOBS_WITH_EXTRA_READ_SCOPES.get((path.name, job_id), (None, ""))
        assert reason.strip(), f"{path.name}: job {job_id} sets its own permissions: list it with the reason"
        assert job["permissions"] == expected, f"{path.name}: job {job_id}: {job['permissions']} is not {expected} ({reason})"


def test_each_job_listed_with_extra_read_scopes_states_them():
    for (name, job_id), (expected, reason) in JOBS_WITH_EXTRA_READ_SCOPES.items():
        job = load(WORKFLOWS / name)["jobs"].get(job_id)
        assert job is not None and job.get("permissions") == expected, f"{name}: job {job_id} must state {expected}: {reason}"
        assert not grants_write(expected) and expected.get("contents") == "read"


def test_the_exception_tables_hold_no_stale_entry():
    jobs = {(p.name, job_id): job for p in workflow_files() for job_id, job in load(p)["jobs"].items()}
    stale_write = {key for key in JOBS_WITH_WRITE_PERMISSIONS if key not in jobs or not grants_write(jobs[key].get("permissions", {}))}
    stale_checkout = {key for key in CHECKOUTS_THAT_KEEP_CREDENTIALS if key not in jobs or not any(
        step.get("with", {}).get("persist-credentials") is not False for _, step in checkouts({"jobs": {key[1]: jobs[key]}}))}
    assert (stale_write, stale_checkout) == (set(), set()), "an exception that no longer applies is removed, not kept"


# ---------------------------------------------------------------- 3. no token left in .git/config

@EACH_WORKFLOW
def test_every_checkout_leaves_no_token_in_the_git_config(path):
    for job_id, step in checkouts(load(path)):
        if step.get("with", {}).get("persist-credentials") is False:
            continue
        reason = CHECKOUTS_THAT_KEEP_CREDENTIALS.get((path.name, job_id), "")
        assert reason.strip(), f"{path.name}: job {job_id}: set `persist-credentials: false` on actions/checkout (or state why in the table)"
