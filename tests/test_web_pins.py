"""web/ pins and the CI wiring around them (docs/specs/m5b-frontend.md R1, R14, S10; I1 part 3).

The lockfile is the reproducibility record of the frontend, so it is pinned from every side: ``package.json`` holds exact
versions only, ``web/.npmrc`` turns install scripts off and keeps saved versions exact, the lockfile carries the Linux native
binaries (it must have been resolved on Linux, not on the Windows desktop), every lockfile entry comes from the public npm
registry with an integrity hash, and the five places that name a Node version agree: ``web/.node-version``, the Dockerfile
tag, the CI ``web`` job and the two existing ``setup-node`` steps.

The CI tests read ``.github/workflows/tests.yml`` as data: they cannot run Actions, so they pin the steps that make the
``web`` job mean what the spec says (``npm ci --ignore-scripts`` only, the dist contract run and never skipped, the Python
setup the fixture server needs).
"""

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
NODE_VERSION = "24.22.2"
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# The R1 list (the lockfile decides the rest). Each is a direct dependency of web/package.json.
REQUIRED = {
    "vite", "@vitejs/plugin-react", "react", "react-dom", "typescript", "oxlint", "react-router", "tailwindcss",
    "@tailwindcss/vite", "@base-ui/react", "eventsource-parser", "vitest", "@vitest/coverage-v8", "jsdom",
    "@testing-library/react", "@testing-library/dom", "@testing-library/user-event", "@testing-library/jest-dom",
    "@playwright/test", "@axe-core/playwright", "lighthouse", "@types/react", "@types/react-dom", "@types/node",
    "lucide-react", "cn", "class-variance-authority",
}
# The native binaries R1 names: a lockfile resolved on Windows may omit them (inferred), so each must be present as linux-x64-gnu.
LINUX_NATIVE = (r"(^|/)@rolldown/binding-linux-x64-gnu$", r"(^|/)lightningcss-linux-x64-gnu$",
                r"(^|/)@tailwindcss/oxide-linux-x64-gnu$")
REGISTRY = "https://registry.npmjs.org/"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d+\.\d+\.\d+\s*$")


@pytest.fixture(scope="module")
def package() -> dict:
    return json.loads((WEB / "package.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def npmrc() -> dict[str, str]:
    lines = (WEB / ".npmrc").read_text(encoding="utf-8").splitlines()
    pairs = [line.split("=", 1) for line in lines if line.strip() and not line.lstrip().startswith(("#", ";"))]
    return {key.strip(): value.strip() for key, value in pairs}


def dependencies(package: dict) -> dict[str, str]:
    return {**package.get("dependencies", {}), **package.get("devDependencies", {})}


# ---------------------------------------------------------------- package.json and .npmrc

def test_every_version_in_package_json_is_exact(package):
    deps = dependencies(package)
    assert deps, "package.json lists no dependency"
    loose = {name: spec for name, spec in deps.items() if not EXACT_VERSION.match(spec)}
    assert not loose, f"ranges, tags or URLs are not allowed: {loose}"
    assert not {k for k in package if k in ("peerDependencies", "optionalDependencies", "bundledDependencies", "overrides")}


def test_the_r1_pin_list_is_installed_as_direct_dependencies(package):
    assert REQUIRED <= set(dependencies(package)), sorted(REQUIRED - set(dependencies(package)))


def test_the_shadcn_cli_is_run_with_npx_when_a_component_is_added_and_is_not_a_dependency(package, lock):
    """The shadcn CLI pulled deepmerge 4.3.1 and minimatch 10.2.6 (two osv-scanner findings, one with no fixed version), plus
    dotenvx, an MCP server and babel, into every `npm ci` of CI and of the Docker web stage, and nothing in web/ imports it
    (the components keep their own copied-in source; components.json is the CLI's configuration only). It is run only as
    `npx shadcn@4.23.0 add <component>` on the day a component is added (D17 allows that), never installed."""
    assert "shadcn" not in dependencies(package)
    shadcn = sorted(k for k in lock["packages"] if re.search(r"(^|/)(shadcn|@shadcn)(/|$)", k))
    assert shadcn == [], shadcn
    readme = (WEB / "README.md").read_text(encoding="utf-8")
    assert "npx shadcn@4.23.0" in readme


def test_nothing_in_web_imports_the_shadcn_package():
    """``@/components/ui/*`` are copied-in files; ``shadcn/tailwind.css`` was dropped at scaffold (its always-emitted `.shimmer`
    rule carries an animation). A new import would need the package back, so it is red here first."""
    import_shadcn = re.compile(r"""(?:from\s*|import\s*\(?\s*|@import\s*|require\s*\(\s*)["']shadcn[/"']""")
    scanned = [p for base in ("src", "e2e", "scripts") for p in (WEB / base).rglob("*") if p.is_file()]
    scanned += [p for p in WEB.glob("*") if p.is_file() and p.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".css", ".html"}
                and p.name not in ("package.json", "package-lock.json")]
    assert len(scanned) > 20
    assert sorted(p.name for p in scanned if import_shadcn.search(p.read_text(encoding="utf-8", errors="replace"))) == []


def test_package_json_is_a_private_esm_project_with_the_node_floor(package):
    assert package["private"] is True and package["type"] == "module"
    assert package["engines"] == {"node": f">={NODE_VERSION}"}


def test_the_install_scripts_are_off_and_saved_versions_are_exact():
    rc = npmrc()
    assert rc.get("ignore-scripts") == "true", rc
    assert rc.get("save-exact") == "true", rc
    assert rc.get("engine-strict") == "true", rc            # the floor is enforced on every install, not only documented


def test_the_scripts_the_ci_job_and_the_readme_name_exist(package):
    scripts = package["scripts"]
    for name in ("dev", "build", "lint", "typecheck", "test", "test:coverage", "e2e", "lighthouse", "audit"):
        assert name in scripts, name
    assert scripts["build"].startswith("tsc -b && vite build")     # R3: tsc -b is part of the build
    assert "--ignore-scripts" not in scripts["build"]
    for hook in ("preinstall", "install", "postinstall", "prepare", "prebuild", "postbuild", "pretest", "posttest"):
        assert hook not in scripts, f"{hook}: ignore-scripts would skip it, so logic must not live there"


# ---------------------------------------------------------------- lockfile

def test_the_lockfile_is_lockfile_v3_and_matches_package_json(package, lock):
    assert lock["lockfileVersion"] == 3
    root = lock["packages"][""]
    assert root.get("dependencies", {}) == package.get("dependencies", {})
    assert root.get("devDependencies", {}) == package.get("devDependencies", {})
    assert root["engines"] == package["engines"]


@pytest.mark.parametrize("pattern", LINUX_NATIVE)
def test_the_lockfile_was_resolved_on_linux_and_holds_the_native_binaries(lock, pattern):
    keys = [k for k in lock["packages"] if re.search(pattern, k)]
    assert keys, f"no {pattern} entry: the lockfile was probably resolved on Windows"
    for key in keys:
        entry = lock["packages"][key]
        assert entry.get("os") == ["linux"] and entry.get("cpu") == ["x64"], (key, entry.get("os"), entry.get("cpu"))
        assert entry.get("libc") in (None, ["glibc"]), key


def test_every_lockfile_entry_comes_from_the_public_registry_with_an_integrity_hash(lock):
    """An entry flagged ``inBundle`` ships INSIDE its parent's tarball (the wasm fallback of @tailwindcss/oxide bundles its
    runtime), so it has no ``resolved`` or ``integrity`` of its own: its parent's tarball hash covers it."""
    entries = {k: v for k, v in lock["packages"].items() if k}
    assert len(entries) > 100
    bundled = {k for k, v in entries.items() if v.get("inBundle")}
    for key, entry in entries.items():
        assert not entry.get("link"), key
        if key in bundled:
            assert "resolved" not in entry and "integrity" not in entry, key
            assert any(key.startswith(f"{parent}/node_modules/") for parent in entries if parent not in bundled), key
            continue
        assert str(entry.get("resolved", "")).startswith(REGISTRY), (key, entry.get("resolved"))
        assert str(entry.get("integrity", "")).startswith("sha512-"), key


def test_the_lockfile_pins_the_versions_package_json_names(package, lock):
    for name, version in dependencies(package).items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name


# ---------------------------------------------------------------- the five Node versions

def web_job_node(workflow) -> str:
    step = next(s for s in workflow["jobs"]["web"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-node"))
    spec = step["with"]
    if "node-version-file" in spec:
        return (ROOT / spec["node-version-file"]).read_text(encoding="utf-8").strip()
    return str(spec["node-version"])


def test_the_node_version_file_names_the_pinned_version():
    assert (WEB / ".node-version").read_text(encoding="utf-8").strip() == NODE_VERSION


def test_the_five_places_that_name_a_node_version_agree(workflow):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    tag = re.search(r"^FROM node:(\S+?)-slim@sha256:[0-9a-f]{64} AS web$", dockerfile, re.M)
    assert tag, "the web stage must be FROM node:<exact version>-slim@sha256:<digest> AS web"
    setup = [str(step["with"]["node-version"]) for job in ("unit", "serve-shipped") for step in workflow["jobs"][job]["steps"]
             if str(step.get("uses", "")).startswith("actions/setup-node")]
    assert len(setup) == 2, setup
    found = {"web/.node-version": (WEB / ".node-version").read_text(encoding="utf-8").strip(), "Dockerfile": tag.group(1),
             "web job": web_job_node(workflow), "unit job": setup[0], "serve-shipped job": setup[1]}
    assert set(found.values()) == {NODE_VERSION}, found


# ---------------------------------------------------------------- the CI web job

def test_no_job_still_uses_setup_node_v4():
    """Every ``uses:`` is a commit SHA now, so the version is read from its ``# vX.Y.Z`` comment."""
    nodes = [m for m in uses_lines() if m["ref"].startswith("actions/setup-node@")]
    assert len(nodes) == 3, [m["ref"] for m in nodes]                      # the unit, serve-shipped and web jobs
    assert "actions/setup-node@v4" not in [m["ref"] for m in nodes]
    assert all(int(re.match(r"\s*#\s*v(\d+)\.", m["rest"]).group(1)) >= 7 for m in nodes), [m["rest"] for m in nodes]


def web_steps(workflow) -> list[dict]:
    return workflow["jobs"]["web"]["steps"]


def run_text(workflow) -> str:
    return "\n".join(str(s.get("run", "")) for s in web_steps(workflow))


def test_the_web_job_installs_only_with_npm_ci_and_no_scripts(workflow):
    text = run_text(workflow)
    assert "npm ci --ignore-scripts" in text
    assert not re.search(r"npm (install|i|add|update)\b", text)
    assert all(step.get("working-directory", "web") == "web" for step in web_steps(workflow) if "npm" in str(step.get("run", "")))


def test_the_web_job_runs_the_spec_steps_in_order(workflow):
    text = run_text(workflow)
    order = ["npm ci --ignore-scripts", "npm run lint", "npm run typecheck", "npm run test:coverage", "npm run audit",
             "npm run build", "tests/test_dist_contract.py", "playwright install", "npm run e2e", "npm run lighthouse",
             "docker build --target web"]
    positions = [text.find(command) for command in order]
    assert all(p >= 0 for p in positions), dict(zip(order, positions))
    assert positions == sorted(positions), dict(zip(order, positions))


def test_the_web_job_never_lets_the_dist_contract_skip(workflow):
    step = next(s for s in web_steps(workflow) if "tests/test_dist_contract.py" in str(s.get("run", "")))
    run = step["run"]
    assert re.search(r"^\s*!\s*grep -E \"\^SKIPPED\"", run, re.M), "a skipped dist contract would be vacuous: fail on it"
    assert "-rs" in run


def test_the_web_job_has_the_python_setup_the_fixture_server_needs(workflow):
    steps = web_steps(workflow)
    setup_uv = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert len(setup_uv) == 1, setup_uv
    text = run_text(workflow)
    assert "uv python install 3.13" in text and "uv sync --extra serve --extra agent" in text
    assert setup_uv[0] < max(i for i, s in enumerate(steps) if "uv sync" in str(s.get("run", "")))


def test_the_web_job_runs_on_the_same_triggers_and_is_not_gated_by_another_job(workflow):
    job = workflow["jobs"]["web"]
    assert job["runs-on"] == "ubuntu-latest" and "needs" not in job and "if" not in job


def test_the_job_names_in_the_workflow_still_hold_the_existing_jobs(workflow):
    assert {"unit", "deepeval-wrapper", "serve-shipped", "neo4j-state", "security-scan", "web"} <= set(workflow["jobs"])


# ---------------------------------------------------------------- workflow hardening (I1 security review: S10, S13)

def uses_lines() -> list[re.Match[str]]:
    """Each ``uses:`` line of the workflow file as text (the YAML parser drops the trailing comment this needs)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return [m for line in text.splitlines() if (m := USES_LINE.match(line))]


def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(workflow):
    """A tag is a movable pointer: a compromised or force-moved ``v4`` would run attacker code in a job that holds the
    token. The 40-hex commit cannot move, and ``# vX.Y.Z`` is what a reviewer (and the next bump) reads it as."""
    found = uses_lines()
    parsed = [str(s["uses"]) for job in workflow["jobs"].values() for s in job["steps"] if "uses" in s]
    assert len(parsed) >= 16, "the workflow lost an action step"
    assert [m["ref"] for m in found] == parsed, "a `uses:` line the text scan and the YAML parse disagree on"
    for m in found:
        assert SHA_PIN.match(m["ref"]), f"{m['ref']}: pin it to a full 40-hex commit SHA, not a tag or branch"
        assert VERSION_COMMENT.match(m["rest"]), f"{m['ref']}: add a trailing `# vX.Y.Z` comment, got {m['rest']!r}"


def test_one_action_has_the_same_pin_in_every_job():
    pins: dict[str, set[tuple[str, str]]] = {}
    for m in uses_lines():
        action, sha = m["ref"].split("@")
        pins.setdefault(action, set()).add((sha, m["rest"].strip()))
    assert pins, "no `uses:` line found"
    assert {action: sorted(p) for action, p in pins.items() if len(p) > 1} == {}


def test_the_workflow_token_is_read_only(workflow):
    assert workflow.get("permissions") == {"contents": "read"}, workflow.get("permissions")
    widened = {name: job["permissions"] for name, job in workflow["jobs"].items() if "write" in str(job.get("permissions", ""))}
    assert widened == {}, "a job that needs a write scope states it and says why"


def test_every_checkout_leaves_no_token_in_the_git_config(workflow):
    checkouts = [(name, step) for name, job in workflow["jobs"].items() for step in job["steps"]
                 if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) >= 6, [name for name, _ in checkouts]
    for name, step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, name


def test_playwright_never_reuses_a_server_that_is_already_on_the_port():
    """S13: a server already listening there (a developer's real-settings app) must never stand in for the guarded fixture
    server, locally or in CI, so every web server (preview, flipped, and the site-key server of the Turnstile specs) states ``reuseExistingServer: false`` (no ``!process.env.CI`` either)."""
    lines = (WEB / "playwright.config.ts").read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("//"))
    assert len(re.findall(r"\bcommand:", code)) == 3, "a web server was added: it needs the same pin"
    assert re.findall(r"\breuseExistingServer:\s*([^,}\s]+)", code) == ["false", "false", "false"]
