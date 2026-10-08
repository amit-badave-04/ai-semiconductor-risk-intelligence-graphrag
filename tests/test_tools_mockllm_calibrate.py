"""tools/mockllm: the profile (``profiles.json``), its calibration from recorded runs, and the staging image's inputs.

Inline fixtures in the shape of the real recorded rows (``eval_deployed.v2e.jsonl``, ``eval_agent.v2.jsonl``) and of the S12
smoke output (``scripts/latency_smoke.py``): ``data/processed/`` is gitignored, so nothing here reads it. The committed
``tools/mockllm/profiles.json`` is checked for shape and plausibility, not for exact numbers.
"""

import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.mockllm import calibrate  # noqa: E402
from tools.mockllm.profile import (  # noqa: E402
    DEFAULT_PROFILE_PATH,
    QUANTILE_POINTS,
    ROLES,
    Profile,
    Table,
    load_profile,
    role_of,
)

STAGING = ROOT / "deploy" / "staging"


# --- fixtures in the shape of the recorded rows ----------------------------------------------------------------------

def v2e_row(i: int, *, routed: str, escalated: bool = False, billed: int = 200, chars_per_token: float = 2.8,
            overhead: float = 1.0, tps: float = 100.0) -> dict:
    """latency = overhead + billed / tps exactly, so the fit has a known answer."""
    return {"id": f"Q{i}", "routed": routed, "escalated": escalated, "answer": "x" * round(billed * chars_per_token),
            "usage": {"prompt_tokens": 13000, "completion_tokens": billed}, "latency_s": overhead + billed / tps,
            "answered_by": "anthropic/claude-sonnet-5" if routed == "strong" or escalated else "openai/gpt-6-luna"}


def v2e_rows() -> list[dict]:
    rows = [v2e_row(i, routed="cheap", billed=80 + 40 * i, chars_per_token=1.4, overhead=1.5) for i in range(8)]
    rows += [v2e_row(10 + i, routed="strong", billed=300 + 50 * i, overhead=2.0, tps=80.0) for i in range(6)]
    rows.append(v2e_row(20, routed="cheap", escalated=True, billed=1500))
    return rows


def s12_doc() -> dict:
    asks = [{"id": f"S{i}", "routed": "cheap", "escalated": i == 0} for i in range(12)]
    calls = [{"ask": f"S{i}", "role": "draft", "ttft_s": 0.8 + 0.01 * i, "decode_s": 1.0, "n_deltas": 26,
              "visible_chars": 280, "prompt_tokens": 13000, "completion_tokens": 240, "finish_reason": "stop"}
             for i in range(12)]
    calls += [{"ask": f"S{i}", "role": "strong", "ttft_s": 1.2 + 0.1 * i, "decode_s": 4.0, "n_deltas": 81,
               "visible_chars": 1400, "prompt_tokens": 13000, "completion_tokens": 500, "finish_reason": "stop"} for i in range(4)]
    calls += [{"ask": "A1", "role": "planner", "latency_s": 0.9 + 0.1 * i, "completion_tokens": 40 + i} for i in range(3)]
    return {"spike": "S12", "version": 1, "asks": asks, "calls": calls}


def agent_rows() -> list[dict]:
    return [{"id": f"A{i}", "agent": {"model_calls": 2, "elapsed_s": 3.0 + 0.2 * i, "planner_usage": {"completion_tokens": 50 + i}}}
            for i in range(6)]


# --- the distribution table ------------------------------------------------------------------------------------------

def test_a_table_reproduces_its_values_and_interpolates_between_quantiles():
    table = Table.from_values([10, 20, 30, 40, 50])
    assert len(table.q) == QUANTILE_POINTS and table.n == 5
    assert table.at(0) == 10 and table.at(1) == 50 and table.median == 30
    assert table.at(0.125) == pytest.approx(15.0)
    assert Table.constant(7).sample(random.Random(1)) == 7
    assert all(10 <= table.sample(random.Random(s)) <= 50 for s in range(50))


def test_sampling_follows_the_distribution():
    table = Table.from_values(list(range(1, 101)))
    draws = sorted(table.sample(random.Random(s)) for s in range(2000))
    assert draws[1000] == pytest.approx(50.5, abs=4)
    assert draws[100] == pytest.approx(5.95, abs=3) and draws[1900] == pytest.approx(95.05, abs=3)      # the 5th and 95th percentiles


def test_a_table_needs_values_and_valid_quantiles():
    with pytest.raises(ValueError):
        Table.from_values([])
    with pytest.raises(ValueError, match="quantile"):
        Table.from_dict({"q": [1, 2, 3]})
    with pytest.raises(ValueError, match="decrease"):
        Table.from_dict({"q": [2.0] + [1.0] * (QUANTILE_POINTS - 1)})


def test_the_role_follows_tools_then_the_model_name():
    assert role_of("mock-luna", True) == "planner" and role_of("mock-sonnet", True) == "planner"
    assert role_of("mock-sonnet", False) == "strong" and role_of("claude-opus-5", False) == "strong"
    assert role_of("mock-luna", False) == "draft" and role_of("anything", False) == "draft" and role_of("", False) == "draft"


# --- calibration from the recorded v2e rows ----------------------------------------------------------------------------

def test_v2e_rows_give_the_decode_rate_the_hidden_tokens_the_ttft_and_the_escalation_rate():
    profile = calibrate.calibrate(v2e_rows=v2e_rows(), today="2026-10-08")
    draft, strong = profile.role("draft"), profile.role("strong")
    assert draft.visible_tokens_per_s == pytest.approx(100.0) and strong.visible_tokens_per_s == pytest.approx(80.0)
    assert profile.visible_chars_per_token == pytest.approx(2.8, abs=0.01)                    # read from the strong rows
    assert strong.hidden_tokens.at(1) == 0 and draft.hidden_tokens.median > 0                # luna reasons, sonnet does not
    assert draft.n == 8 and strong.n == 6 and draft.provisional and strong.provisional
    # latency 1.5 s of fixed time less the assumed 0.5 s of retrieval, plus the hidden tokens' decode time, is the first-token time
    expected = [1.5 - 0.5 + hidden / 100.0 for hidden in draft.hidden_tokens.q]
    assert draft.ttft_s.median == pytest.approx(expected[len(expected) // 2], rel=0.3)
    assert profile.escalation_rate == pytest.approx(1 / 9)                                   # 1 escalated of 9 cheap-routed rows
    assert profile.routed_strong_share == pytest.approx(6 / 15) and profile.generated_at == "2026-10-08"
    assert profile.role("planner").note.startswith("default, not measured")


def test_the_retrieval_overhead_is_an_explicit_assumption_that_moves_ttft_one_for_one():
    low = calibrate.calibrate(v2e_rows=v2e_rows(), retrieval_overhead_s=0.5).role("strong").ttft_s.median
    high = calibrate.calibrate(v2e_rows=v2e_rows(), retrieval_overhead_s=0.9).role("strong").ttft_s.median
    assert low - high == pytest.approx(0.4, abs=1e-3)


def test_v2e_rows_without_enough_of_each_kind_are_refused():
    with pytest.raises(ValueError, match="at least 5 draft and 5 strong"):
        calibrate.calibrate(v2e_rows=v2e_rows()[:6])
    with pytest.raises(ValueError, match="nothing to calibrate"):
        calibrate.calibrate()


def test_rows_with_missing_fields_are_skipped_not_fatal():
    rows = v2e_rows() + [{"id": "bad", "routed": "cheap"}, {"id": "bad2", "routed": "strong", "answer": "", "usage": {}}]
    assert calibrate.calibrate(v2e_rows=rows).role("draft").n == 8


def test_agent_rows_give_the_planner_role_per_model_call():
    profile = calibrate.calibrate(v2e_rows=v2e_rows(), agent_rows=agent_rows())
    planner = profile.role("planner")
    assert planner.n == 6 and planner.ttft_s.median == pytest.approx((3.0 + 0.2 * 2.5) / 2, rel=0.1)
    assert planner.visible_tokens.median == pytest.approx((50 + 2.5) / 2, rel=0.1)
    with pytest.raises(ValueError, match="planner runs"):
        calibrate.calibrate(v2e_rows=v2e_rows(), agent_rows=agent_rows()[:2])


# --- calibration from the S12 live smoke ---------------------------------------------------------------------------------

def test_s12_replaces_the_provisional_numbers_with_measured_ones():
    base = calibrate.calibrate(v2e_rows=v2e_rows(), agent_rows=agent_rows())
    profile = calibrate.calibrate(s12_doc=s12_doc(), base=base)
    draft, strong, planner = profile.role("draft"), profile.role("strong"), profile.role("planner")
    assert not draft.provisional and not strong.provisional and not planner.provisional
    assert draft.n == 12 and strong.n == 4 and planner.n == 3
    assert draft.ttft_s.at(0) == pytest.approx(0.8) and draft.ttft_s.at(1) == pytest.approx(0.91)
    assert strong.ttft_s.at(0) == pytest.approx(1.2) and planner.ttft_s.at(0) == pytest.approx(0.9)
    assert profile.visible_chars_per_token == pytest.approx(1400 / 500)                         # strong calls: chars per billed token
    visible_draft = 280 / (1400 / 500)
    assert draft.visible_tokens.median == pytest.approx(visible_draft) and draft.hidden_tokens.median == pytest.approx(240 - visible_draft)
    assert draft.visible_tokens_per_s == pytest.approx(visible_draft / 1.0) and strong.visible_tokens_per_s == pytest.approx(500 / 4.0)
    assert profile.chunk_interval_s == pytest.approx(1.0 / 25, abs=0.001)                       # decode_s / (n_deltas - 1), draft and strong
    assert profile.escalation_rate == pytest.approx(1 / 12)
    assert [s["kind"] for s in profile.sources] == ["v2e", "agent", "s12"]


def test_s12_with_too_few_calls_or_asks_leaves_the_earlier_numbers_alone():
    base = calibrate.calibrate(v2e_rows=v2e_rows(), agent_rows=agent_rows())
    thin = s12_doc()
    thin["calls"] = [c for c in thin["calls"] if c["role"] != "draft"][:5] + [c for c in thin["calls"] if c["role"] == "draft"][:2]
    thin["asks"] = thin["asks"][:5]
    profile = calibrate.calibrate(s12_doc=thin, base=base)
    assert profile.role("draft") == base.role("draft") and profile.escalation_rate == base.escalation_rate
    assert profile.role("strong").provisional is False        # four strong calls are enough


# --- the file ------------------------------------------------------------------------------------------------------------

def test_a_profile_survives_a_round_trip_and_bad_files_are_refused(tmp_path):
    profile = calibrate.calibrate(v2e_rows=v2e_rows(), agent_rows=agent_rows(), today="2026-10-08")
    path = tmp_path / "profiles.json"
    calibrate.write_profile(profile, path)
    assert load_profile(path).to_dict() == profile.to_dict()
    good = json.loads(path.read_text(encoding="utf-8"))
    for mutate, message in ((lambda d: d.update(version=2), "version"), (lambda d: d["roles"].pop("planner"), "planner"),
                            (lambda d: d.update(escalation_rate=1.5), "escalation_rate"),
                            (lambda d: d["roles"]["draft"].update(visible_tokens_per_s=0), "positive"),
                            (lambda d: d.update(chunk_interval_s=0), "chunk_interval_s")):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        with pytest.raises((ValueError, KeyError), match=message):
            Profile.from_dict(broken)


def test_the_cli_writes_a_loadable_profile_from_files(tmp_path, capsys):
    (tmp_path / "v2e.jsonl").write_text("\n".join(json.dumps(r) for r in v2e_rows()), encoding="utf-8")
    (tmp_path / "agent.jsonl").write_text("\n".join(json.dumps(r) for r in agent_rows()), encoding="utf-8")
    (tmp_path / "s12.json").write_text(json.dumps(s12_doc()), encoding="utf-8")
    out = tmp_path / "out.json"
    assert calibrate.main(["--v2e", str(tmp_path / "v2e.jsonl"), "--agent", str(tmp_path / "agent.jsonl"), "--out", str(out)]) == 0
    assert calibrate.main(["--base", str(out), "--s12", str(tmp_path / "s12.json"), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "provisional" in printed and "measured" in printed
    assert not load_profile(out).role("draft").provisional


def test_the_committed_profile_is_complete_and_plausible():
    profile = load_profile(DEFAULT_PROFILE_PATH)
    assert set(profile.roles) == set(ROLES)
    draft, strong = profile.role("draft"), profile.role("strong")
    assert 0.0 < profile.escalation_rate < 0.2 and 1.5 < profile.visible_chars_per_token < 4.5
    assert all(profile.role(r).visible_tokens_per_s >= calibrate.TPS_RANGE[0] for r in ROLES)
    assert draft.hidden_tokens.median > 0 and strong.hidden_tokens.at(1) == 0
    assert all(profile.role(r).ttft_s.at(0) >= calibrate.MIN_TTFT_S for r in ROLES)
    assert strong.visible_tokens.median > draft.visible_tokens.median          # the strong model writes longer answers
    assert any(s["kind"] == "v2e" for s in profile.sources) and draft.provisional
    raw = DEFAULT_PROFILE_PATH.read_text(encoding="utf-8")
    assert "sk-" not in raw and "http" not in raw and not re.search(r"[A-Za-z]:\\", raw)


# --- the image: inputs, not a build (no Docker here) ------------------------------------------------------------------------

def _requirement_blocks(path: Path) -> dict[str, str]:
    blocks, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith((" ", "#")):
            current = line.split("==")[0]
            blocks[current] = line
        elif current:
            blocks[current] += "\n" + line
    return blocks


def test_every_mock_requirement_is_pinned_with_hashes_at_the_version_the_service_uses():
    mock = _requirement_blocks(STAGING / "requirements-mockllm.txt")
    serve = _requirement_blocks(ROOT / "deploy" / "requirements-serve.txt")
    assert {"fastapi", "uvicorn", "starlette", "pydantic"} <= set(mock)
    for name, block in mock.items():
        assert re.match(rf"{re.escape(name)}==[\w.]+ \\\n", block), block[:60]
        assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", block)) >= 1, name
    shared = {n for n in mock if n in serve}
    assert shared >= {"fastapi", "uvicorn", "starlette", "pydantic", "anyio", "h11", "click", "idna"}
    for name in shared:
        assert mock[name].split()[0] == serve[name].split()[0], name              # same pin as the service under test
    assert not {"litellm", "neo4j", "onnxruntime", "numpy", "pymupdf", "pdfplumber"} & set(mock)


def test_the_dockerfile_installs_hash_locked_runs_unprivileged_and_copies_the_grammar_file():
    text = (STAGING / "Dockerfile.mockllm").read_text(encoding="utf-8")
    assert "--require-hashes -r deploy/staging/requirements-mockllm.txt" in text
    assert re.search(r"^USER app$", text, re.M) and "useradd" in text
    assert "COPY src/semigraph/retrieval/ids.py src/semigraph/retrieval/ids.py" in text
    assert "COPY tools/mockllm tools/mockllm" in text and 'CMD ["python", "-m", "tools.mockllm"]' in text
    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.13-slim$", text, re.M) and "sha256:<digest>" in text      # pin is OPEN, said so
    assert not re.search(r"^(ENV|ARG).*(KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S", text, re.M | re.I)


def test_the_mock_image_inputs_do_not_name_pymupdf():
    """The AGPL PDF library must reach no image. (Other files of ``deploy/`` name it in comments that forbid it; the mock's
    own inputs have no reason to.)"""
    files = [STAGING / "Dockerfile.mockllm", STAGING / "requirements-mockllm.in", STAGING / "requirements-mockllm.txt"]
    for path in files:
        assert "pymupdf" not in path.read_text(encoding="utf-8").lower(), path


def test_the_image_layout_serves_with_nothing_of_the_semigraph_package_loaded(tmp_path):
    """Copy exactly what the Dockerfile copies into a fresh directory and run the server from there."""
    shutil.copy(ROOT / "src" / "semigraph" / "retrieval" / "ids.py", _mkdir(tmp_path / "src" / "semigraph" / "retrieval") / "ids.py")
    _mkdir(tmp_path / "tools")
    shutil.copy(ROOT / "tools" / "__init__.py", tmp_path / "tools" / "__init__.py")
    shutil.copytree(ROOT / "tools" / "mockllm", tmp_path / "tools" / "mockllm", ignore=shutil.ignore_patterns("__pycache__"))
    code = (
        "import sys, json\n"
        "from fastapi.testclient import TestClient\n"
        "from tools.mockllm.server import create_app\n"
        "app = create_app(env={'MOCKLLM_TIME_SCALE': '0'})\n"
        "r = TestClient(app).post('/v1/chat/completions', json={'model': 'mock-luna', 'messages': [{'role': 'user', 'content': "
        "'QUESTION: q\\n\\n=== SOURCE EXCERPTS ===\\n[0001045810-26-000021:I.1A:0001]\\nWe depend on a limited number of "
        "suppliers for advanced packaging capacity.\\n'}]})\n"
        "heavy = [m for m in ('semigraph', 'litellm', 'neo4j', 'numpy', 'onnxruntime') if m in sys.modules]\n"
        "print(json.dumps({'status': r.status_code, 'text': r.json()['choices'][0]['message']['content'], 'heavy': heavy}))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=120,
                            env={k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"})
    assert result.returncode == 0, result.stderr[-800:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["status"] == 200 and "[0001045810-26-000021:I.1A:0001]" in out["text"] and out["heavy"] == []


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path
