"""llm_json failure-handling tests — every battle scar, with litellm fully mocked.

No network, no API spend: semigraph.llm.completion and time.sleep are
monkeypatched.
"""

import json
from types import SimpleNamespace

import litellm
import pytest
from pydantic import BaseModel

import semigraph.llm as llm_mod
from semigraph.llm import llm_json


class Item(BaseModel):
    name: str
    value: int


def make_resp(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason
        )]
    )


GOOD_JSON = json.dumps({"name": "x", "value": 1})


class FakeCompletion:
    """Scripted completion double that records every call's kwargs."""

    def __init__(self, script):
        self.script = list(script)  # items: response | exception
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def patch_completion(monkeypatch):
    def _patch(script):
        fake = FakeCompletion(script)
        monkeypatch.setattr(llm_mod, "completion", fake)
        return fake
    return _patch


def test_success_and_forbidden_params_never_sent(patch_completion):
    fake = patch_completion([make_resp(GOOD_JSON)])
    out = llm_json("p", Item, model="anthropic/claude-sonnet-5")
    assert out == Item(name="x", value=1)
    call = fake.calls[0]
    # battle scar: non-default sampling params are a 400 — must never be sent
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in call
    # battle scar: thinking disabled by default on structured Sonnet calls
    assert call["thinking"] == {"type": "disabled"}
    assert call["num_retries"] == 2


def test_thinking_off_false_omits_thinking_kwarg(patch_completion):
    fake = patch_completion([make_resp(GOOD_JSON)])
    llm_json("p", Item, model="anthropic/claude-haiku-4-5", thinking_off=False)
    assert "thinking" not in fake.calls[0]


def test_markdown_fences_stripped(patch_completion):
    patch_completion([make_resp(f"```json\n{GOOD_JSON}\n```")])
    assert llm_json("p", Item, model="m") == Item(name="x", value=1)


def test_truncation_regenerates_fresh_with_doubled_budget(patch_completion):
    fake = patch_completion([
        make_resp('{"name": "x", "va', finish_reason="length"),
        make_resp(GOOD_JSON),
    ])
    out = llm_json("p", Item, model="m", max_tokens=4000)
    assert out.value == 1
    assert fake.calls[0]["max_tokens"] == 4000
    # battle scar: never "fix" truncated JSON — regenerate from scratch, doubled
    assert fake.calls[1]["max_tokens"] == 8000
    assert fake.calls[1]["messages"] == [{"role": "user", "content": "p"}]


def test_budget_capped_at_8000(patch_completion):
    fake = patch_completion([
        make_resp("x", finish_reason="length"),
        make_resp("x", finish_reason="length"),
        make_resp(GOOD_JSON),
    ])
    llm_json("p", Item, model="m", max_tokens=6000)
    assert [c["max_tokens"] for c in fake.calls] == [6000, 8000, 8000]


def test_empty_content_retries_fresh(patch_completion):
    fake = patch_completion([make_resp(None), make_resp(GOOD_JSON)])
    assert llm_json("p", Item, model="m").name == "x"
    assert fake.calls[1]["messages"] == [{"role": "user", "content": "p"}]


def test_schema_mismatch_gets_correction_turn(patch_completion):
    bad = json.dumps({"name": "x"})  # missing "value"
    fake = patch_completion([make_resp(bad), make_resp(GOOD_JSON)])
    assert llm_json("p", Item, model="m") == Item(name="x", value=1)
    msgs = fake.calls[1]["messages"]
    assert len(msgs) == 3
    assert msgs[1] == {"role": "assistant", "content": bad}
    assert "Invalid JSON" in msgs[2]["content"]


def test_transient_error_backs_off_then_succeeds(patch_completion, monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    err = litellm.APIConnectionError(
        message="boom", llm_provider="anthropic", model="m"
    )
    fake = patch_completion([err, make_resp(GOOD_JSON)])
    assert llm_json("p", Item, model="m").value == 1
    assert sleeps == [15]  # first backoff step of 15/60/180/300
    assert len(fake.calls) == 2


def test_non_transient_error_raises_immediately(patch_completion):
    patch_completion([ValueError("auth-like failure")])
    with pytest.raises(ValueError):
        llm_json("p", Item, model="m")


def test_gives_up_after_four_attempts(patch_completion):
    patch_completion([make_resp(None)] * 4)
    with pytest.raises(RuntimeError, match="failed after 4 attempts"):
        llm_json("p", Item, model="m")
