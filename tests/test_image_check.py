"""The image containment check and its CI job (docs/security/accepted-risks.md R5-7 pre-I4 check and R5-3 gate;
docs/specs/m5b-frontend.md Acceptance, verifier Warning 4 and R13).

``deploy/checks/image_check.sh`` runs INSIDE the built production image, as the serving user, and prints one PASS or FAIL line
per check. ``.github/workflows/image.yml`` builds the image, starts it with fake secrets (canaries) and runs the script. Neither
can run on a Windows desktop (no Docker, no /proc), so these tests read both as data, mirroring tests/test_web_pins.py:

* every named check is in the script, and the script's list of names is the list of the checks it reports (two-way);
* the harness (the part that counts failures and sets the exit status) is run for real under ``sh`` when one is available;
* the workflow is pinned the way tests.yml is, runs the script as the serving user, passes the canaries, uses no secrets.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "checks" / "image_check.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "image.yml"
SERVING_USER = "app"
CANARY_NAMES = (
    "NEO4J_PASSWORD", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TURNSTILE_SECRET_KEY", "ADMIN_TOKEN",
    "EMBEDDING_API_KEY", "ORIGIN_AUTH_SECRET", "IP_HASH_PEPPER", "LANGFUSE_SECRET_KEY", "LANGFUSE_HASH_SALT",
)
# Settings fields that are repr=False but hold a URL, not a secret.
NOT_SECRET_FIELDS = ("openai_base_url", "anthropic_api_base", "anthropic_base_url")
CHECKS = (
    "not_root", "canary_env_present", "api_pid_found", "api_proc_environ_unreadable", "api_proc_root_owned",
    "canary_not_in_files", "canary_not_in_api_proc", "canary_not_in_readable_proc_environ", "no_env_file",
    "worker_env_scrubbed", "worker_no_new_privs", "worker_nproc_zero", "worker_proc_root_owned", "worker_parse_ok",
    "worker_leaves_no_tmp", "worker_cmd_flags", "no_setuid", "nothing_writable", "dist_root_owned", "dist_readable",
    "dist_path_default", "srv_root_owned", "python_env_flags", "no_node", "workspace_store_none",
    "tools_present", "api_runs_as_app", "worker_fork_refused",
)
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d+\.\d+\.\d+\s*$")
HARNESS = re.compile(r"^# harness begin\n(?P<body>.*?)^# harness end\n", re.S | re.M)


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


def expected_names(script: str) -> list[str]:
    match = re.search(r"^EXPECTED='(?P<names>[^']*)'$", script, re.M)
    assert match, "the script must list the checks it expects in one single-quoted EXPECTED='...' assignment"
    return match.group("names").split()


def reported_names(script: str) -> set[str]:
    shell = re.findall(r"\breport\s+([a-z_]+)\s+(?:PASS|FAIL|SKIP)\b", script)
    python = re.findall(r'result\(\s*"([a-z_]+)"', script)
    return set(shell) | set(python)


# ---------------------------------------------------------------- the script


def test_every_named_check_is_in_the_script_and_nothing_else_is_reported(script):
    assert sorted(expected_names(script)) == sorted(CHECKS)
    assert reported_names(script) == set(CHECKS)


def test_script_is_posix_sh_ascii_and_has_no_shell_extensions(script):
    assert script.startswith("#!/bin/sh\n")
    assert script.isascii()
    for banned in ("[[", "<(", "local ", "function ", "$'", "pipefail", "source ", "declare ", "echo -e"):
        assert banned not in script, banned
    assert not re.search(r"\[ [^\]\n]*==", script), "== inside [ ] is a bashism"
    assert not re.search(r"^\s*set\s+\+e", script, re.M), "an error must never be switched off for the whole script"


def test_script_never_spells_the_canary_prefix_so_it_cannot_match_its_own_search(script):
    # the image check greps /srv for the canary prefix; this file lives in /srv/checks. It builds the prefix from two halves.
    assert "CANARY" + "_" not in script
    assert 'pat="CANARY""_"' in script


def test_script_runs_as_the_serving_user_and_refuses_root(script):
    assert re.search(r'id -u', script)
    assert "not_root" in script


def test_script_exits_non_zero_when_any_check_fails_and_when_one_is_missing(script):
    assert re.search(r'^finish\s*$', script, re.M), "the last statement must be finish"
    assert script.rstrip().endswith("finish")
    match = HARNESS.search(script)
    assert match, "the harness must sit between '# harness begin' and '# harness end'"
    body = match.group("body")
    assert "fails=$((fails + 1))" in body
    assert "exit 1" in body
    assert 'for n in $EXPECTED' in body, "a check that never reported must count as a failure"


def test_python_part_failure_is_a_failure(script):
    assert "py_rc" in script
    assert re.search(r'py_rc.*-ne 0|"\$py_rc" -ne 0', script)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_script_has_valid_sh_syntax():
    result = subprocess.run(["sh", "-n"], input=SCRIPT.read_bytes(), capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def _run_harness(script: str, body: str, expected: str, mode: str = "ci") -> subprocess.CompletedProcess:
    harness = HARNESS.search(script).group("body")
    program = f"EXPECTED='{expected}'\nmode={mode}\n{harness}\n{body}\n"
    return subprocess.run(["sh"], input=program.encode(), capture_output=True, timeout=60)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("body,code", [
    ("report a PASS ok\nreport b PASS ok\nfinish", 0),
    ("report a PASS ok\nreport b FAIL broken\nfinish", 1),
    ("report a PASS ok\nfinish", 1),                       # b never reported
    ("report a PASS ok\nreport b pass sloppy\nfinish", 1),   # only the exact word PASS passes
    ("report a FAIL x\nreport b FAIL y\nfinish", 1),
])
def test_harness_exit_status(script, body, code):
    result = _run_harness(script, body, "a b")
    assert result.returncode == code, result.stdout.decode()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("mode,code", [("ci", 1), ("deployed", 0)])
def test_a_skipped_check_fails_in_ci_mode_and_is_counted_apart_in_deployed_mode(script, mode, code):
    result = _run_harness(script, "report a PASS ok\nreport b SKIP 'not here'\nfinish", "a b", mode)
    out = result.stdout.decode()
    assert result.returncode == code, out
    assert "SKIP b not here" in out.splitlines()
    assert "PASS b" not in out
    if code == 0:
        assert "all checks PASS, 1 skipped (deployed mode)" in out
    else:
        assert "all checks PASS" not in out


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_the_finish_line_of_a_run_without_skips_says_nothing_about_skips(script):
    out = _run_harness(script, "report a PASS ok\nreport b PASS ok\nfinish", "a b", "deployed").stdout.decode()
    assert "image check: all checks PASS\n" in out
    assert "skipped" not in out


def _segment(script: str, pattern: str) -> str:
    match = re.search(pattern, script, re.S | re.M)
    assert match, pattern
    return match.group(0)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("py_rc,code", [(0, 0), (3, 1)])
def test_a_crash_of_the_python_part_is_a_failure_even_when_its_lines_pass(script, py_rc, code):
    segment = _segment(script, r'^py_out=\$\(run_python.*?^EOF\n')
    body = f"run_python() {{ echo 'RESULT a PASS ok'; return {py_rc}; }}\nSECRET_NAMES=x\n{segment}\nfinish"
    result = _run_harness(script, body, "a")
    assert result.returncode == code, result.stdout.decode()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("uid,status", [("0", "FAIL"), ("1000", "PASS")])
def test_not_root_reads_the_uid(script, uid, status):
    segment = _segment(script, r'^uid=\$\(id -u\)\nif .*?fi$')
    out = _run_harness(script, f"id() {{ echo {uid}; }}\n{segment}", "not_root").stdout.decode()
    assert out.splitlines()[0].startswith(f"{status} not_root")


def test_the_tools_the_script_uses_are_all_checked(script):
    line = next(l for l in script.splitlines() if l.startswith("for t in "))
    listed = line.split(" in ", 1)[1].split(";", 1)[0].split()
    for tool in ("find", "grep", "stat", "awk", "tr", "printenv", "dirname", "id", "head", "cat"):
        assert tool in listed, tool
    assert "find grep stat awk tr printenv dirname id head cat" in script


def test_the_canary_search_skips_only_the_model_directory_under_srv(script):
    assert "--exclude-dir" not in script, "a directory name pattern would skip any directory called models, under /tmp as well"
    assert "! -path /srv/models" in script
    line = next(l for l in script.splitlines() if "found=$(grep -rlaF" in l)
    assert " /srv " not in line, "/srv itself is searched through its entries other than the models directory"


def test_image_check_mode_is_never_set_by_the_image_or_the_machine_config():
    for name in ("Dockerfile", "fly.toml"):
        assert "IMAGE_CHECK_MODE" not in (ROOT / name).read_text(encoding="utf-8"), name


def test_check_1_pins_the_mode_to_ci_so_the_canary_checks_cannot_be_skipped(workflow):
    lines = [l for l in _steps_text(workflow).splitlines() if "docker exec -u app " in l and "image_check.sh" in l]
    assert len(lines) == 1
    assert "-e IMAGE_CHECK_MODE=ci " in lines[0], lines[0]


def test_harness_prints_one_line_per_check_with_its_status(script):
    result = _run_harness(script, "report a PASS fine\nreport b FAIL bad\nfinish", "a b")
    lines = result.stdout.decode().splitlines()
    assert "PASS a fine" in lines
    assert "FAIL b bad" in lines


# ---------------------------------------------------------------- the workflow


def test_workflow_has_least_privilege_and_a_timeout(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert len(jobs) == 1
    (job,) = jobs.values()
    assert job["timeout-minutes"] == 60
    assert "permissions" not in job, "no job widens the token"


def test_workflow_triggers(workflow):
    on = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in on
    push = on["push"]
    assert push["branches"] == ["v2"]
    for path in ("Dockerfile", "deploy/**", "src/semigraph/**", ".github/workflows/image.yml"):
        assert path in push["paths"], path
    assert "pull_request" not in on


def test_every_action_is_pinned_to_a_commit_with_a_version_comment(workflow_text):
    uses = [line for line in workflow_text.splitlines() if USES_LINE.match(line.split("#")[0])]
    assert uses, "the job checks out the repository"
    for line in uses:
        code, _, comment = line.partition("#")
        ref = USES_LINE.match(code).group("ref")
        assert SHA_PIN.match(ref), f"not pinned by full commit SHA: {ref}"
        assert VERSION_COMMENT.match("  #" + comment), f"no trailing # vX.Y.Z comment: {line}"


def test_checkout_does_not_persist_credentials(workflow):
    (job,) = workflow["jobs"].values()
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] is False


def _steps_text(workflow) -> str:
    (job,) = workflow["jobs"].values()
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def test_the_production_dockerfile_is_built_with_defaults_and_no_secret(workflow):
    text = _steps_text(workflow)
    assert re.search(r"docker build\b[^\n]*\s\.\s*$", text, re.M)
    build_args = re.findall(r"--build-arg\s+(\w+)=", text)
    assert build_args == ["EMBEDDER_VARIANT"], "the one build argument is the variant production builds with"
    assert "--secret" not in text
    assert "--target" not in text, "the check runs on the runtime stage, the image that is deployed"
    assert "-f " not in text.split("docker build", 1)[1].split("\n", 1)[0], "the default Dockerfile"


def test_the_container_gets_every_canary_and_no_network(workflow):
    text = _steps_text(workflow)
    run_line = next(line for line in text.splitlines() if "docker run" in line or "-e " in line)
    assert run_line
    for name in CANARY_NAMES:
        assert f"{name}=CANARY_{name}" in text, name
    assert "--network none" in text
    assert "docker run -d" in text


def test_the_script_runs_in_the_container_as_the_serving_user(workflow):
    text = _steps_text(workflow)
    assert f"docker exec -u {SERVING_USER} " in text
    assert "sh /srv/checks/image_check.sh" in text
    assert "--privileged" not in text
    for line in text.splitlines():
        if re.search(r"docker exec[^\n]*-u (root|0)\b", line):
            assert "setpriv --reuid app" in line or "command -v setpriv" in line, "a root exec may only drop to the serving user first (the deployed-mode step)"


def test_the_job_waits_for_the_boot_line_with_a_timeout_and_always_prints_the_log(workflow):
    (job,) = workflow["jobs"].values()
    text = _steps_text(workflow)
    assert "process marked non-dumpable" in text
    assert "docker logs" in text
    always = [s for s in job["steps"] if str(s.get("if", "")).strip() in ("always()", "${{ always() }}")]
    assert any("docker logs" in str(s.get("run", "")) for s in always)
    assert any("docker rm" in str(s.get("run", "")) for s in always)


def test_the_container_log_must_not_carry_a_canary(workflow):
    text = _steps_text(workflow)
    assert re.search(r"docker logs[^\n]*\n?[^\n]*CANARY", text) or "CANARY" in text.split("docker logs", 1)[1]


def test_workflow_uses_no_github_secret_and_no_fly(workflow_text):
    assert "secrets." not in workflow_text
    assert "flyctl" not in workflow_text.lower()
    assert "fly deploy" not in workflow_text.lower()
    assert "FLY_API_TOKEN" not in workflow_text
    assert "${{ github.token }}" not in workflow_text


def test_workflow_is_ascii_and_free_of_local_paths(workflow_text):
    assert workflow_text.isascii()
    assert not re.search(r"[A-Za-z]:[\\/](Users|Documents)", workflow_text)


# ---------------------------------------------------------------- review fixes (CR-1..4, SR-1..3)


def _secret_settings_fields() -> list[str]:
    from semigraph.config import Settings

    return sorted(n for n, f in Settings.model_fields.items() if f.repr is False and n not in NOT_SECRET_FIELDS)


def test_every_secret_setting_has_a_canary_in_the_script_and_the_workflow(script, workflow_text):
    fields = _secret_settings_fields()
    assert len(fields) >= 10, fields
    names = re.search(r'^SECRET_NAMES="([^"]*)"$', script, re.M).group(1).split()
    for field in fields:
        env = field.upper()
        assert env in names, f"{env} has no canary in SECRET_NAMES"
        assert f"-e {env}=CANARY_{env}" in workflow_text, f"{env} is not passed to the container as a canary"
    assert sorted(names) == sorted(CANARY_NAMES)


def test_every_canary_value_passes_the_settings_length_checks(workflow_text):
    # The app refuses to start with a secret shorter than 32 bytes (asgi_middleware MIN_SECRET_BYTES for
    # ORIGIN_AUTH_SECRET); a short canary made the first CI run fail before the check could run.
    values = re.findall(r"-e ([A-Z0-9_]+)=(CANARY_\S+)", workflow_text)
    assert sorted({name for name, _ in values}) == sorted(CANARY_NAMES)
    for name, value in values:
        assert len(value.encode()) >= 32, f"{name} canary is {len(value)} bytes"


def test_the_worker_env_name_test_covers_pepper_salt_and_auth(script):
    line = next(l for l in script.splitlines() if "for w in (" in l)
    for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PEPPER", "SALT", "AUTH"):
        assert f'"{word}"' in line, word


def test_the_script_has_no_carriage_return():
    assert b"\r" not in SCRIPT.read_bytes()


def test_gitattributes_forces_lf_for_the_check_scripts():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "deploy/checks/*.sh text eol=lf" in rules


def test_the_image_is_built_with_the_variant_fly_toml_builds_with(workflow, workflow_text):
    text = _steps_text(workflow)
    assert "fly.toml" in text and "tomllib" in text and "EMBEDDER_VARIANT" in text
    assert re.search(r"docker build\b[^\n]*--build-arg EMBEDDER_VARIANT=\"?\$", text)
    on = workflow.get("on", workflow.get(True))
    assert "fly.toml" in on["push"]["paths"]
    assert "flyctl" not in workflow_text.lower()


def test_every_exec_of_the_script_has_a_time_limit_shorter_than_the_servers_window(workflow):
    (job,) = workflow["jobs"].values()
    seen = 0
    for step in job["steps"]:
        for line in str(step.get("run", "")).splitlines():
            if "image_check.sh" in line and "docker exec" in line:
                seen += 1
                match = re.search(r"\btimeout (\d+) docker exec", line)
                assert match and int(match.group(1)) <= 80, line
    assert seen == 2
    assert "rerun" in _steps_text(workflow)


def test_the_workflow_proves_the_deployed_mode_command_the_runbook_gives(workflow):
    text = _steps_text(workflow)
    assert "command -v setpriv" in text
    assert "IMAGE_CHECK_MODE=deployed" in text
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "setpriv --reuid app --regid app --clear-groups env IMAGE_CHECK_MODE=deployed sh /srv/checks/image_check.sh" in runbook
    assert 'flyctl ssh console -a semigraph -C "sh /srv/checks/image_check.sh"' not in runbook


def test_deployed_mode_skips_the_canary_checks_and_keeps_the_rest(script):
    assert "IMAGE_CHECK_MODE" in script
    for name in ("canary_env_present", "canary_not_in_files", "canary_not_in_api_proc", "canary_not_in_readable_proc_environ"):
        assert re.search(rf'report {name} SKIP "\$skipped"', script), name
        assert not re.search(rf'report {name} PASS "\$skipped"', script), name
    assert 'skipped="skipped: no canaries in deployed mode"' in script


def test_the_script_checks_the_serving_user_of_the_server_and_that_forks_are_refused(script):
    assert re.search(r"/proc/\$api_pid/status", script) and "Uid:" in script and "id -u app" in script
    assert "os.fork()" in script and "BlockingIOError" in script and "os._exit(0)" in script
    assert "threading.Thread" in script and "RuntimeError" in script
    assert re.search(r'result\("worker_fork_refused"', script)


def test_the_script_checks_its_tools_and_has_a_positive_control(script):
    assert "command -v" in script and "tools_present" in script
    assert re.search(r'grep -qaF -e "\$pat" /proc/\$\$/environ', script)


def test_no_node_prunes_site_packages_and_the_parent_walk_covers_missing_paths(script):
    node_line = next(l for l in script.splitlines() if l.startswith("nodefile=$(find"))
    assert "site-packages" in node_line
    block = script.split("for p in $(python -c", 1)[1].split("\ndone\n", 1)[0]
    assert "continue" not in block, "a missing sys.path entry must still have its parents checked"


def test_the_worker_command_flags_check_reads_the_parse_workers_own_builder(script):
    assert "parse._worker_command(" in script
    assert 'sandbox.module_command("some.module"' not in script


def test_the_tmp_comparison_ignores_hidden_top_level_entries(script):
    assert 'startswith(".")' in script


def test_the_script_has_no_unused_helper(script):
    assert "yes_no" not in script


def test_the_workflow_comments_do_not_overstate_the_evidence(workflow_text):
    assert "network egress" in workflow_text and "not measured" in workflow_text


def test_each_run_of_the_script_gets_a_fresh_container_with_its_own_boot_line_wait(workflow):
    (job,) = workflow["jobs"].values()
    (step,) = [s for s in job["steps"] if "image_check.sh" in str(s.get("run", ""))]
    body = str(step["run"])
    first, second = [m.start() for m in re.finditer(r"timeout 80 docker exec", body)]
    between = body[first:second]
    assert "start_and_wait" in between, "a fresh container (removed, started, waited for) before the second exec"
    fn = body.split("start_and_wait() {", 1)[1].split("\n          }\n", 1)[0]
    assert "docker rm -f semigraph-check" in fn and "docker run -d" in fn and "process marked non-dumpable" in fn
    assert body.count("start_and_wait\n") == 2


def test_the_first_ci_image_run_findings_stay_fixed(script):
    # Image run 42688935960: four checks measured the wrong thing. The canary values carry a suffix (prefix match),
    # non-dumpable is measured by the refused reads and PR_GET_DUMPABLE (the /proc owner stays the uid on that kernel),
    # and the workspace-store search skips source files.
    assert 'case "$v" in "$pat$n"*) ;;' in script
    assert 'cat "/proc/$api_pid/maps"' in script and 'owner" = "0"' not in script
    assert 'prctl(3, 0, 0, 0, 0)' in script and 'info["dumpable"] == 0' in script
    assert 'info["proc_uid"] == 0' not in script
    ws_line = next(line for line in script.splitlines() if line.startswith("ws=$(find"))
    assert "-path /srv/src" in ws_line and "-name '*.py'" in ws_line and "-prune" in ws_line
