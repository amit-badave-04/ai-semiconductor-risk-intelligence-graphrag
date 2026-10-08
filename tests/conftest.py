"""Fixtures every test gets. Only ``pytest`` is imported here: this file is loaded by the serve-shipped CI selection too."""

import pytest

# The four variables LiteLLM reads from the process environment to send a model call somewhere else. Production REFUSES to
# boot while one is set (``config.Settings``), and a developer shell or a gateway wrapper may export one of them (Claude
# Code's own environment exports ANTHROPIC_BASE_URL): a test that builds a production ``Settings`` would then fail on one
# machine and pass on another. A test about the refusal sets the variable itself, after this fixture has run.
MODEL_BASE_VARIABLES = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL")


@pytest.fixture(autouse=True)
def model_base_variables_come_only_from_the_test(monkeypatch):
    """No test depends on the environment of the machine that runs it: these four start unset in every test."""
    for name in MODEL_BASE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
