"""config.Settings: secrets never appear in a repr (a failing test prints the fixture's Settings object, key included), and the
adjudication model is a setting with the documented default and env name."""

import pytest

from semigraph.config import Settings

SECRETS = {"anthropic_api_key": "sk-ant-secret-1", "neo4j_password": "pw-secret-2", "embedding_api_key": "emb-secret-3",
           "admin_token": "adm-secret-4", "turnstile_secret_key": "ts-secret-5"}


def test_no_secret_field_appears_in_the_repr_or_str_of_the_settings():
    settings = Settings(_env_file=None, **SECRETS)
    text = repr(settings) + str(settings)
    assert not [v for v in SECRETS.values() if v in text]
    assert "neo4j_uri" in text and "adjudication_model" in text                  # the rest of the object is still shown


def test_the_secrets_are_still_readable_where_the_code_needs_them():
    settings = Settings(_env_file=None, **SECRETS)
    assert {k: getattr(settings, k) for k in SECRETS} == SECRETS


def test_the_adjudication_model_defaults_to_luna_and_reads_the_documented_environment_variable(monkeypatch):
    assert Settings(_env_file=None).adjudication_model == "openai/gpt-6-luna"
    monkeypatch.setenv("ADJUDICATION_MODEL", "anthropic/claude-sonnet-5")
    assert Settings(_env_file=None).adjudication_model == "anthropic/claude-sonnet-5"


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_every_secret_field_is_excluded_from_the_repr_by_declaration(name):
    assert Settings.model_fields[name].repr is False
