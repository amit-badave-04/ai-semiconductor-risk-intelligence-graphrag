"""The staging touch points in production code (M5a I5, docs/v2/M5_DECISIONS.md 2.1 "Staging rules").

The staging API (deploy/staging/fly.stg.toml) runs with the mock LLM, a stubbed bot check, an origin-header secret and the
staging database. Three things are pinned here:

* the VALIDATORS of ``semigraph.config``: a staging process refuses to boot unless it touches no live system (an
  allowlist: the staging database host, no provider key, the mock on a ``.internal`` name, mock models, a secret of 32
  bytes or more, no freshness monitor, a client-address header the load generator sets), and a production process refuses
  every staging switch, whatever its value (``TURNSTILE_STUB``, ``OPENAI_API_BASE``, a mock model, the staging database,
  ``ORIGIN_AUTH_SECRET``). Every refusal names the SETTING and never prints a value;
* the BOT CHECK stub (``guard.verify_turnstile(..., stub=True)``) and the route that passes it: any non-empty token passes
  with no call to Cloudflare, a missing token does not, and the stub never passes in production;
* the ORIGIN-HEADER middleware wiring in ``main.create_app``: wired only when the secret is set, ``/healthz`` exempt (and
  still carrying the embedder fields), everything else refused with a 403 before routing.

Only ``semigraph``, pydantic, FastAPI and the test fakes are imported: this file runs in the serve-shipped CI job.
"""

import asyncio
import contextlib
import importlib.util
import logging
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from serve_state_fakes import RouteSettings, build_route_app, fresh_drain  # noqa: F401 - fresh_drain: a fixture

from semigraph import config
from semigraph.config import Settings
from semigraph.serve import guard, main, routes
from semigraph.serve.asgi_middleware import OriginAuth
from semigraph.serve.limiters import make_limiters

ROOT = Path(__file__).resolve().parent.parent
FLY = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
STG = tomllib.loads((ROOT / "deploy" / "staging" / "fly.stg.toml").read_text(encoding="utf-8"))
LIVE_DB_HOST = tomllib.loads((ROOT / "deploy" / "neo4j" / "fly.toml").read_text(encoding="utf-8"))["app"] + ".internal"
STAGING_DB_URI = "bolt://semigraph-neo4j-stg.internal:7687"
MOCK_BASE = "http://semigraph-mockllm.internal:8000/v1"

PEPPER = "pepper-for-the-staging-tests-0123456789-abcdef"                  # gitleaks:allow
ADMIN_TOKEN = "admin-token-for-the-staging-tests-0123456789-abc"          # gitleaks:allow
TURNSTILE_SECRET = "turnstile-secret-for-the-staging-tests-01234"         # gitleaks:allow
ORIGIN_SECRET = "origin-secret-for-the-staging-tests-0123456789-xyz"      # gitleaks:allow
MOCK_KEY = "mock-key-for-the-staging-tests-0123456789"                    # gitleaks:allow
LIVE_SECRETS = {"ip_hash_pepper": PEPPER, "admin_token": ADMIN_TOKEN, "turnstile_secret_key": TURNSTILE_SECRET}
STAGING_SECRETS = {"ip_hash_pepper": PEPPER, "admin_token": ADMIN_TOKEN, "origin_auth_secret": ORIGIN_SECRET,
                   "openai_api_key": MOCK_KEY}

# Every setting a staging or a production refusal of these switches can name (`\bNAME\b`: no name contains another).
STAGING_NAMES = config.STAGING_CHECKED_SETTINGS
PRODUCTION_SWITCHES = ("TURNSTILE_STUB", "OPENAI_API_BASE", "OPENAI_BASE_URL", "ORIGIN_AUTH_SECRET", "NEO4J_URI", "LLM_MODEL",
                       "ANSWER_MODEL", "ESCALATION_MODEL", "CRITIC_MODEL", "ADJUDICATION_MODEL", "AGENT_PLANNER_MODEL")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell (or exported by Fly) must neither fail nor satisfy these tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def live(**changes) -> Settings:
    """What the live machine builds: ``fly.toml [env]``, its app name, and the secrets it holds (fakes here)."""
    env = {key.lower(): value for key, value in FLY["env"].items()}
    return Settings(_env_file=None, **{**env, "fly_app_name": FLY["app"], **LIVE_SECRETS, **changes})


def staging(**changes) -> Settings:
    """What the staging API builds: ``fly.stg.toml [env]``, its app name, and the secrets ``scripts/staging.py`` generates
    for a window (fakes here)."""
    env = {key.lower(): value for key, value in STG["env"].items()}
    return Settings(_env_file=None, **{**env, "fly_app_name": STG["app"], **STAGING_SECRETS, **changes})


def named(refused: pytest.ExceptionInfo, names=STAGING_NAMES) -> list[str]:
    text = str(refused.value)
    return [name for name in names if re.search(rf"\b{name}\b", text)]


# ---------------------------------------------------------------- the staging shape boots

def test_the_staging_definition_and_the_secrets_a_window_generates_boot_as_staging():
    s = staging()
    assert s.environment == "staging" and s.fly_app_name == STG["app"] == "semigraph-stg"
    assert not s.is_production and s.turnstile_stub is True and s.turnstile_required is True
    assert s.openai_api_base == MOCK_BASE and s.neo4j_uri == STAGING_DB_URI
    assert (s.answer_model, s.escalation_model, s.agent_planner_model) == ("openai/mock-luna", "openai/mock-sonnet",
                                                                           "openai/mock-luna")
    assert s.client_ip_header == "x-test-client-ip" and s.freshness_enabled is False and s.anthropic_api_key == ""


def test_the_staging_names_are_settings_fields_and_the_switches_default_to_off():
    assert all(name.lower() in Settings.model_fields for name in (*config.STAGING_CHECKED_SETTINGS, *config.CHECKED_SETTINGS))
    s = Settings(_env_file=None)
    assert (s.turnstile_stub, s.origin_auth_secret, s.openai_api_base, s.openai_api_key) == (False, "", "", "")
    assert s.openai_base_url == ""


@pytest.mark.parametrize("name", ["origin_auth_secret", "openai_api_key"])
def test_the_two_new_secrets_stay_out_of_the_repr(name):
    assert Settings.model_fields[name].repr is False
    text = repr(staging()) + str(staging())
    assert ORIGIN_SECRET not in text and MOCK_KEY not in text


@pytest.mark.parametrize("app", sorted(config.FLY_APP_ENVIRONMENTS))
def test_every_registered_app_boots_as_the_environment_it_is_registered_for(app):
    environment = config.FLY_APP_ENVIRONMENTS[app]
    if environment == "production":
        assert live().fly_app_name == app
    else:
        assert staging(fly_app_name=app).environment == "staging"


def test_the_staging_apps_that_run_this_codes_python_are_registered_and_their_database_is_not():
    """The mock, the S7 machine and the load generators are the apps of deploy/staging that run our Python; the database
    app runs none, and is only ever a HOST NAME (STAGING_NEO4J_HOST)."""
    apps = {path.name: tomllib.loads(path.read_text(encoding="utf-8"))["app"]
            for path in (ROOT / "deploy" / "staging").glob("fly.*.toml")}
    assert apps["fly.stg.toml"] == "semigraph-stg" and apps["fly.neo4j-stg.toml"] + ".internal" == config.STAGING_NEO4J_HOST
    for name, app in apps.items():
        if name == "fly.neo4j-stg.toml":
            assert app not in config.FLY_APP_ENVIRONMENTS
        else:
            assert config.FLY_APP_ENVIRONMENTS[app] == "staging", name
    assert config.FLY_APP_ENVIRONMENTS["semigraph"] == "production"


# ---------------------------------------------------------------- staging refuses what could reach a live system

@pytest.mark.parametrize("uri", [
    f"bolt://{LIVE_DB_HOST}:7687",                                   # the live database
    "bolt://localhost:7687", "bolt://127.0.0.1:7687", "bolt://[::1]:7687", "bolt://[fdaa::3]:7687",
    "bolt://semigraph-neo4j-stg.internal.evil.example:7687",         # the allowed name as a prefix of another host
    "bolt://evil-semigraph-neo4j-stg.internal:7687",
    f"bolt://semigraph-neo4j-stg.internal@{LIVE_DB_HOST}:7687",      # the allowed name as user information
    f"bolt://user:pw@semigraph-neo4j-stg.internal:7687",
    f"bolt://semigraph-neo4j-stg.internal\\@{LIVE_DB_HOST}:7687",
    "bolt://semigraph-neo4j-stg.internal :7687", "bolt://semigraph-neo4j-stg.internal\n", " bolt://semigraph-neo4j-stg.internal",
    "bolt://", "", "semigraph-neo4j-stg.internal", "bolt://[unclosed:7687"])
def test_staging_refuses_any_database_host_but_the_staging_one(uri):
    with pytest.raises(ValidationError) as refused:
        staging(neo4j_uri=uri)
    assert named(refused) == ["NEO4J_URI"]


@pytest.mark.parametrize("uri", [STAGING_DB_URI, "bolt://semigraph-neo4j-stg.internal", "neo4j://SEMIGRAPH-NEO4J-STG.INTERNAL:7687",
                                 "bolt+s://semigraph-neo4j-stg.internal:7687", "bolt://semigraph-neo4j-stg.internal:notaport"])
def test_staging_accepts_the_staging_database_by_host(uri):
    """Compared by host, with the standard parser: the port is the driver's business (a bad port is its error)."""
    assert staging(neo4j_uri=uri).neo4j_uri == uri


def test_staging_takes_the_live_hosts_name_from_the_live_definition_not_from_this_file():
    assert LIVE_DB_HOST == "semigraph-neo4j.internal" and LIVE_DB_HOST != config.STAGING_NEO4J_HOST


@pytest.mark.parametrize("key", ["sk-ant-api03-not-a-real-key", "x", "sk-ant-"])  # gitleaks:allow
def test_staging_refuses_a_provider_key_of_anthropic(key):
    with pytest.raises(ValidationError) as refused:
        staging(anthropic_api_key=key)
    assert named(refused) == ["ANTHROPIC_API_KEY"]


@pytest.mark.parametrize("key", ["", "sk-proj-not-a-real-key", "Mock-key", " mock-key", "mock", "xmock-key", "openai-mock-"])
def test_staging_accepts_only_the_mocks_key(key):
    with pytest.raises(ValidationError) as refused:
        staging(openai_api_key=key)
    assert named(refused) == ["OPENAI_API_KEY"]


@pytest.mark.parametrize("base", [
    "", "https://api.openai.com/v1", "http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[fdaa::3]:8000/v1",
    "http://mock.internal.evil.example/v1", "http://evil-internal/v1", "http://internal/v1", "http://.internal/v1",
    "http://semigraph-mockllm.internal@api.openai.com/v1", "http://user:pw@semigraph-mockllm.internal/v1",
    "ftp://semigraph-mockllm.internal/v1", "semigraph-mockllm.internal:8000", "http://semigraph-mockllm.internal /v1",
    "http://[unclosed/v1"])
def test_staging_refuses_a_model_base_that_is_not_a_private_internal_http_address(base):
    with pytest.raises(ValidationError) as refused:
        staging(openai_api_base=base)
    assert named(refused) == ["OPENAI_API_BASE"]


@pytest.mark.parametrize("base", [MOCK_BASE, "https://mock.internal", "http://MOCK.INTERNAL:8000/v1", "http://a.b.internal/v1"])
def test_staging_accepts_a_model_base_on_a_private_internal_host(base):
    assert staging(openai_api_base=base).openai_api_base == base


@pytest.mark.parametrize("setting", ["answer_model", "escalation_model", "agent_planner_model"])
@pytest.mark.parametrize("model", ["openai/gpt-6-luna", "anthropic/claude-sonnet-5", "", "openai/mock", "openai/mock-", "OpenAI/mock-luna",
                                   "mock-luna", "xopenai/mock-luna", "gemini/mock-x"])
def test_staging_refuses_any_serve_model_that_is_not_a_mock(setting, model):
    with pytest.raises(ValidationError) as refused:
        staging(**{setting: model})
    assert named(refused) == [setting.upper()]


def test_staging_accepts_any_mock_name_for_the_serve_models():
    s = staging(answer_model="openai/mock-anything", escalation_model="openai/mock-x", agent_planner_model="openai/mock-1")
    assert s.answer_model == "openai/mock-anything"


@pytest.mark.parametrize("secret", ["", "a", "a" * 31, "é" * 15])
def test_staging_refuses_an_origin_secret_under_32_bytes(secret):
    with pytest.raises(ValidationError) as refused:
        staging(origin_auth_secret=secret)
    assert named(refused) == ["ORIGIN_AUTH_SECRET"]


@pytest.mark.parametrize("secret", ["a" * 32, "é" * 16, ORIGIN_SECRET])
def test_staging_counts_the_origin_secret_in_bytes(secret):
    assert staging(origin_auth_secret=secret).origin_auth_secret == secret


def test_staging_refuses_the_freshness_monitor():
    with pytest.raises(ValidationError) as refused:
        staging(freshness_enabled=True)
    assert named(refused) == ["FRESHNESS_ENABLED"]


@pytest.mark.parametrize("header", ["x-forwarded-for", "", "x-real-ip", "cf-connecting-ip"])
def test_staging_refuses_a_client_address_header_nobody_vouches_for(header):
    with pytest.raises(ValidationError) as refused:
        staging(client_ip_header=header)
    assert named(refused) == ["CLIENT_IP_HEADER"]


@pytest.mark.parametrize("header,stored", [("x-test-client-ip", "x-test-client-ip"), ("fly-client-ip", "fly-client-ip"),
                                           (" X-Test-Client-IP ", "x-test-client-ip")])
def test_staging_accepts_the_load_generators_header_and_flys(header, stored):
    assert staging(client_ip_header=header).client_ip_header == stored


def test_a_staging_refusal_names_every_problem_at_once():
    with pytest.raises(ValidationError) as refused:
        staging(neo4j_uri=f"bolt://{LIVE_DB_HOST}:7687", anthropic_api_key="sk-ant-x", openai_api_key="sk-x",
                openai_api_base="https://api.openai.com/v1", answer_model="openai/gpt-6-luna", escalation_model="",
                agent_planner_model="anthropic/x", origin_auth_secret="short", freshness_enabled=True,
                client_ip_header="x-forwarded-for")
    assert set(named(refused)) == set(STAGING_NAMES) - {"ENVIRONMENT", "FLY_APP_NAME"}


def test_the_staging_rules_apply_to_a_staging_process_anywhere_not_only_on_the_staging_app():
    """A local process with ENVIRONMENT=staging and no Fly app name is a staging process too."""
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, environment="staging")
    assert "NEO4J_URI" in named(refused) and "OPENAI_API_BASE" in named(refused)


def test_a_development_process_may_use_every_staging_switch_for_a_local_smoke_run():
    """tests/test_loadtest_local_smoke.py runs the mock LLM on 127.0.0.1 against a local app: no validator applies."""
    s = Settings(_env_file=None, environment="development", turnstile_stub=True, openai_api_base="http://127.0.0.1:9/v1",
                 answer_model="openai/mock-luna", origin_auth_secret="short", openai_api_key="mock-x")
    assert not s.is_production and s.turnstile_stub is True


# ---------------------------------------------------------------- production refuses every staging switch

def test_the_live_shape_still_boots_with_the_staging_fields_added():
    s = live()
    assert s.is_production and s.turnstile_stub is False and s.openai_api_base == "" and s.origin_auth_secret == ""
    assert not any(value.startswith("openai/mock-") for value in (s.answer_model, s.escalation_model, s.agent_planner_model))


@pytest.mark.parametrize("changes,setting", [
    ({"turnstile_stub": True}, "TURNSTILE_STUB"),
    ({"openai_api_base": MOCK_BASE}, "OPENAI_API_BASE"),
    ({"openai_api_base": "https://api.openai.com/v1"}, "OPENAI_API_BASE"),
    ({"openai_api_base": "http://localhost:8000"}, "OPENAI_API_BASE"),
    ({"openai_api_base": " "}, "OPENAI_API_BASE"),
    ({"openai_base_url": MOCK_BASE}, "OPENAI_BASE_URL"),
    ({"openai_base_url": "https://api.openai.com/v1"}, "OPENAI_BASE_URL"),
    ({"openai_base_url": " "}, "OPENAI_BASE_URL"),
    ({"answer_model": "openai/mock-luna"}, "ANSWER_MODEL"),
    ({"escalation_model": "openai/mock-sonnet"}, "ESCALATION_MODEL"),
    ({"agent_planner_model": "openai/mock-luna"}, "AGENT_PLANNER_MODEL"),
    ({"llm_model": "openai/mock-x"}, "LLM_MODEL"),
    ({"critic_model": "openai/mock-x"}, "CRITIC_MODEL"),
    ({"adjudication_model": "openai/mock-x"}, "ADJUDICATION_MODEL"),
    ({"answer_model": " OpenAI/Mock-Luna "}, "ANSWER_MODEL"),
    ({"neo4j_uri": STAGING_DB_URI}, "NEO4J_URI"),
    ({"neo4j_uri": "bolt://SEMIGRAPH-NEO4J-STG.internal"}, "NEO4J_URI"),
    ({"origin_auth_secret": ORIGIN_SECRET}, "ORIGIN_AUTH_SECRET"),
    ({"origin_auth_secret": "x"}, "ORIGIN_AUTH_SECRET"),
], ids=lambda value: str(value))
def test_production_refuses_each_staging_switch_and_names_only_that_setting(changes, setting):
    with pytest.raises(ValidationError) as refused:
        live(**changes)
    assert named(refused, PRODUCTION_SWITCHES) == [setting]


@pytest.mark.parametrize("variable", ["OPENAI_BASE_URL", "OPENAI_API_BASE"])
def test_production_refuses_a_model_base_held_in_the_process_environment(monkeypatch, variable):
    """LiteLLM reads both variables from ``os.environ`` itself, ``OPENAI_BASE_URL`` first (litellm/main.py), and the live
    calls pass no ``api_base`` of their own: a production process holding either would send every ``openai/`` call to that
    address. The live ``Settings`` is built from the process environment (nothing passes it keywords), so the environment is
    what must be refused, by name and without its value."""
    monkeypatch.setenv(variable, "http://sentinel-base-138.example/v1")
    with pytest.raises(ValidationError) as refused:
        live()
    assert named(refused, PRODUCTION_SWITCHES) == [variable]
    assert "sentinel-base-138" not in str(refused.value) and "input_value" not in str(refused.value)


@pytest.mark.parametrize("variable", ["OPENAI_BASE_URL", "OPENAI_API_BASE"])
def test_an_empty_model_base_in_the_process_environment_is_not_a_base(monkeypatch, variable):
    """An exported-but-empty variable is falsy for LiteLLM's ``or`` chain as well, so the live app still boots."""
    monkeypatch.setenv(variable, "")
    assert live().is_production


def test_the_model_base_url_may_hold_credentials_so_it_stays_out_of_the_repr():
    assert Settings.model_fields["openai_base_url"].repr is False
    assert "sentinel-url-138" not in repr(Settings(_env_file=None, openai_base_url="https://user:sentinel-url-138@host/v1"))


def test_the_live_database_and_the_live_models_are_not_refused():
    assert live(neo4j_uri=f"bolt://{LIVE_DB_HOST}:7687").neo4j_uri.endswith(":7687")
    assert live(answer_model="openai/gpt-6-luna", escalation_model="anthropic/claude-sonnet-5").is_production


def test_production_refuses_a_stubbed_bot_check_even_with_the_secret_set():
    """The stub is not a weaker mode of a configured check: the live machine refuses to boot with it at all."""
    with pytest.raises(ValidationError) as refused:
        live(turnstile_stub=True, turnstile_required=True)
    assert named(refused, PRODUCTION_SWITCHES) == ["TURNSTILE_STUB"]


def test_the_production_check_lists_the_switches_it_refuses_in_the_pre_deploy_report():
    assert set(PRODUCTION_SWITCHES) <= set(config.CHECKED_SETTINGS)
    assert len(set(config.CHECKED_SETTINGS)) == len(config.CHECKED_SETTINGS)
    # settings only a STAGING refusal names are not in the live report: it would print PASS for rules production lacks
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FRESHNESS_ENABLED"}.isdisjoint(config.CHECKED_SETTINGS)
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FRESHNESS_ENABLED"} <= set(config.STAGING_CHECKED_SETTINGS)


def test_the_pre_deploy_check_reads_each_refusal_as_one_setting_with_its_reason():
    """scripts/check_env_fly.py splits a refusal into ``{SETTING: reason}``: every problem must start with its setting."""
    spec = importlib.util.spec_from_file_location("check_env_fly_for_staging", ROOT / "scripts" / "check_env_fly.py")
    check_env_fly = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = check_env_fly
    spec.loader.exec_module(check_env_fly)

    with pytest.raises(ValidationError) as refused:
        live(turnstile_stub=True, openai_api_base=MOCK_BASE, openai_base_url=MOCK_BASE, answer_model="openai/mock-luna",
             neo4j_uri=STAGING_DB_URI, origin_auth_secret=ORIGIN_SECRET)
    reasons = check_env_fly.explain(refused.value)
    assert set(reasons) == {"TURNSTILE_STUB", "OPENAI_API_BASE", "OPENAI_BASE_URL", "ANSWER_MODEL", "NEO4J_URI",
                            "ORIGIN_AUTH_SECRET"}
    assert all(reason.startswith(name) for name, reason in reasons.items())

    with pytest.raises(ValidationError) as refused:
        staging(anthropic_api_key="sk-ant-x", freshness_enabled=True, client_ip_header="x-forwarded-for")
    assert set(check_env_fly.explain(refused.value)) == {"ANTHROPIC_API_KEY", "FRESHNESS_ENABLED", "CLIENT_IP_HEADER"}


# ---------------------------------------------------------------- no refusal prints a value

SENTINELS = {
    "production": {"openai_api_base": "http://sentinel-base-137.example/v1", "openai_base_url": "http://sentinel-url-137.example/v1",
                   "origin_auth_secret": "sentinel-origin-137",
                   "answer_model": "openai/mock-sentinel-137", "neo4j_uri": "bolt://semigraph-neo4j-stg.internal:7687/sentinel-137"},
    "staging": {"anthropic_api_key": "sentinel-anthropic-137", "openai_api_key": "sentinel-openai-137",  # gitleaks:allow
                "openai_api_base": "http://sentinel-base-137.example/v1", "origin_auth_secret": "sentinel-origin-137",
                "answer_model": "sentinel-model-137/x", "neo4j_uri": "bolt://sentinel-host-137.example:7687",
                "client_ip_header": "sentinel-header-137"},
}


def assert_no_value_printed(refused: pytest.ExceptionInfo, sentinels: dict) -> None:
    text = str(refused.value)
    assert "input_value" not in text
    values = [*sentinels.values(), PEPPER, ADMIN_TOKEN, TURNSTILE_SECRET, ORIGIN_SECRET, MOCK_KEY]
    assert [value for value in values if value in text] == []
    assert re.findall(r"sentinel-[\w.-]+-137", text) == []


def test_a_production_refusal_never_prints_a_value():
    with pytest.raises(ValidationError) as refused:
        live(turnstile_stub=True, **SENTINELS["production"])
    assert len(named(refused, PRODUCTION_SWITCHES)) == 6          # every switch was refused ...
    assert_no_value_printed(refused, SENTINELS["production"])    # ... and none of the values was printed


def test_a_staging_refusal_never_prints_a_value():
    with pytest.raises(ValidationError) as refused:
        staging(**SENTINELS["staging"])
    assert set(named(refused)) == {"NEO4J_URI", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "ANSWER_MODEL",
                                   "ORIGIN_AUTH_SECRET", "CLIENT_IP_HEADER"}
    assert_no_value_printed(refused, SENTINELS["staging"])


# ---------------------------------------------------------------- the bot check stub (guard.verify_turnstile)

def verify(token, *, secret="", is_production=False, required=True, **kwargs):
    return asyncio.run(guard.verify_turnstile(token, "203.0.113.7", secret, is_production, required=required, **kwargs))


@pytest.fixture
def no_cloudflare(monkeypatch):
    """Any call to Cloudflare (an httpx client) fails the test."""
    import httpx

    def refuse(*args, **kwargs):
        pytest.fail("the stubbed bot check must not call Cloudflare")

    monkeypatch.setattr(httpx, "AsyncClient", refuse)


@pytest.mark.parametrize("secret", ["", TURNSTILE_SECRET], ids=["no secret (staging)", "a secret"])
@pytest.mark.parametrize("token", ["any-token", "x", "0"])
def test_the_stub_passes_any_non_empty_token_without_calling_cloudflare(no_cloudflare, secret, token):
    assert verify(token, secret=secret, required=True, stub=True) is True


@pytest.mark.parametrize("token", [None, ""])
def test_the_stub_still_refuses_a_missing_token(no_cloudflare, token):
    assert verify(token, stub=True) is False


def test_the_stub_never_passes_in_production_even_if_the_validators_were_skipped(no_cloudflare, caplog):
    with caplog.at_level(logging.ERROR, logger=guard.logger.name):
        assert verify("any-token", is_production=True, stub=True) is False
    assert any("TURNSTILE_STUB" in record.getMessage() for record in caplog.records)


def test_without_the_stub_a_required_check_without_a_secret_still_fails_closed(no_cloudflare):
    assert verify("any-token", required=True) is False and verify("any-token", required=True, stub=False) is False


def test_the_stub_is_a_keyword_only_argument_so_no_caller_can_pass_it_by_position():
    with pytest.raises(TypeError):
        asyncio.run(guard.verify_turnstile("t", "ip", "", False, True, True))


# ---------------------------------------------------------------- the route passes the flag, and only the flag

class StubbedSettings(RouteSettings):
    turnstile_required = True
    turnstile_secret_key = ""
    turnstile_stub = True


class Recorder:
    """A ``verify_turnstile`` double: records how the route called it."""

    def __init__(self, answer=False):
        self.answer, self.calls = answer, []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.answer


def ask(settings, monkeypatch, recorder, token="any-token"):
    monkeypatch.setattr(guard, "verify_turnstile", recorder)
    with TestClient(build_route_app(settings=settings)) as client:
        return client.post("/api/ask", json={"question": "Which HBM suppliers does Nvidia depend on?", "turnstile_token": token})


def test_the_ask_route_passes_the_stub_flag_of_the_settings(monkeypatch, fresh_drain):
    recorder = Recorder()
    reply = ask(StubbedSettings(), monkeypatch, recorder)
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT
    (args, kwargs), = recorder.calls
    assert kwargs["stub"] is True and kwargs["required"] is True and args[0] == "any-token"


@pytest.mark.parametrize("settings", [RouteSettings(), type("NoStub", (RouteSettings,), {"turnstile_stub": False})()],
                         ids=["a settings object without the attribute", "the stub off"])
def test_a_settings_object_without_the_stub_never_turns_it_on(monkeypatch, fresh_drain, settings):
    """Test doubles of other suites carry no such attribute: a missing one must read as off, never raise."""
    recorder = Recorder()
    ask(settings, monkeypatch, recorder)
    (_, kwargs), = recorder.calls
    assert kwargs["stub"] is False


CID = "0001045810-26-000021:I.1:0320"


async def fake_answer_stream(question, driver, embedder, strategy="hybrid", **kw):
    yield {"event": "retrieval", "anchors": {"Nvidia": 1045810},
           "counts": {"edges": 2, "metrics": 1, "risks": 1, "temporal": 0, "chunks": 1}}
    yield {"event": "delta", "text": f"HBM suppliers [{CID}]."}
    yield {"event": "done", "answer": f"Nvidia depends on HBM suppliers [{CID}].", "citations": [CID], "hallucinated": [],
           "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.00007,
           "chunk_ids": [CID], "context_chars": 100, "strategy": strategy, "question": question}


def stubbed_staging_ask(monkeypatch, settings, token):
    """A paid ask through the real route and the REAL bot check, with only the answer writer faked."""
    from semigraph.serve import store

    monkeypatch.setattr(routes, "aanswer_stream", fake_answer_stream)
    monkeypatch.setattr(store, "log_query", lambda driver, **kw: None)
    monkeypatch.setattr(store, "get_policy", lambda driver, key: None)
    monkeypatch.setattr(store, "ledger_summary", lambda driver: {"today": {"paid": 0}})
    with TestClient(build_route_app(settings=settings)) as client:
        return client.post("/api/ask", json={"question": "Which HBM suppliers does Nvidia depend on?", "turnstile_token": token})


def test_a_staging_ask_with_any_token_passes_the_required_bot_check_that_has_no_secret(monkeypatch, fresh_drain, no_cloudflare):
    """The staging API requires the check and holds no secret (staging.py forbids TURNSTILE_SECRET_KEY): without the stub every
    ask would be a 403, with it a load generator's token passes."""
    reply = stubbed_staging_ask(monkeypatch, StubbedSettings(), "any-token")
    assert reply.status_code == 200 and "Nvidia depends on HBM suppliers" in reply.text       # the stream ran to its done event


def test_a_staging_ask_without_a_token_is_still_a_bot_check_failure(monkeypatch, fresh_drain, no_cloudflare):
    reply = stubbed_staging_ask(monkeypatch, StubbedSettings(), None)
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT


def test_the_same_ask_with_the_stub_off_is_refused_so_the_stub_is_what_lets_it_through(monkeypatch, fresh_drain):
    off = type("StubOff", (StubbedSettings,), {"turnstile_stub": False})()
    reply = stubbed_staging_ask(monkeypatch, off, "any-token")
    assert reply.status_code == 403 and reply.json()["detail"] == routes.MSG_BOT


# ---------------------------------------------------------------- main: the posture line and the origin gate

def test_the_stubbed_bot_check_is_logged_as_a_warning_not_as_a_fail_closed_error(caplog):
    with caplog.at_level(logging.INFO, logger=main.logger.name):
        main.log_bot_check_posture(SimpleNamespace(turnstile_required=True, turnstile_secret_key="", turnstile_stub=True,
                                                   is_production=False, max_queries_per_day=0, uploads_enabled=True))
    levels = {record.levelno for record in caplog.records}
    assert logging.ERROR not in levels and any("TURNSTILE_STUB" in r.getMessage() for r in caplog.records)


def test_a_required_check_without_a_secret_is_still_an_error_when_not_stubbed(caplog):
    with caplog.at_level(logging.INFO, logger=main.logger.name):
        main.log_bot_check_posture(SimpleNamespace(turnstile_required=True, turnstile_secret_key="", is_production=False,
                                                   max_queries_per_day=150, uploads_enabled=False))
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "TURNSTILE_REQUIRED without TURNSTILE_SECRET_KEY" in caplog.records[0].getMessage()


def build_app(monkeypatch, settings, embedder=None):
    """``main.create_app()`` over the settings, with the lifespan that connects to Neo4j replaced by one that makes the limiters."""
    embedder = embedder or SimpleNamespace(name="onnx:model_q8.onnx", variant="q8", fidelity=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()

    @contextlib.asynccontextmanager
    async def lifespan(application):
        application.state.limiters = make_limiters(SimpleNamespace(embed_slots=1, db_thread_limit=2))
        yield

    app.router.lifespan_context = lifespan
    app.state.driver, app.state.embedder = object(), embedder
    monkeypatch.setattr(routes, "run_cypher", lambda driver, query, **params: [{"ok": 1}])
    return app


def middleware_of(app) -> list[type]:
    return [entry.cls for entry in app.user_middleware]


def test_the_origin_gate_is_wired_only_when_the_secret_is_set(monkeypatch):
    assert middleware_of(build_app(monkeypatch, SimpleNamespace(origin_auth_secret=""))) == []
    assert middleware_of(build_app(monkeypatch, SimpleNamespace(origin_auth_secret=ORIGIN_SECRET))) == [OriginAuth]
    assert middleware_of(build_app(monkeypatch, SimpleNamespace())) == []          # settings without the attribute: off


def test_without_a_secret_the_app_answers_everyone_as_it_always_did(monkeypatch):
    app = build_app(monkeypatch, SimpleNamespace(origin_auth_secret=""))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/no-such-route").status_code == 404                  # reached routing: no 403 from a gate


def test_with_a_secret_every_request_but_healthz_needs_the_header(monkeypatch):
    app = build_app(monkeypatch, SimpleNamespace(origin_auth_secret=ORIGIN_SECRET))
    with TestClient(app) as client:
        refused = client.post("/api/ask", json={"question": "Which HBM suppliers does Nvidia depend on?"})
        assert refused.status_code == 403 and refused.json() == {"detail": "Forbidden"}
        assert client.get("/api/no-such-route").status_code == 403
        assert client.get("/api/no-such-route", headers={"X-Origin-Auth": "wrong" + ORIGIN_SECRET}).status_code == 403
        assert client.get("/api/no-such-route", headers={"X-Origin-Auth": ORIGIN_SECRET}).status_code == 404


def test_healthz_stays_open_and_still_carries_the_embedder_fields(monkeypatch):
    backend = SimpleNamespace(name="onnx:model_q8.onnx", variant="q8", fidelity=None)
    app = build_app(monkeypatch, SimpleNamespace(origin_auth_secret=ORIGIN_SECRET), embedder=backend)
    with TestClient(app) as client:
        reply = client.get("/healthz")
    assert reply.status_code == 200
    assert reply.json()["embedder_variant"] == "q8" and reply.json()["status"] == "ok"


def test_the_secret_is_never_logged_or_echoed_by_the_wiring(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        app = build_app(monkeypatch, SimpleNamespace(origin_auth_secret=ORIGIN_SECRET))
        with TestClient(app) as client:
            reply = client.get("/api/no-such-route", headers={"X-Origin-Auth": "nope"})
    assert ORIGIN_SECRET not in reply.text and ORIGIN_SECRET not in caplog.text and ORIGIN_SECRET not in repr(app.user_middleware)

