"""scripts/build_onnx_embedder.py downloads the model weights at one pinned commit of the model repo (owner decision 2026-11-09).

Without a revision, ``hf_hub_download`` follows the repo's default branch, so two image builds can ship different weights. The
build now names the commit in ONE constant, ``REPO_REVISION``, and every download passes it. Docker is not available to these
tests: they read the script's syntax tree and drive ``download()`` against a stand-in ``huggingface_hub``.
"""

import ast
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_onnx_embedder.py"
SHA40 = re.compile(r"[0-9a-f]{40}")

spec = importlib.util.spec_from_file_location("build_onnx_embedder_revision_script", SCRIPT)
bo = importlib.util.module_from_spec(spec)
sys.modules["build_onnx_embedder_revision_script"] = bo
spec.loader.exec_module(bo)

TREE = ast.parse(SCRIPT.read_text(encoding="utf-8"))


def calls_named(name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called == name:
                found.append(node)
    return found


def test_revision_constant_is_a_full_commit_sha():
    assert hasattr(bo, "REPO_REVISION"), "the pinned commit must live in one named constant"
    assert SHA40.fullmatch(bo.REPO_REVISION), "a branch or tag name moves; only a 40-hex commit sha is a pin"


def test_the_constant_is_assigned_once_and_never_read_from_the_environment():
    assigns = [n for n in ast.walk(TREE) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REPO_REVISION" for t in n.targets)]
    assert len(assigns) == 1
    assert isinstance(assigns[0].value, ast.Constant), "a literal, not os.environ or an argument"


def test_every_hf_hub_download_call_passes_the_revision():
    calls = calls_named("hf_hub_download")
    assert len(calls) >= 2, "the cached attempt and the network attempt"
    for call in calls:
        assert any(kw.arg == "revision" for kw in call.keywords), f"hf_hub_download at line {call.lineno} has no revision="


def test_every_call_of_download_passes_the_pinned_constant():
    calls = [c for c in calls_named("download") if isinstance(c.func, ast.Name)]
    assert len(calls) == 3, "model.onnx, model.onnx_data and tokenizer.json"
    for call in calls:
        args = [a.id for a in call.args if isinstance(a, ast.Name)] + [kw.value.id for kw in call.keywords if isinstance(kw.value, ast.Name)]
        assert "REPO_REVISION" in args, f"download() at line {call.lineno} does not pass REPO_REVISION"


def test_both_attempts_of_download_reach_hub_with_the_pinned_revision(monkeypatch, tmp_path):
    seen = []
    target = tmp_path / "f"

    def fake_hub_download(repo, filename, **kwargs):
        seen.append((repo, filename, kwargs))
        if kwargs.get("local_files_only"):
            raise FileNotFoundError("not cached")
        return str(target)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=fake_hub_download))
    out = bo.download(bo.REPO, "tokenizer.json", bo.REPO_REVISION)
    assert out == target
    assert len(seen) == 2
    for _repo, _name, kwargs in seen:
        assert kwargs.get("revision") == bo.REPO_REVISION


def test_download_requires_a_revision():
    with pytest.raises(TypeError):
        bo.download(bo.REPO, "tokenizer.json")


def test_dockerfile_passes_no_model_revision_of_its_own():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"(?im)^\s*(ARG|ENV)\s+\w*(REVISION|MODEL_ID|HF_MODEL)\w*", text), "the script's constant is the only pin"
