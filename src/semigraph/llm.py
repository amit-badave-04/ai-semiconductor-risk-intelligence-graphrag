"""Hardened structured-LLM helper (final form from notebooks 12/14).

Claude Sonnet 5 via LiteLLM — every rule below was verified live and each
cost a failed run before it was learned; do not relax them:

- never pass temperature/top_p/top_k (the API rejects non-default sampling
  params with a 400)
- thinking={"type": "disabled"} on structured Sonnet calls — thinking is on
  by default and can eat a capped max_tokens budget, returning content=None.
  Haiku calls omit it (thinking_off=False): Haiku is thinking-off by default.
- finish_reason == "length" means the JSON was TRUNCATED mid-generation:
  asking the model to "fix" it just truncates again — regenerate from
  scratch with a doubled budget
- retry ONLY known-transient errors (network blips, 429s, 5xx/529 overload)
  with a long-tailed backoff; anything else (auth, bad request) raises
  immediately
- genuine schema mismatches get correction turns with the invalid output
  in-context
"""

import logging
import re
import time

import litellm
from litellm import completion
from pydantic import ValidationError

from .config import get_settings

logger = logging.getLogger("semigraph.llm")

# Transient failures worth waiting out. 529 overload incidents can last
# minutes, hence the long tail.
TRANSIENT = (
    litellm.APIConnectionError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.RateLimitError,
    litellm.Timeout,
)

BACKOFF_S = [15, 60, 180, 300]
MAX_BUDGET = 8000

_FENCE_RE = re.compile(r"^```(json)?|```$", flags=re.MULTILINE)


def _salvage_json_object(text: str) -> str | None:
    """The first balanced {...} object inside prose (judges occasionally wrap
    their JSON in a sentence); None when there is no such object."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def llm_json(
    prompt: str,
    model_cls,
    *,
    model: str | None = None,
    max_tokens: int = 4000,
    thinking_off: bool = True,
):
    """Call the LLM and validate the JSON response against a Pydantic model.

    Returns a validated instance of ``model_cls`` or raises RuntimeError
    after 4 attempts.
    """
    model = model or get_settings().llm_model
    kwargs = {"thinking": {"type": "disabled"}} if thinking_off else {}
    messages = [{"role": "user", "content": prompt}]
    budget = max_tokens
    last_err = "unknown"
    for attempt in range(4):
        try:
            resp = completion(
                model=model, messages=messages, max_tokens=budget,
                num_retries=2, **kwargs,
            )
        except TRANSIENT as e:
            wait = BACKOFF_S[attempt]
            logger.warning(
                "transient error (%s) — waiting %ss then retrying",
                type(e).__name__, wait,
            )
            time.sleep(wait)
            last_err = f"transient: {type(e).__name__}"
            continue
        choice = resp.choices[0]
        content = choice.message.content
        if not content:  # empty response — retry fresh
            last_err = f"empty response (finish_reason={choice.finish_reason})"
            messages = [{"role": "user", "content": prompt}]
            continue
        if choice.finish_reason == "length":  # truncated — bigger budget, fresh start
            budget = min(budget * 2, MAX_BUDGET)
            last_err = "output truncated"
            messages = [{"role": "user", "content": prompt}]
            continue
        raw = _FENCE_RE.sub("", content.strip()).strip()
        try:
            return model_cls.model_validate_json(raw)
        except ValidationError as e:
            salvaged = _salvage_json_object(raw)
            if salvaged is not None:
                try:
                    return model_cls.model_validate_json(salvaged)
                except ValidationError:
                    pass
            last_err = str(e)[:300]
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": raw},
                {"role": "user",
                 "content": f"Invalid JSON for the schema: {e}. Reply with corrected JSON only."},
            ]
    raise RuntimeError(f"llm_json failed after 4 attempts — last error: {last_err}")
