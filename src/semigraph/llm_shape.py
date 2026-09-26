"""Per-provider completion parameters for the answering call.

Each shape was probed live through LiteLLM 1.90.2 on 2026-09-26; do not "unify" them:

- Anthropic (and any unrecognised model name, which keeps the legacy behaviour): ``max_tokens`` plus
  ``thinking`` disabled — thinking is on by default and can eat a capped budget (see ``semigraph.llm``).
- OpenAI GPT-6: ``max_tokens`` is a 400, the budget is ``max_completion_tokens``; no ``thinking`` kwarg.
- Gemini 3.x: thinking is ON by default and billed as output (312 hidden tokens for a trivial question on
  3.8 Flash); ``reasoning_effort="low"`` removes it. ``minimal`` and a zero thinking budget are rejected
  or ignored by that model.
- OpenAI-compatible hosts (DeepInfra, Together, Fireworks, OpenRouter): plain ``max_tokens``; the open-weight
  models probed there (DeepSeek V4 Flash/Pro) do not think by default.
"""

_PLAIN_MAX_TOKENS = ("deepinfra/", "together_ai/", "fireworks_ai/", "openrouter/")


def completion_params(model: str, max_tokens: int) -> dict:
    """The budget + reasoning keyword arguments for ``litellm.completion`` on ``model``."""
    if model.startswith("openai/"):
        return {"max_completion_tokens": max_tokens}
    if model.startswith("gemini/"):
        return {"max_tokens": max_tokens, "reasoning_effort": "low"}
    if model.startswith(_PLAIN_MAX_TOKENS):
        return {"max_tokens": max_tokens}
    return {"max_tokens": max_tokens, "thinking": {"type": "disabled"}}


# USD per million (input, output) tokens for the models the service actually runs, so cost accounting never
# depends on LiteLLM's price map (fetched over the network at import; it does not yet list these models).
# Values are LiteLLM's cost map on 2026-09-26; the providers' own price pages were not consulted.
KNOWN_PRICES_PER_MTOK = {
    "openai/gpt-6-luna": (0.20, 0.50),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
}
