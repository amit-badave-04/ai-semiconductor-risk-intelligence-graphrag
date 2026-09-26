"""Per-provider completion parameters and model-aware cost (each shape was probed live on 2026-09-26)."""

import pytest

import semigraph.retrieval.answerer as ans
from semigraph.llm_shape import completion_params


def test_anthropic_and_unknown_models_keep_the_legacy_shape():
    legacy = {"max_tokens": 900, "thinking": {"type": "disabled"}, "allowed_openai_params": ["thinking"]}
    assert completion_params("anthropic/claude-sonnet-5", 900) == legacy
    assert completion_params("m", 900) == legacy  # placeholder names used across the existing tests


def test_openai_models_take_max_completion_tokens_and_no_thinking_kwarg():
    p = completion_params("openai/gpt-6-luna", 900)
    assert p == {"max_completion_tokens": 900}  # max_tokens is a 400 on these models


def test_gemini_thinking_is_turned_down_because_its_hidden_tokens_are_billed():
    assert completion_params("gemini/gemini-3.8-flash", 900) == {"max_tokens": 900, "reasoning_effort": "low"}


@pytest.mark.parametrize("model", ["deepinfra/deepseek-ai/DeepSeek-V4-Flash", "together_ai/x", "fireworks_ai/x", "openrouter/x"])
def test_openai_compatible_hosts_take_plain_max_tokens(model):
    assert completion_params(model, 900) == {"max_tokens": 900}


def test_llm_text_uses_the_provider_shape(monkeypatch):
    calls = []

    class Choice:
        message = type("M", (), {"content": "ok"})()
        finish_reason = "stop"

    def fake_completion(**kw):
        calls.append(kw)
        return type("R", (), {"choices": [Choice()]})()

    monkeypatch.setattr(ans, "completion", fake_completion)
    assert ans.llm_text("p", model="openai/gpt-6-luna", max_tokens=700) == "ok"
    assert calls[0]["max_completion_tokens"] == 700 and "max_tokens" not in calls[0] and "thinking" not in calls[0]


def test_usage_cost_without_a_model_is_the_configured_list_price():
    assert ans.usage_cost({"prompt_tokens": 1_000_000, "completion_tokens": 0}) == pytest.approx(2.0)


def test_usage_cost_for_a_named_model_uses_that_models_price(monkeypatch):
    seen = {}

    def fake_cost_per_token(model, prompt_tokens, completion_tokens):
        seen["model"] = model
        return prompt_tokens * 0.09 / 1e6, completion_tokens * 0.18 / 1e6

    monkeypatch.setattr(ans.litellm, "cost_per_token", fake_cost_per_token)
    cost = ans.usage_cost({"prompt_tokens": 10_000, "completion_tokens": 1_000}, model="deepinfra/x")
    assert seen["model"] == "deepinfra/x"
    assert cost == pytest.approx(0.00090 + 0.00018)


def test_usage_cost_for_a_model_with_no_known_price_falls_back_to_the_configured_price(monkeypatch):
    def unknown(**kw):
        raise Exception("model not in cost map")

    monkeypatch.setattr(ans.litellm, "cost_per_token", unknown)
    assert ans.usage_cost({"prompt_tokens": 1_000_000, "completion_tokens": 0}, model="x/new") == pytest.approx(2.0)


# --- review finding W4: the deployed models are priced from a local table, not from a network-fetched map ---

def test_deployed_models_are_priced_without_asking_litellm(monkeypatch):
    def unreachable(**kw):
        raise AssertionError("litellm price map must not be needed for the deployed models")

    monkeypatch.setattr(ans.litellm, "cost_per_token", unreachable)
    luna = ans.usage_cost({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, model="openai/gpt-6-luna")
    sonnet = ans.usage_cost({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, model="anthropic/claude-sonnet-5")
    assert luna == pytest.approx(0.10 + 0.50) and sonnet == pytest.approx(2.0 + 10.0)


# --- M1b: the answering model is its own setting, so extraction/judging never move with it ---

def test_answer_model_defaults_to_sonnet_like_llm_model_and_is_independent_of_it():
    from semigraph.config import Settings

    s = Settings(_env_file=None)
    assert s.answer_model == "anthropic/claude-sonnet-5" == s.llm_model
    assert Settings(answer_model="openai/gpt-6-luna", _env_file=None).llm_model == "anthropic/claude-sonnet-5"


def test_the_answer_path_defaults_to_answer_model_not_llm_model(monkeypatch):
    from semigraph.config import Settings

    monkeypatch.setattr(ans, "get_settings", lambda: Settings(answer_model="openai/gpt-6-luna", _env_file=None))
    assert ans.TextStream("p").model == "openai/gpt-6-luna"


def test_llm_text_defaults_to_answer_model(monkeypatch):
    from semigraph.config import Settings

    calls = []

    class Choice:
        message = type("M", (), {"content": "ok"})()
        finish_reason = "stop"

    monkeypatch.setattr(ans, "get_settings", lambda: Settings(answer_model="openai/gpt-6-luna", _env_file=None))
    monkeypatch.setattr(ans, "completion", lambda **kw: calls.append(kw) or type("R", (), {"choices": [Choice()]})())
    ans.llm_text("p")
    assert calls[0]["model"] == "openai/gpt-6-luna"


def test_openai_reasoning_effort_is_opt_in_and_allow_listed_for_litellm():
    assert completion_params("openai/gpt-6-luna", 400) == {"max_completion_tokens": 400}                      # answering path unchanged
    assert completion_params("openai/gpt-6-luna", 400, reasoning_effort="none") == {
        "max_completion_tokens": 400, "reasoning_effort": "none", "allowed_openai_params": ["reasoning_effort"]}
    assert "reasoning_effort" not in completion_params("anthropic/claude-sonnet-5", 400, reasoning_effort="none")   # other providers ignore it
