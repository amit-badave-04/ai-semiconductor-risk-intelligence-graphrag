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


def completion_params(model: str, max_tokens: int, *, reasoning_effort: str | None = None) -> dict:
    """The budget + reasoning keyword arguments for ``litellm.completion`` on ``model``.

    ``reasoning_effort`` (OpenAI GPT-6 only; ``"none"`` / ``"low"`` / ``"medium"``; ``"minimal"`` is a 400) turns the hidden
    reasoning down. Live probe 2026-09-26 on ``openai/gpt-6-luna``: default 131-141 reasoning tokens for a one-line
    verdict (and a stricter answer that overthinks paraphrases), ``"none"`` 0 reasoning tokens. Callers that classify
    short texts pass ``"none"``; the answering path keeps the default (its quality was benchmarked with reasoning on)."""
    if model.startswith("openai/"):
        if reasoning_effort:
            return {"max_completion_tokens": max_tokens, "reasoning_effort": reasoning_effort,
                    "allowed_openai_params": ["reasoning_effort"]}
        return {"max_completion_tokens": max_tokens}
    if model.startswith("gemini/"):
        return {"max_tokens": max_tokens, "reasoning_effort": "low"}
    if model.startswith(_PLAIN_MAX_TOKENS):
        return {"max_tokens": max_tokens}
    # allowed_openai_params: with LiteLLM's bundled (offline) price map, claude-sonnet-5 is not known to support
    # `thinking` and the call fails with UnsupportedParamsError before it is sent; the allow-list makes it map either way.
    return {"max_tokens": max_tokens, "thinking": {"type": "disabled"}, "allowed_openai_params": ["thinking"]}


def provider_kwargs(model: str, settings) -> dict:
    """Where the call goes, for ``litellm.completion`` / ``acompletion`` on ``model``: ``{"api_base": base}`` for an
    ``openai/`` model when ``settings.openai_api_base`` is set, else ``{}`` (the provider's own endpoint).

    This is the staging switch (M5a I5): the staging API names the mock LLM's private address in ``OPENAI_API_BASE`` and
    every model string it runs is ``openai/mock-*``, so every call reaches the mock and none reaches a provider. Production
    refuses a base (``config.Settings``), so live calls go where they always went. Every call of the serve path spreads
    this (a test scans the source for a bare call). ``settings`` may be any object: one without the attribute, or with an
    empty or blank one, adds nothing; a fresh dict is returned each time."""
    base = getattr(settings, "openai_api_base", None)
    if not model.startswith("openai/") or not isinstance(base, str) or not base.strip():
        return {}
    return {"api_base": base.strip()}


# The staging models (M5a I5): ``openai/mock-*`` is priced as the real model it stands for, so a staging run exercises the
# live cap arithmetic. ``serve.estimate`` prices an unlisted mock as the real model of its ROLE; ``answerer.usage_cost`` has
# no role and prices one only through this table.
MOCK_MODEL_PREFIX = "openai/mock-"
MOCK_ALIASES = {"openai/mock-luna": "openai/gpt-6-luna", "openai/mock-sonnet": "anthropic/claude-sonnet-5",
                "openai/mock-haiku": "anthropic/claude-haiku-4-5"}


# USD per million (input, output) tokens for the models the service actually runs, so cost accounting never
# depends on LiteLLM's price map (fetched over the network at import; it does not yet list these models).
# Values are LiteLLM's cost map on 2026-09-26. Anthropic's own pricing page
# (https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-08) confirms Claude Sonnet 5 at $2 / $10 per
# million tokens and states that Claude 4.6 and later models, Sonnet 5 included, get the full 1M-token context window at
# standard pricing (no long-context premium). OpenAI's price page for Luna and Anthropic's for Haiku 4.5 were not consulted:
# those two rows still rest on LiteLLM's map alone.
KNOWN_PRICES_PER_MTOK = {
    # 0.10 is LiteLLM's short-context input price. A probe that priced a 1M-token prompt returned 0.20 (a long-context
    # tier), which was wrongly copied here first and overstated Luna's cost up to 2x; corrected 2026-09-26.
    "openai/gpt-6-luna": (0.10, 0.50),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
}
