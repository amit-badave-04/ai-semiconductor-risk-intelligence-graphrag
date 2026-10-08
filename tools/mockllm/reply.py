"""What the mock answers to one request, decided before anything is sent: text or tool call, tokens, timings.

Pure (no I/O, no clock): the server plays a :class:`Reply` back in time; tests read it directly. The random draws happen
in a fixed order (time to first token, slow-start draw, length, hidden tokens, invalid-id draw, then the text), so one seed
gives one reply.
"""

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass

from .answers import compose
from .knobs import Knobs
from .planner import FINAL_TEXT, message_text, plan_reply
from .profile import Profile, RoleProfile, role_of
from .shapes import usage_body

DEFAULT_MODEL = "mock-luna"
BUDGET_SHARE = 0.9          # the sampled length never uses more than this share of the request's token budget


@dataclass(frozen=True)
class Reply:
    role: str
    model: str
    content: str | None
    tool_call: tuple[str, str, str] | None     # (id, name, JSON arguments)
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int                     # billed: visible plus hidden reasoning
    hidden_tokens: int
    ttft_s: float
    decode_s: float                            # time to stream the visible text after the first token
    slow: bool
    invalid_injected: bool
    source: str                                # excerpts | risk_lines | template | refusal | tool_call | final

    @property
    def usage(self) -> dict:
        return usage_body(self.prompt_tokens, self.completion_tokens, self.hidden_tokens)


def flatten_messages(messages: list) -> str:
    return "\n".join(message_text(m) for m in messages if isinstance(m, Mapping))


def _token_budget(body: Mapping) -> int | None:
    for key in ("max_completion_tokens", "max_tokens"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _prompt_tokens(body: Mapping, prompt_text: str, profile: Profile) -> int:
    chars = len(prompt_text) + (len(json.dumps(body["tools"])) if body.get("tools") else 0)
    return max(1, math.ceil(chars / profile.prompt_chars_per_token))


def _ttft(role: RoleProfile, knobs: Knobs, rng: random.Random) -> tuple[float, bool]:
    ttft = role.ttft_s.sample(rng)
    if rng.random() < knobs.slow_ttft_rate:
        return knobs.slow_ttft_s, True
    return ttft, False


def build_reply(body: Mapping, profile: Profile, knobs: Knobs, rng: random.Random) -> Reply:
    model = str(body.get("model") or DEFAULT_MODEL)
    role_name = role_of(model, bool(body.get("tools")))
    role = profile.role(role_name)
    prompt_text = flatten_messages(body.get("messages") or [])
    prompt_tokens = _prompt_tokens(body, prompt_text, profile)
    ttft, slow = _ttft(role, knobs, rng)
    if role_name == "planner":
        return _planner_reply(body, role, profile, rng, model, prompt_tokens, ttft, slow)
    return _answer_reply(body, role_name, role, profile, knobs, rng, model, prompt_text, prompt_tokens, ttft, slow)


def _planner_reply(body, role: RoleProfile, profile: Profile, rng, model, prompt_tokens, ttft, slow) -> Reply:
    plan = plan_reply(body, rng)
    if plan.tool_call:
        _, _, arguments = plan.tool_call
        tokens = max(math.ceil(len(arguments) / profile.visible_chars_per_token), round(role.visible_tokens.sample(rng)))
        source, finish = "tool_call", "tool_calls"
    else:
        tokens, source, finish = max(1, math.ceil(len(FINAL_TEXT) / profile.visible_chars_per_token)), "final", "stop"
    return Reply("planner", model, plan.content, plan.tool_call, finish, prompt_tokens, tokens, 0, ttft,
                 tokens / role.visible_tokens_per_s, slow, False, source)


def _clamp(visible: float, hidden: int, budget: int | None) -> tuple[float, int]:
    if budget is None:
        return visible, hidden
    limit = max(1, int(budget * BUDGET_SHARE))
    if visible + hidden <= limit:
        return visible, hidden
    hidden = min(hidden, limit // 2)
    return max(1.0, limit - hidden), hidden


def _answer_reply(body, role_name: str, role: RoleProfile, profile: Profile, knobs: Knobs, rng, model, prompt_text,
                  prompt_tokens, ttft, slow) -> Reply:
    budget = _token_budget(body)
    visible, hidden = _clamp(role.visible_tokens.sample(rng), int(round(role.hidden_tokens.sample(rng))), budget)
    fabricate = role_name == "draft" and rng.random() < knobs.invalid_id_rate
    draft = compose(prompt_text, rng, target_chars=int(visible * profile.visible_chars_per_token), fabricate=fabricate)
    text, finish = draft.text, "stop"
    if budget is not None:
        room = max(1, int((budget - hidden) * profile.visible_chars_per_token))
        if len(text) > room:
            text, finish = text[:room], "length"
    visible_tokens = max(1, round(len(text) / profile.visible_chars_per_token))
    return Reply(role_name, model, text, None, finish, prompt_tokens, visible_tokens + hidden, hidden, ttft,
                 visible_tokens / role.visible_tokens_per_s, slow, draft.fabricated_id is not None, draft.source)
