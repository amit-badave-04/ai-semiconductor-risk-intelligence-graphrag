"""The OpenAI wire shapes the mock speaks (chat completions, streaming chunks, errors, model list).

Shapes follow what the OpenAI chat completions API returns and what the clients under test read:
``retrieval/answerer_async.AsyncTextStream`` takes ``chunk.choices[0].delta.content``, ``choices[0].finish_reason`` and the
``usage`` of the last chunk (it counts only a usage with a truthy ``prompt_tokens``); ``agent/planner.LiteLLMPlanner`` takes
``choices[0].message.tool_calls[i].id / .function.name / .function.arguments``, ``finish_reason`` and ``usage``.

Stream order, as the API sends it: a first chunk with ``role``, content chunks, a chunk with an empty ``delta`` and the
``finish_reason``, then (only when the request set ``stream_options.include_usage``) a chunk with ``choices: []`` and the
``usage``, then ``data: [DONE]``. With ``include_usage`` every earlier chunk carries ``"usage": null``.
"""

import json
import random
import string

FINGERPRINT = "fp_mockllm"
SSE_DONE = b"data: [DONE]\n\n"
_ID_ALPHABET = string.ascii_letters + string.digits


def new_completion_id(rng: random.Random) -> str:
    return "chatcmpl-" + "".join(rng.choices(_ID_ALPHABET, k=29))


def new_tool_call_id(rng: random.Random) -> str:
    return "call_" + "".join(rng.choices(_ID_ALPHABET, k=24))


def usage_body(prompt_tokens: int, completion_tokens: int, hidden_tokens: int = 0) -> dict:
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": hidden_tokens, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0}}


def tool_calls_body(calls: list[tuple[str, str, str]]) -> list[dict]:
    """``calls`` = (id, name, JSON arguments string)."""
    return [{"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}
            for cid, name, arguments in calls]


def completion_body(completion_id: str, model: str, created: int, content: str | None, finish_reason: str, usage: dict,
                    tool_calls: list[tuple[str, str, str]] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content, "refusal": None}
    if tool_calls:
        message["tool_calls"] = tool_calls_body(tool_calls)
    return {"id": completion_id, "object": "chat.completion", "created": created, "model": model,
            "system_fingerprint": FINGERPRINT,
            "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish_reason}],
            "usage": usage}


def chunk_body(completion_id: str, model: str, created: int, delta: dict, finish_reason: str | None, *,
               include_usage: bool, usage: dict | None = None) -> dict:
    """One ``chat.completion.chunk``. ``usage`` given = the closing usage chunk (``choices: []``)."""
    body = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
            "system_fingerprint": FINGERPRINT,
            "choices": [] if usage is not None else [{"index": 0, "delta": delta, "logprobs": None,
                                                      "finish_reason": finish_reason}]}
    if include_usage:
        body["usage"] = usage
    return body


def sse_event(body: dict) -> bytes:
    return b"data: " + json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n\n"


def error_body(message: str, *, type_: str, code: str | None, param: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


def rate_limit_body(model: str) -> dict:
    """The body of an OpenAI 429 for a per-minute request limit."""
    return error_body(f"Rate limit reached for {model} in organization org-mockllm on requests per min (RPM): "
                      "Limit 500, Used 500, Requested 1. Please try again in 120ms. "
                      "Visit https://platform.openai.com/account/rate-limits to learn more.",
                      type_="requests", code="rate_limit_exceeded")


def rate_limit_headers(retry_after_s: float) -> dict[str, str]:
    return {"retry-after": str(max(0, round(retry_after_s))), "retry-after-ms": str(max(0, round(retry_after_s * 1000))),
            "x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": f"{retry_after_s:g}s"}


def models_body(ids: list[str], created: int) -> dict:
    return {"object": "list", "data": [model_body(i, created) for i in ids]}


def model_body(model_id: str, created: int) -> dict:
    return {"id": model_id, "object": "model", "created": created, "owned_by": "mockllm"}
