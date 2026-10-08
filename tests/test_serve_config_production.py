"""The production validators of ``semigraph.config`` (M5a closeout, Opus panel finding V2 M2 and the V3 deploy blocker).

A production process refuses to boot with a bot check that is off, a cap that is raised or zeroed (0 means "off"
elsewhere), a window that turns itself off, a weak admin token, an environment name that differs from the Fly app it
runs on, or a client-address header that does not name what the proxy sets. What is pinned here:

* every pin is "not looser than live": ``fly.toml [env]`` plus the code defaults it leaves alone must boot, and the one
  step past each pin must not (the old validators let every case below boot with ``ENVIRONMENT=production``);
* ``ENVIRONMENT`` and ``CLIENT_IP_HEADER`` are normalized (strip, lower case) once, in ``Settings``, so
  ``"production "`` cannot skip the validators and ``" fly-client-ip"`` cannot pass them while the guard fails to find
  the header;
* ``ENVIRONMENT`` is tied to ``FLY_APP_NAME``: the live app must be production, the staging app staging, anything else
  refuses to boot, and no Fly app name means a local process;
* every refusal names the SETTING, never its value, and one error names every problem (the pre-deploy check prints it).

Only ``semigraph.config``, ``semigraph.serve.estimate`` (the per-address share's two rules are computed from the live estimates,
not from a number) and pydantic are imported: this file runs in the serve-shipped CI job.
"""

import re
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from semigraph import config
from semigraph.config import Settings

ROOT = Path(__file__).resolve().parent.parent
FLY = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
PEPPER = "pepper-for-the-config-tests-0123456789-abcdef"                 # gitleaks:allow
TURNSTILE_SECRET = "turnstile-secret-for-the-config-tests-01234"         # gitleaks:allow
ADMIN_TOKEN = "admin-token-for-the-config-tests-0123456789-abc"          # gitleaks:allow
SECRETS = {"ip_hash_pepper": PEPPER, "turnstile_secret_key": TURNSTILE_SECRET, "admin_token": ADMIN_TOKEN}
# What a window's staging API boots with (deploy/staging/fly.stg.toml [env] plus the secrets scripts/staging.py generates; fakes
# here): a staging process is no longer "anything goes", it must satisfy the staging validators (tests/test_serve_config_staging.py).
STG = tomllib.loads((ROOT / "deploy" / "staging" / "fly.stg.toml").read_text(encoding="utf-8"))
STAGING_SECRETS = {"origin_auth_secret": "origin-secret-for-the-config-tests-0123456789-xyz",    # gitleaks:allow
                   "openai_api_key": "mock-key-for-the-config-tests-0123456789"}               # gitleaks:allow

# Every setting a production refusal can name. `\bNAME\b` keeps RATE_LIMIT_QUESTIONS apart from FREE_RATE_LIMIT_QUESTIONS.
PINNED = ("ENVIRONMENT", "FLY_APP_NAME", "IP_HASH_PEPPER", "ADMIN_TOKEN", "TURNSTILE_REQUIRED", "TURNSTILE_SECRET_KEY",
          "CLIENT_IP_HEADER", "MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_SPEND_SHARE_PER_IP_USD",
          "PAID_PER_IP_PER_DAY", "MAX_CONCURRENT_ANSWERS", "RATE_LIMIT_QUESTIONS", "FREE_RATE_LIMIT_QUESTIONS",
          "READ_RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_WINDOW_SECONDS", "WORKSPACE_CREATE_PER_DAY", "UPLOADS_PER_HOUR",
          "CACHE_READ_BUDGET_PER_S", "KILL_SWITCH_STALE_S",
          # the staging switches production refuses (tests/test_serve_config_staging.py pins each one)
          "TURNSTILE_STUB", "OPENAI_API_BASE", "OPENAI_BASE_URL", "ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL",
          "ORIGIN_AUTH_SECRET", "NEO4J_URI", "LLM_MODEL", "ANSWER_MODEL",
          "ESCALATION_MODEL", "CRITIC_MODEL", "ADJUDICATION_MODEL", "AGENT_PLANNER_MODEL")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell (or exported by Fly) must neither fail nor satisfy these tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def live(**changes) -> Settings:
    """What the live machine builds: ``fly.toml [env]``, its app name, and the secrets it holds (fakes here)."""
    env = {key.lower(): value for key, value in FLY["env"].items()}
    return Settings(_env_file=None, **{**env, "fly_app_name": FLY["app"], **SECRETS, **changes})


def staging_kwargs(**changes) -> dict:
    """What the staging API is given. ``fly_app_name`` is the staging app's unless a test changes it."""
    env = {key.lower(): value for key, value in STG["env"].items()}
    return {**env, "fly_app_name": STG["app"], **SECRETS, **STAGING_SECRETS, **changes}


def staging(**changes) -> Settings:
    return Settings(_env_file=None, **staging_kwargs(**changes))


def named(refused: pytest.ExceptionInfo) -> list[str]:
    text = str(refused.value)
    return [name for name in PINNED if re.search(rf"\b{name}\b", text)]


# ---------------------------------------------------------------- today's live shape still boots

def test_the_live_shape_boots_with_what_fly_toml_sets_and_the_secrets():
    s = live()
    assert s.is_production and s.environment == "production" and s.fly_app_name == "semigraph"
    assert s.turnstile_required is True


def test_turnstile_required_is_a_plain_fly_toml_entry_not_only_a_secret():
    """It is not secret, and the production validators refuse to boot without it: it belongs where the pre-deploy check
    and a reader of the repo can see it (docs/RUNBOOK.md)."""
    assert FLY["env"]["TURNSTILE_REQUIRED"] == "true"


@pytest.mark.parametrize("setting,pin", [
    ("rate_limit_questions", 5), ("free_rate_limit_questions", 30), ("read_rate_limit_per_minute", 120),
    ("workspace_create_per_day", 3), ("uploads_per_hour", 10)])
def test_a_window_is_pinned_at_what_the_live_machine_runs_today(setting, pin):
    """fly.toml sets none of these, so today's live value is the code default; the pin may not drift above it."""
    assert setting.upper() not in FLY["env"]
    assert getattr(live(), setting) == pin == Settings.model_fields[setting].default == config.PRODUCTION_COUNT_CEILINGS[setting]


def test_the_other_pins_are_what_the_live_machine_runs_today():
    s = live()
    assert (s.rate_limit_window_seconds, s.cache_read_budget_per_s, s.kill_switch_stale_s) == (600, 10, 30)
    assert (config.PRODUCTION_MIN_RATE_LIMIT_WINDOW_S, config.PRODUCTION_MAX_CACHE_READ_BUDGET_PER_S,
            config.PRODUCTION_MAX_KILL_SWITCH_STALE_S) == (600, 10, 30)


def test_the_per_address_spend_share_is_the_code_default_live_runs_and_is_what_production_allows_at_most():
    """fly.toml sets none of it, so what runs today is the code default; the production ceiling is the same number."""
    assert "PAID_SPEND_SHARE_PER_IP_USD" not in FLY["env"]
    assert (live().paid_spend_share_per_ip_usd == Settings.model_fields["paid_spend_share_per_ip_usd"].default
            == config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD == 1.32)


# The share's two rules (council 4, applied to the live estimates). Both are COMPUTED here, in whole micro-dollars, from
# ``serve.estimate`` and the production caps: when an estimate or a cap moves, the share is re-derived (config.py has the
# window) instead of a pinned number quietly going stale. tests/test_state_contract.py runs the same rules through every
# state backend.
DEMO_SETTLED_MICRO = 15 * 2_000 + 3 * 40_000 + 150_000     # council 4 test (b): settled spend when the office's 2nd agent ask arrives


def micro(usd: float) -> int:
    return round(usd * 1_000_000)


def live_estimates_micro() -> dict[str, int]:
    """What ``serve.estimate`` says each live ask can cost, for the configuration the live machine boots with."""
    from semigraph.serve import estimate
    settings = live()
    return {ask_type: estimate.estimate_micro(ask_type, settings) for ask_type in estimate.ASK_TYPES}


def test_after_seven_addresses_have_each_spent_a_full_share_the_rest_of_the_day_still_admits_a_hybrid_ask():
    """Rule (i'): pausing live asks takes at least eight addresses. Each address records at most its share, so what seven of
    them leave of the cap must hold the dearest ask a visitor makes most (the hybrid estimate). (The first form of this rule,
    ``7 x share < cap``, was met by $1.40 with $0.20 left, which is less than any live ask: seven addresses could stop live
    asks for the day. Council 4 asked for eight.)"""
    estimates = live_estimates_micro()
    share, cap = micro(config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD), micro(config.PRODUCTION_MAX_SPEND_USD_PER_DAY)
    left_after_seven = cap - 7 * share
    assert left_after_seven >= estimates["hybrid"], (
        f"seven full shares ({7 * share}) leave {left_after_seven} of the {cap} day, less than a hybrid ask's estimate "
        f"({estimates['hybrid']}): seven addresses could stop live asks. Lower the share to at most "
        f"{(cap - estimates['hybrid']) // 7}")
    assert left_after_seven >= estimates["vector"]               # the cheapest live ask fits too


def test_the_share_admits_the_buyer_demo_from_one_office_including_its_second_agent_ask():
    """Rule (ii) (council 4 test (b)): 20 asks from one address including 3 escalations and 2 agent asks. The second agent
    ask arrives with 15 x $0.002 + 3 x $0.04 + $0.15 = $0.30 settled, and is admitted while settled + its estimate fits the
    share."""
    estimates = live_estimates_micro()
    assert DEMO_SETTLED_MICRO == 300_000
    share = micro(config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD)
    assert share >= DEMO_SETTLED_MICRO + estimates["agent"], (
        f"the office's second agent ask would be refused: {DEMO_SETTLED_MICRO} settled + {estimates['agent']} estimated "
        f"is more than the share, {share}. Raise the share to at least {DEMO_SETTLED_MICRO + estimates['agent']}")


def test_the_share_sits_inside_the_window_the_two_rules_leave_and_the_window_is_not_empty():
    estimates = live_estimates_micro()
    cap = micro(config.PRODUCTION_MAX_SPEND_USD_PER_DAY)
    lowest = DEMO_SETTLED_MICRO + estimates["agent"]            # rule (ii)
    highest = (cap - estimates["hybrid"]) // 7                  # rule (i'): 7 x share <= cap - hybrid
    assert lowest <= highest, f"no share meets both rules: the demo needs {lowest} and eight addresses allow {highest}"
    assert lowest <= micro(config.PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD) <= highest


def test_the_share_is_read_from_its_environment_variable(monkeypatch):
    monkeypatch.setenv("PAID_SPEND_SHARE_PER_IP_USD", "1.33")
    with pytest.raises(ValidationError) as refused:
        live()
    assert named(refused) == ["PAID_SPEND_SHARE_PER_IP_USD"]
    monkeypatch.setenv("PAID_SPEND_SHARE_PER_IP_USD", "1.32")
    assert live().paid_spend_share_per_ip_usd == 1.32


def test_outside_production_the_share_may_be_off_or_above_the_live_pin_but_never_negative_or_not_a_number():
    for value in (0, 0.5, 5.0):
        assert Settings(_env_file=None, paid_spend_share_per_ip_usd=value).paid_spend_share_per_ip_usd == value
    assert staging(paid_spend_share_per_ip_usd=0).environment == "staging"
    for value in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, paid_spend_share_per_ip_usd=value)


def test_a_refused_share_names_the_setting_and_never_prints_its_value():
    with pytest.raises(ValidationError) as refused:
        live(paid_spend_share_per_ip_usd=1.3737)
    text = str(refused.value)
    assert named(refused) == ["PAID_SPEND_SHARE_PER_IP_USD"]
    assert "1.3737" not in text and "input_value" not in text
    assert "at most 1.32 in production" in text                  # the ceiling is stated, the offending value is not


# ---------------------------------------------------------------- the gaps: each booted before, none boots now

REFUSED = [
    ({"rate_limit_questions": 0}, "RATE_LIMIT_QUESTIONS"),
    ({"rate_limit_questions": 6}, "RATE_LIMIT_QUESTIONS"),
    ({"free_rate_limit_questions": 0}, "FREE_RATE_LIMIT_QUESTIONS"),
    ({"free_rate_limit_questions": 31}, "FREE_RATE_LIMIT_QUESTIONS"),
    ({"read_rate_limit_per_minute": 0}, "READ_RATE_LIMIT_PER_MINUTE"),
    ({"read_rate_limit_per_minute": 121}, "READ_RATE_LIMIT_PER_MINUTE"),
    ({"workspace_create_per_day": 0}, "WORKSPACE_CREATE_PER_DAY"),
    ({"workspace_create_per_day": 4}, "WORKSPACE_CREATE_PER_DAY"),
    ({"uploads_per_hour": 0}, "UPLOADS_PER_HOUR"),
    ({"uploads_per_hour": 11}, "UPLOADS_PER_HOUR"),
    ({"rate_limit_window_seconds": 0}, "RATE_LIMIT_WINDOW_SECONDS"),        # a window of 0 s never holds an event
    ({"rate_limit_window_seconds": 599}, "RATE_LIMIT_WINDOW_SECONDS"),
    ({"cache_read_budget_per_s": 1e6}, "CACHE_READ_BUDGET_PER_S"),
    ({"cache_read_budget_per_s": 10.5}, "CACHE_READ_BUDGET_PER_S"),
    ({"kill_switch_stale_s": 86_400}, "KILL_SWITCH_STALE_S"),
    ({"kill_switch_stale_s": 31}, "KILL_SWITCH_STALE_S"),
    ({"admin_token": "a"}, "ADMIN_TOKEN"),
    ({"admin_token": "a" * 31}, "ADMIN_TOKEN"),
    ({"admin_token": " " * 40}, "ADMIN_TOKEN"),
    ({"client_ip_header": "x-forwarded-for"}, "CLIENT_IP_HEADER"),
    ({"paid_spend_share_per_ip_usd": 0}, "PAID_SPEND_SHARE_PER_IP_USD"),          # 0 means "off" elsewhere
    ({"paid_spend_share_per_ip_usd": 1.33}, "PAID_SPEND_SHARE_PER_IP_USD"),
    ({"paid_spend_share_per_ip_usd": 1.3200001}, "PAID_SPEND_SHARE_PER_IP_USD"),
    ({"paid_spend_share_per_ip_usd": 1.40}, "PAID_SPEND_SHARE_PER_IP_USD"),          # the first live value: it let seven addresses stop live asks
    ({"paid_spend_share_per_ip_usd": 1e9}, "PAID_SPEND_SHARE_PER_IP_USD"),
    # LiteLLM reads both for the Anthropic route and live calls pass no api_base: a held value would divert every Sonnet call
    ({"anthropic_api_base": "https://gateway.example/v1"}, "ANTHROPIC_API_BASE"),
    ({"anthropic_api_base": " "}, "ANTHROPIC_API_BASE"),
    ({"anthropic_base_url": "https://gateway.example"}, "ANTHROPIC_BASE_URL"),
    ({"anthropic_base_url": " "}, "ANTHROPIC_BASE_URL"),
]


@pytest.mark.parametrize("changes,setting", REFUSED, ids=lambda value: str(value))
def test_production_refuses_the_gap_and_names_only_that_setting(changes, setting):
    with pytest.raises(ValidationError) as refused:
        live(**changes)
    assert named(refused) == [setting]


@pytest.mark.parametrize("changes", [
    {"rate_limit_questions": 5}, {"rate_limit_questions": 1}, {"free_rate_limit_questions": 30},
    {"read_rate_limit_per_minute": 120}, {"workspace_create_per_day": 3}, {"uploads_per_hour": 10},
    {"rate_limit_window_seconds": 600}, {"rate_limit_window_seconds": 3600}, {"cache_read_budget_per_s": 10},
    {"cache_read_budget_per_s": 1}, {"kill_switch_stale_s": 30}, {"kill_switch_stale_s": 20},
    {"admin_token": "a" * 32}, {"admin_token": ""},
    {"paid_spend_share_per_ip_usd": 1.32}, {"paid_spend_share_per_ip_usd": 1.25}, {"paid_spend_share_per_ip_usd": 0.5},
    {"paid_spend_share_per_ip_usd": 0.000001}], ids=lambda value: str(value))
def test_the_pin_itself_and_anything_tighter_boots(changes):
    assert live(**changes).is_production


ANTHROPIC_BASES = ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL")


@pytest.mark.parametrize("variable", ANTHROPIC_BASES)
def test_production_refuses_an_anthropic_base_held_in_the_process_environment(monkeypatch, variable):
    """LiteLLM 1.100.0 reads both variables from ``os.environ`` itself for the Anthropic route (``litellm/main.py``) and the live
    Sonnet calls pass no ``api_base`` of their own: a production process holding either would send every escalation to that
    address. The live ``Settings`` is built from the process environment, so the environment is what must be refused, by name
    and without its value (the same rule as ``OPENAI_BASE_URL``)."""
    monkeypatch.setenv(variable, "https://sentinel-anthropic-base-139.example/v1")
    with pytest.raises(ValidationError) as refused:
        live()
    assert named(refused) == [variable]
    text = str(refused.value)
    assert "sentinel-anthropic-base-139" not in text and "input_value" not in text


@pytest.mark.parametrize("variable", ANTHROPIC_BASES)
def test_an_empty_anthropic_base_in_the_process_environment_is_not_a_base(monkeypatch, variable):
    """An exported-but-empty variable is falsy for LiteLLM's ``or`` chain as well, so the live app still boots."""
    monkeypatch.setenv(variable, "")
    assert live().is_production


def test_both_anthropic_bases_are_named_when_both_are_held(monkeypatch):
    for variable in ANTHROPIC_BASES:
        monkeypatch.setenv(variable, "https://sentinel-anthropic-base-139.example")
    with pytest.raises(ValidationError) as refused:
        live()
    assert sorted(named(refused)) == sorted(ANTHROPIC_BASES)


def test_the_anthropic_bases_are_checked_before_deploy_and_kept_out_of_the_repr():
    """A base URL may carry credentials, and the pre-deploy check lists every setting a production refusal can name."""
    assert set(ANTHROPIC_BASES) <= set(config.CHECKED_SETTINGS)
    for name in ANTHROPIC_BASES:
        assert Settings.model_fields[name.lower()].repr is False
        assert Settings.model_fields[name.lower()].default == ""
    text = repr(Settings(_env_file=None, anthropic_api_base="https://user:sentinel-pw-139@host/v1",
                         anthropic_base_url="https://user:sentinel-pw-139@host"))
    assert "sentinel-pw-139" not in text


def test_outside_production_an_anthropic_base_is_allowed_for_a_local_gateway():
    """A developer may route their own calls through a proxy; only the live app refuses it."""
    s = Settings(_env_file=None, environment="development", anthropic_api_base="http://localhost:4000",
                 anthropic_base_url="http://localhost:4000")
    assert not s.is_production and s.anthropic_api_base == s.anthropic_base_url == "http://localhost:4000"


def test_an_empty_admin_token_keeps_the_admin_routes_off_and_boots():
    """No token means every /api/admin route answers 404 (routes._check_admin). Safe, if inconvenient: the pre-deploy
    check warns about it, the validator only refuses a token that is set and weak."""
    assert live(admin_token="").admin_token == ""


def test_one_error_names_every_problem_at_once():
    with pytest.raises(ValidationError) as refused:
        live(ip_hash_pepper="short", admin_token="a", rate_limit_questions=0, turnstile_required=False,
             kill_switch_stale_s=86_400)
    assert sorted(named(refused)) == sorted(
        ["IP_HASH_PEPPER", "ADMIN_TOKEN", "RATE_LIMIT_QUESTIONS", "TURNSTILE_REQUIRED", "KILL_SWITCH_STALE_S"])


def test_outside_production_every_window_may_be_off_and_the_token_short():
    s = staging(rate_limit_questions=0, free_rate_limit_questions=0, read_rate_limit_per_minute=0,
                rate_limit_window_seconds=0, cache_read_budget_per_s=1e6, kill_switch_stale_s=86_400, admin_token="a",
                workspace_create_per_day=0, uploads_per_hour=0)
    assert not s.is_production
    assert not Settings(_env_file=None, environment="development", rate_limit_questions=0, admin_token="a").is_production


@pytest.mark.parametrize("sentinels", [
    {"admin_token": "sentinel-admin-137", "ip_hash_pepper": "sentinel-pepper-137", "client_ip_header": "sentinel-hdr-137"},
    {"fly_app_name": "sentinel-app-137", "environment": "sentinel-env-137"},
    {"environment": "sentinel-env-137"},
], ids=["production", "unknown app", "wrong environment"])
def test_a_refusal_never_prints_a_value(sentinels):
    with pytest.raises(ValidationError) as refused:
        live(rate_limit_questions=0, **sentinels)
    text = str(refused.value)
    assert "input_value" not in text
    assert [value for value in (*sentinels.values(), PEPPER, TURNSTILE_SECRET, ADMIN_TOKEN) if value in text] == []


def test_every_setting_a_test_expects_in_a_refusal_is_listed_for_the_pre_deploy_check():
    assert set(PINNED) <= set(config.CHECKED_SETTINGS)
    assert len(set(config.CHECKED_SETTINGS)) == len(config.CHECKED_SETTINGS)
    assert all(name.lower() in Settings.model_fields for name in config.CHECKED_SETTINGS)


# ---------------------------------------------------------------- ENVIRONMENT and CLIENT_IP_HEADER are normalized once

@pytest.mark.parametrize("raw", ["production ", " Production", "PRODUCTION\t", "\nproduction\r\n"])
def test_an_environment_with_stray_whitespace_or_case_is_still_production_and_still_validated(raw):
    assert live(environment=raw).environment == "production"
    with pytest.raises(ValidationError) as refused:
        live(environment=raw, ip_hash_pepper="")          # the old code skipped every validator for "production "
    assert named(refused) == ["IP_HASH_PEPPER"]


@pytest.mark.parametrize("raw", [" fly-client-ip", "Fly-Client-IP ", "\tFLY-CLIENT-IP\r\n"])
def test_the_client_address_header_is_stored_normalized(raw):
    assert live(client_ip_header=raw).client_ip_header == "fly-client-ip"


def test_a_header_that_is_not_the_proxys_is_still_refused_after_normalizing():
    with pytest.raises(ValidationError) as refused:
        live(client_ip_header=" X-Forwarded-For ")
    assert named(refused) == ["CLIENT_IP_HEADER"]


def test_other_environments_are_normalized_too():
    assert staging(environment=" Staging ").environment == "staging"
    assert not Settings(_env_file=None, environment="Development").is_production


# ---------------------------------------------------------------- ENVIRONMENT is tied to FLY_APP_NAME

def test_the_known_apps_and_their_environments_are_the_live_app_and_the_staging_window_apps():
    """The live app, the staging API, and the three staging apps that run this code's Python (the mock LLM, the S7 machine,
    the load generators); the staging database app runs none and is not here (tests/test_serve_config_staging.py)."""
    assert config.FLY_APP_ENVIRONMENTS == {"semigraph": "production", "semigraph-stg": "staging",
                                           "semigraph-mockllm": "staging", "semigraph-tools-stg": "staging",
                                           "semigraph-loadgen-stg": "staging"}
    assert FLY["app"] == "semigraph" and FLY["env"]["ENVIRONMENT"] == config.FLY_APP_ENVIRONMENTS[FLY["app"]]


@pytest.mark.parametrize("environment", ["development", "staging", "", "prod"])
def test_the_live_app_must_run_as_production(environment):
    with pytest.raises(ValidationError) as refused:
        live(environment=environment)
    if environment == "staging":
        # the live app's settings run as staging fail the staging rules too (the database host, the mock key, ...)
        assert "ENVIRONMENT" in named(refused)
    else:
        assert named(refused) == ["ENVIRONMENT"]


def test_the_live_app_without_an_environment_is_refused_not_treated_as_development():
    """Forgetting ENVIRONMENT would otherwise leave every production validator off on the live machine."""
    env = {key.lower(): value for key, value in FLY["env"].items() if key != "ENVIRONMENT"}
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, fly_app_name="semigraph", **env, **SECRETS)
    assert named(refused) == ["ENVIRONMENT"]


def test_the_staging_app_must_run_as_staging_and_is_not_production():
    s = staging()
    assert s.environment == "staging" and not s.is_production and s.fly_app_name == "semigraph-stg"
    for environment in ("production", "development"):
        with pytest.raises(ValidationError) as refused:
            live(fly_app_name="semigraph-stg", environment=environment)
        assert "ENVIRONMENT" in named(refused)


def test_an_app_this_build_does_not_know_is_refused_without_naming_it():
    with pytest.raises(ValidationError) as refused:
        live(fly_app_name="someone-elses-app", environment="development")
    assert "FLY_APP_NAME" in named(refused) and "someone-elses-app" not in str(refused.value)


def test_without_a_fly_app_name_any_environment_is_a_local_process():
    for environment in ("development", "production"):
        kwargs = SECRETS | {"turnstile_required": True, "client_ip_header": "fly-client-ip"}
        assert Settings(_env_file=None, environment=environment, **kwargs).environment == environment
    assert staging(fly_app_name="").environment == "staging"       # a staging process must still satisfy the staging rules


def test_the_tie_reads_the_fly_app_name_environment_variable(monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", "semigraph")
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)
    assert named(refused) == ["ENVIRONMENT"]
    monkeypatch.setenv("FLY_APP_NAME", "semigraph-stg")
    kwargs = staging_kwargs()
    del kwargs["fly_app_name"]
    assert Settings(_env_file=None, **kwargs).fly_app_name == "semigraph-stg"
