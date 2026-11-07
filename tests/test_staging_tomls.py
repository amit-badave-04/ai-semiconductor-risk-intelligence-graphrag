"""deploy/staging/*.toml and Dockerfile.tools: what a staging app definition may and may not contain (M5a I5).

The staging fleet is internet-reachable (behind a secret header), throwaway and cheap to get wrong, so the definitions are
checked as text AND as parsed data:

* no secret anywhere: not a key in ``[env]`` whose name looks like one, and not one of the known secret names (or
  ``SEC_USER_AGENT``, which names a real person) in the raw text, comments included, so a grep-based review cannot trip on it;
* no live host: every ``*.internal`` / ``*.fly.dev`` name in any file is a registered staging app, never ``semigraph`` or
  ``semigraph-neo4j``; the app of each file is a registered staging app;
* the stg API: ``ENVIRONMENT=staging``, the mock models, the mock's private base URL naming the app that ``fly.mockllm.toml``
  defines, the staging database host naming the app that ``fly.neo4j-stg.toml`` defines, ``hard_limit`` above the 120
  concurrent streams of the gate (and above the soft limit), ``kill_signal`` / ``kill_timeout`` at the top level above the
  first table, and every key a real ``Settings`` field (``Settings`` ignores an unknown variable, so a typo would be silent);
* no staging toml carries a ``[[vm]]`` block (it would win over ``flyctl deploy --vm-size``): the machine class of every app
  comes from ``deploy/staging/windows.json`` only (the quote's arithmetic and the deploy flags), and every app of every window
  has a priced machine entry there;
* ``fly.w1b.toml`` (window W1b): the stg API with the live memory settings (fp32 model path, ``EMBED_SLOTS`` and
  ``MAX_CONCURRENT_ANSWERS`` equal to the code defaults that ``fly.toml`` does not override, no upload cap set, live swap, drain
  budget and proxy limits), differing from ``fly.stg.toml`` in nothing else;
* no PDF library in any staging file.
"""

import json
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "deploy" / "staging"
WINDOWS = json.loads((STAGING / "windows.json").read_text(encoding="utf-8"))
APP_NAMES = {entry["name"] for entry in WINDOWS["apps"].values()}
LIVE_APPS = ("semigraph", "semigraph-neo4j")

TOMLS = sorted(STAGING.glob("*.toml"))
TOML_BY_NAME = {path.name: tomllib.loads(path.read_text(encoding="utf-8")) for path in TOMLS}
SECRET_NAMES = (
    "SEC_USER_AGENT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NEO4J_PASSWORD", "NEO4J_AUTH", "ADMIN_TOKEN", "IP_HASH_PEPPER",
    "TURNSTILE_SECRET_KEY", "ORIGIN_AUTH_SECRET", "LANGFUSE_SECRET_KEY", "EMBEDDING_API_KEY", "MOCKLLM_ADMIN_TOKEN",
    "LANGFUSE_HASH_SALT", "LANGFUSE_PUBLIC_KEY")
SECRET_LOOKING_KEY = re.compile(r"(SECRET|PASSWORD|PEPPER|API_KEY|USER_AGENT|CREDENTIAL|(^|_)TOKEN$|(^|_)AUTH($|_))",
                                re.IGNORECASE)
KEY_PREFIX = re.compile(r"^\s*\w+\s*=", re.MULTILINE)                  # `NAME =` of a table entry is a name, not a value
HOST_RE = re.compile(r"(?<![\w.-])([a-z][a-z0-9-]*)\.(?:internal|fly\.dev)\b")
# The two secrets the staging validators require and the toml must not hold (scripts/staging.py generates and pushes them per
# window). Obviously fake, built here so the test can say what a staging machine reads back once they are in its environment.
MOCK_KEY = "mock-key-for-the-toml-test-0123456789"                    # gitleaks:allow
ORIGIN_SECRET = "origin-secret-for-the-toml-test-0123456789-abcdef"     # gitleaks:allow (40+ bytes: the staging rule is 32)


def env_of(name: str) -> dict:
    return TOML_BY_NAME[name].get("env", {})


def test_the_staging_definitions_exist():
    assert {"fly.stg.toml", "fly.neo4j-stg.toml", "fly.mockllm.toml", "fly.tools.toml"} <= set(TOML_BY_NAME)
    assert (STAGING / "Dockerfile.tools").is_file() and (STAGING / "windows.json").is_file()


# --- no secret, no person, no live host ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path", TOMLS, ids=lambda p: p.name)
def test_no_secret_in_env_and_no_secret_name_in_the_text(path):
    for key in TOML_BY_NAME[path.name].get("env", {}):
        assert not SECRET_LOOKING_KEY.search(key), f"{path.name}: [env] {key} looks like a secret"
    raw = path.read_text(encoding="utf-8")
    for name in SECRET_NAMES:
        assert name not in raw.upper(), f"{path.name} names {name}"
    values = KEY_PREFIX.sub("", re.sub(r"https?://\S+", "", raw))
    assert not re.search(r"[A-Za-z0-9_-]{40,}", values), f"{path.name} holds a long opaque string"


@pytest.mark.parametrize("path", [*TOMLS, STAGING / "Dockerfile.tools", STAGING / "windows.json"], ids=lambda p: p.name)
def test_no_pdf_library_and_no_secret_name_in_any_staging_definition(path):
    raw = path.read_text(encoding="utf-8")
    assert "pymupdf" not in raw.lower() and "fitz" not in raw.lower()
    for name in ("SEC_USER_AGENT", "ANTHROPIC_API_KEY"):
        assert name not in raw


@pytest.mark.parametrize("path", [*TOMLS, STAGING / "Dockerfile.tools", STAGING / "windows.json"], ids=lambda p: p.name)
def test_every_host_named_is_a_registered_staging_app_never_a_live_one(path):
    raw = path.read_text(encoding="utf-8")
    hosts = set(HOST_RE.findall(raw))
    assert hosts <= APP_NAMES, f"{path.name} names {sorted(hosts - APP_NAMES)}"
    assert not hosts & set(LIVE_APPS)
    assert not re.search(r"fly-client-ip", raw, re.IGNORECASE) or path.name == "windows.json"


@pytest.mark.parametrize("path", TOMLS, ids=lambda p: p.name)
def test_the_app_of_each_file_is_a_registered_staging_app(path):
    app = TOML_BY_NAME[path.name]["app"]
    assert app in APP_NAMES and app not in LIVE_APPS


def test_the_registry_never_lists_a_live_app_and_every_staging_app_is_not_named_like_one():
    assert not APP_NAMES & set(LIVE_APPS)
    assert all(name.startswith("semigraph-") for name in APP_NAMES)


# --- the staging API ------------------------------------------------------------------------------------------------------------

def test_the_stg_api_is_staging_with_the_mock_models_and_stubbed_bot_check():
    env = env_of("fly.stg.toml")
    assert env["ENVIRONMENT"] == "staging"
    assert env["CLIENT_IP_HEADER"] == "x-test-client-ip"
    assert env["TURNSTILE_REQUIRED"] == "true" and env["TURNSTILE_STUB"] == "true"
    assert env["FRESHNESS_ENABLED"] == "false"
    for key in ("ANSWER_MODEL", "ESCALATION_MODEL", "AGENT_PLANNER_MODEL"):
        assert env[key].startswith("openai/mock-"), key
    assert int(env["EMBED_SLOTS"]) >= 2
    assert int(env["MAX_CONCURRENT_ANSWERS"]) >= 120
    assert env["STATE_BACKEND"] in ("inprocess", "neo4j")


def test_the_stg_api_points_at_the_mock_and_the_staging_database_defined_beside_it():
    env = env_of("fly.stg.toml")
    mock_app = TOML_BY_NAME["fly.mockllm.toml"]["app"]
    db_app = TOML_BY_NAME["fly.neo4j-stg.toml"]["app"]
    assert env["OPENAI_API_BASE"] == f"http://{mock_app}.internal:{env_of('fly.mockllm.toml')['MOCKLLM_PORT']}/v1"
    assert env["NEO4J_URI"] == f"bolt://{db_app}.internal:7687"
    assert db_app == "semigraph-neo4j-stg"                     # the host the staging validator allows (plan item 4.1)
    assert TOML_BY_NAME["fly.stg.toml"]["app"] == "semigraph-stg"


def test_every_paid_ask_cap_is_switched_off_in_the_stg_api_and_can_be_put_back_by_deploy_set():
    """The staging caps are raised for the load model; the production-settings sub-run puts them back with
    ``scripts/staging.py deploy --set``, so every cap the toml zeroes must be a name ``--set`` accepts."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import staging
    env = env_of("fly.stg.toml")
    caps = {key for key in env if key.startswith(("MAX_QUERIES", "MAX_SPEND", "PAID_"))}
    assert {"MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_PER_IP_PER_DAY", "PAID_SPEND_SHARE_PER_IP_USD"} <= caps
    assert all(float(env[key]) == 0 for key in caps), {key: env[key] for key in caps}
    assert caps <= staging.SETTABLE, sorted(caps - staging.SETTABLE)
    assert env["PAID_SPEND_SHARE_PER_IP_USD"] == "0" and list(env).index("PAID_SPEND_SHARE_PER_IP_USD") == list(env).index(
        "PAID_PER_IP_PER_DAY") + 1                                                    # next to the per-address count cap


def test_the_staging_images_that_are_not_the_api_carry_no_settings_to_build():
    """The production config refuses an app name it does not know, so a staging app must not build a ``Settings`` unless its
    name is registered (``FLY_APP_NAME`` is set by Fly on every machine). The mock and the generator ship no ``semigraph``
    code at all (the mock one file, the citation grammar); the tools image ships it and builds none (tests/test_tools_s7.py)."""
    for name, allowed in (("Dockerfile.mockllm", {"src/semigraph/retrieval/ids.py"}), ("Dockerfile.loadgen", set())):
        text = (STAGING / name).read_text(encoding="utf-8")
        copied_src = {line.split()[1] for line in text.splitlines() if line.startswith("COPY ") and line.split()[1].startswith("src")}
        assert copied_src == allowed, (name, copied_src)
        assert "COPY pyproject.toml" not in text and "--no-deps ." not in text, name       # the package itself is never installed


def test_the_proxy_limits_admit_the_gate_streams():
    concurrency = TOML_BY_NAME["fly.stg.toml"]["http_service"]["concurrency"]
    assert concurrency["type"] == "requests"
    assert concurrency["hard_limit"] > 120 and concurrency["hard_limit"] > concurrency["soft_limit"] > 0
    assert (concurrency["soft_limit"], concurrency["hard_limit"]) == (200, 400)


def test_kill_signal_and_timeout_are_top_level_above_the_first_table():
    for name in ("fly.stg.toml",):
        doc, raw = TOML_BY_NAME[name], (STAGING / name).read_text(encoding="utf-8")
        assert doc["kill_signal"] == "SIGTERM" and doc["kill_timeout"] == 300
        first_table = min(m.start() for m in re.finditer(r"^\[", raw, re.MULTILINE))
        assert raw.index("kill_signal") < first_table and raw.index("kill_timeout") < first_table


def test_every_stg_env_key_is_a_settings_field_so_a_typo_cannot_be_silent(monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    fields = {name.upper() for name in Settings.model_fields}
    unknown = set(env_of("fly.stg.toml")) - fields             # every staging switch is a real field now (TURNSTILE_STUB, OPENAI_API_BASE)
    assert not unknown, f"not Settings fields: {sorted(unknown)}"


def test_the_stg_env_builds_a_settings_that_reads_back_the_plan_values(monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import FLY_APP_ENVIRONMENTS, Settings
    for key in list(os.environ):
        if key.upper() in {k.upper() for k in Settings.model_fields}:
            monkeypatch.delenv(key, raising=False)
    for key, value in env_of("fly.stg.toml").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FLY_APP_NAME", "semigraph-stg")
    # the toml alone is not a bootable staging machine: its two secrets come from `scripts/staging.py` (fly secrets), and the
    # staging validators refuse a machine without them - by setting name, never by value
    with pytest.raises(ValueError) as refused:
        Settings(_env_file=None)
    assert "OPENAI_API_KEY" in str(refused.value) and "ORIGIN_AUTH_SECRET" in str(refused.value)
    monkeypatch.setenv("OPENAI_API_KEY", MOCK_KEY)
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", ORIGIN_SECRET)
    settings = Settings(_env_file=None)
    assert FLY_APP_ENVIRONMENTS["semigraph-stg"] == "staging" == settings.environment
    assert settings.turnstile_stub is True and settings.turnstile_required is True
    assert settings.openai_api_base == env_of("fly.stg.toml")["OPENAI_API_BASE"]
    assert ORIGIN_SECRET not in repr(settings) and MOCK_KEY not in repr(settings)
    assert settings.client_ip_header == "x-test-client-ip"
    assert settings.embed_slots == 2 and settings.max_concurrent_answers == 120
    assert (settings.max_queries_per_day, settings.paid_per_ip_per_day, settings.max_spend_usd_per_day) == (0, 0, 0.0)
    # the address's share of the day's spend is a cap too: left on, one test address would be refused (IP_SPEND) after about two
    # hybrid asks, and the gate's 1,000 virtual users would read as errors
    assert settings.paid_spend_share_per_ip_usd == 0
    assert settings.answer_model == "openai/mock-luna" and settings.escalation_model == "openai/mock-sonnet"
    assert settings.neo4j_uri == "bolt://semigraph-neo4j-stg.internal:7687"
    assert settings.freshness_enabled is False and settings.sec_user_agent == ""


# --- the W1b API: the staging API with the LIVE memory settings (council 7, decision 1, step 3) -----------------------------------

LIVE_TOML = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
W1B_ONLY = {"ONNX_MODEL_PATH", "EMBED_SLOTS", "MAX_CONCURRENT_ANSWERS", "MAX_UPLOADS_PER_DAY"}   # what differs from fly.stg.toml


def _live_default(setting: str):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    return Settings.model_fields[setting.lower()].default


def _fp32_model_path_the_dockerfile_builds() -> str:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pattern = re.search(r"ONNX_MODEL_PATH=(\S+)", text).group(1)
    assert "${EMBEDDER_VARIANT}" in pattern
    return pattern.replace("${EMBEDDER_VARIANT}", "fp32")


def test_the_w1b_w1c_and_w1d_windows_deploy_this_toml_for_their_api_and_no_other_window_does():
    """W1c is W1b with only the ask workload changed (council 8), so it deploys the same api config; W1d (the upload-ceiling
    confirmation) is W1c with no asks and no mock LLM, and deploys it too: it sets no UPLOAD_* so the code default ceiling applies."""
    assert WINDOWS["windows"]["W1b"]["toml_overrides"] == {"api": "deploy/staging/fly.w1b.toml"}
    assert [k for k, w in WINDOWS["windows"].items() if "toml_overrides" in w] == ["W1b", "W1c", "W1d"]
    assert WINDOWS["windows"]["W1c"]["toml_overrides"] == WINDOWS["windows"]["W1b"]["toml_overrides"]
    assert WINDOWS["windows"]["W1d"]["toml_overrides"] == WINDOWS["windows"]["W1b"]["toml_overrides"]
    assert TOML_BY_NAME["fly.w1b.toml"]["app"] == WINDOWS["apps"]["api"]["name"] == "semigraph-stg"


def test_the_w1b_api_serves_the_fp32_model_the_image_is_built_with():
    path = env_of("fly.w1b.toml")["ONNX_MODEL_PATH"]
    assert path == _fp32_model_path_the_dockerfile_builds() == "/srv/models/qwen3-embedding-0.6b-fp32/model_fp32.onnx"
    assert "q8" not in path
    assert WINDOWS["windows"]["W1b"]["build_args"]["api"] == {"require": ["EMBEDDER_VARIANT=fp32"], "refuse": ["KEEP_UNPATCHED=1"]}


@pytest.mark.parametrize("setting", ["EMBED_SLOTS", "MAX_CONCURRENT_ANSWERS"])
def test_the_w1b_concurrency_settings_are_the_live_ones_which_are_the_code_defaults(setting):
    assert setting not in LIVE_TOML["env"], f"fly.toml now sets {setting}: W1b must follow it"
    assert env_of("fly.w1b.toml")[setting] == str(_live_default(setting)) == {"EMBED_SLOTS": "1", "MAX_CONCURRENT_ANSWERS": "2"}[setting]
    assert setting not in {k for k in env_of("fly.neo4j-stg.toml")}


def test_the_w1b_api_sets_no_upload_cap_so_the_live_code_defaults_apply():
    env, live = env_of("fly.w1b.toml"), LIVE_TOML["env"]
    assert env["UPLOADS_ENABLED"] == live["UPLOADS_ENABLED"] == "true"
    assert not {key for key in env if key.startswith("UPLOAD_") and key != "UPLOADS_ENABLED"}
    assert "MAX_UPLOADS_PER_DAY" not in env and "MAX_UPLOADS_PER_DAY" not in live
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    assert _live_default("MAX_UPLOADS_PER_DAY") == 40 and Settings.model_fields["upload_max_pages"].default == 30


def test_the_w1b_api_differs_from_the_stg_api_only_in_the_memory_settings():
    stg, w1b = env_of("fly.stg.toml"), env_of("fly.w1b.toml")
    differing = {key for key in {*stg, *w1b} if stg.get(key) != w1b.get(key)}
    assert differing == W1B_ONLY, sorted(differing ^ W1B_ONLY)
    for key in ("ENVIRONMENT", "OPENAI_API_BASE", "NEO4J_URI", "CLIENT_IP_HEADER", "TURNSTILE_STUB", "ANSWER_MODEL", "ESCALATION_MODEL",
                "AGENT_PLANNER_MODEL", "FRESHNESS_ENABLED", "ONNX_THREADS", "LLM_ANSWER_MAX_TOKENS", "STATE_BACKEND", "AGENT_ENABLED"):
        assert w1b[key] == stg[key], key
    assert w1b["ONNX_THREADS"] == LIVE_TOML["env"]["ONNX_THREADS"] and w1b["LLM_ANSWER_MAX_TOKENS"] == LIVE_TOML["env"]["LLM_ANSWER_MAX_TOKENS"]
    assert all(float(w1b[key]) == 0 for key in w1b if key.startswith(("MAX_QUERIES", "MAX_SPEND", "PAID_")))


def test_the_w1b_api_has_the_live_swap_the_drain_budget_and_the_live_proxy_limits():
    doc = TOML_BY_NAME["fly.w1b.toml"]
    assert doc["swap_size_mb"] == LIVE_TOML["swap_size_mb"] == 1024
    assert (doc["kill_signal"], doc["kill_timeout"]) == (LIVE_TOML["kill_signal"], LIVE_TOML["kill_timeout"]) == ("SIGTERM", 300)
    assert doc["http_service"]["concurrency"] == LIVE_TOML["http_service"]["concurrency"] == {"type": "requests", "soft_limit": 20,
                                                                                                "hard_limit": 40}
    assert doc["checks"] == LIVE_TOML["checks"] and doc["primary_region"] == LIVE_TOML["primary_region"] == "sin"
    raw = (STAGING / "fly.w1b.toml").read_text(encoding="utf-8")
    first_table = min(m.start() for m in re.finditer(r"^\[", raw, re.MULTILINE))
    assert raw.index("swap_size_mb") < first_table and raw.index("kill_signal") < first_table


def test_every_w1b_env_key_is_a_settings_field_and_builds_the_live_shaped_settings(monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import Settings
    fields = {name.upper() for name in Settings.model_fields}
    assert not set(env_of("fly.w1b.toml")) - fields
    for key in list(os.environ):
        if key.upper() in fields:
            monkeypatch.delenv(key, raising=False)
    for key, value in env_of("fly.w1b.toml").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FLY_APP_NAME", "semigraph-stg")
    monkeypatch.setenv("OPENAI_API_KEY", MOCK_KEY)
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", ORIGIN_SECRET)
    settings = Settings(_env_file=None)
    assert settings.environment == "staging" and settings.turnstile_stub is True
    assert settings.onnx_model_path.name == "model_fp32.onnx" and settings.onnx_threads == 1
    assert (settings.embed_slots, settings.max_concurrent_answers) == (1, 2)
    assert (settings.upload_max_bytes, settings.upload_max_pages, settings.upload_max_tokens, settings.upload_max_chunks,
            settings.max_uploads_per_day) == (15 * 1024 * 1024, 30, 10000, 120, 40)
    assert settings.neo4j_uri == "bolt://semigraph-neo4j-stg.internal:7687"
    assert settings.openai_api_base == "http://semigraph-mockllm.internal:8000/v1"


# --- the machine class of each app is windows.json's, never the toml's ----------------------------------------------------------------

@pytest.mark.parametrize("path", TOMLS, ids=lambda p: p.name)
def test_no_staging_toml_carries_a_machine_size_so_the_windows_class_always_applies(path):
    """A ``[[vm]]`` block in the app's toml WINS over ``flyctl deploy --vm-size / --vm-memory`` (window W0 asked for
    shared-cpu-2x / 4 GB and got the toml's performance-1x / 2 GB). The class of a machine therefore comes from
    ``deploy/staging/windows.json`` only, through ``scripts/staging.py deploy``; the toml names no size. ``[[compute]]`` is
    flyctl's older spelling of the same table."""
    doc = TOML_BY_NAME[path.name]
    assert "vm" not in doc and "compute" not in doc, f"{path.name} carries a machine size"
    raw = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*\[\[?\s*(vm|compute)\s*\]\]?", raw, re.MULTILINE | re.IGNORECASE), path.name


@pytest.mark.parametrize("app_key, toml_name", [("api", "fly.stg.toml"), ("neo4j", "fly.neo4j-stg.toml"),
                                                ("mockllm", "fly.mockllm.toml"), ("tools", "fly.tools.toml"),
                                                ("loadgen", "fly.loadgen.toml")])
def test_each_apps_toml_names_the_registered_app(app_key, toml_name):
    assert TOML_BY_NAME[toml_name]["app"] == WINDOWS["apps"][app_key]["name"]


def test_every_app_of_every_window_has_a_priced_machine_entry_because_the_deploy_reads_its_class_from_there():
    for key, window in WINDOWS["windows"].items():
        machine_apps = {m["app"] for m in window["machines"]}
        assert set(window["apps"]) <= machine_apps, f"{key}: an app without a machine class (deploy would not know its size)"
        for machine in window["machines"]:
            assert machine["class"] in WINDOWS["rates_30_day_usd"], (key, machine)


def test_the_neo4j_stg_definition_has_the_live_database_settings_and_a_volume():
    env, live = env_of("fly.neo4j-stg.toml"), tomllib.loads((ROOT / "deploy" / "neo4j" / "fly.toml").read_text("utf-8"))["env"]
    assert env == live                                          # same tuning, so S7 measures the database live runs on
    assert TOML_BY_NAME["fly.neo4j-stg.toml"]["mounts"] == [{"source": "neo4j_data", "destination": "/data"}]
    assert "services" not in TOML_BY_NAME["fly.neo4j-stg.toml"] and "http_service" not in TOML_BY_NAME["fly.neo4j-stg.toml"]


@pytest.mark.parametrize("path", ["deploy/neo4j/fly.toml", "deploy/staging/fly.neo4j-stg.toml"])
def test_the_neo4j_definitions_give_the_database_time_to_stop_cleanly(path):
    """Fly's default stop (SIGINT, then the VM ends 5 s later) cut Neo4j off before "Stopped." on 2026-11-06, and the next
    boot had to recover the volume's journal (M5_DECISIONS 10.9): SIGTERM and a long enough kill timeout let it shut down."""
    config = tomllib.loads((ROOT / path).read_text("utf-8"))
    assert config.get("kill_signal") == "SIGTERM"
    assert 60 <= int(str(config.get("kill_timeout", "0")).rstrip("s")) <= 300


def test_the_mock_and_tools_apps_expose_nothing_publicly():
    for name in ("fly.mockllm.toml", "fly.tools.toml"):
        assert "services" not in TOML_BY_NAME[name] and "http_service" not in TOML_BY_NAME[name]


# --- Dockerfile.tools ----------------------------------------------------------------------------------------------------------------

def test_dockerfile_tools_is_the_runtime_stage_without_the_model_stage_and_runs_as_non_root():
    text = (STAGING / "Dockerfile.tools").read_text(encoding="utf-8")
    instructions = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert sum(line.startswith("FROM ") for line in instructions) == 1            # no model stage
    assert "deploy/requirements-serve.txt" in text and "COPY tools/s7 tools/s7" in text and "COPY tools/probe tools/probe" in text
    assert "build_onnx_embedder" not in text and "/models" not in text
    assert any(line.startswith("USER app") for line in instructions) and "ENV " in text
    assert instructions[-1] == 'CMD ["sleep", "infinity"]'
    assert not re.search(r"^\s*(ENV|ARG)\s+\w*(KEY|SECRET|TOKEN|PASSWORD)", text, re.MULTILINE | re.IGNORECASE)
