"""M2 image: the MiniLM reranker is fetched at build time, SHA-256 verified, and sits where the settings point (R24, gate R13).

Docker is not available, so these pin the Dockerfile, fly.toml and the build script; the script itself runs against a fake fetcher
(no network).
"""

import re
import hashlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_reranker_model", ROOT / "scripts" / "build_reranker_model.py")
brm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brm)

DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
LOCAL = ROOT / "models" / "reranker-minilm-l6"


def test_the_onnx_pin_is_the_frozen_file_size_and_hash_prefix():
    size, digest = brm.FILES["onnx/model_quint8_avx2.onnx"]
    assert size == 23_200_716 and digest.startswith("c80a8b34256ea453") and len(digest) == 64


@pytest.mark.skipif(not LOCAL.is_dir(), reason="local model files not present")
def test_every_pinned_hash_matches_the_local_copy_the_bench_ran_with():
    for name, (_size, digest) in brm.FILES.items():
        assert hashlib.sha256((LOCAL / name).read_bytes()).hexdigest() == digest, name


def test_the_dockerfile_runs_the_verifying_script_in_the_model_stage_to_the_path_the_settings_expect():
    model = DOCKERFILE.split(" AS model", 1)[1].split(" AS web", 1)[0]
    assert "COPY scripts/build_reranker_model.py scripts/" in model
    assert "RUN python scripts/build_reranker_model.py --out /models/reranker-minilm-l6" in model
    between = model[model.index("COPY scripts/build_reranker_model.py") : model.index("RUN python scripts/build_reranker_model.py")]
    assert "pip install" not in between and "pip install" not in model.split("RUN python scripts/build_reranker_model.py")[-1]
    assert model.index("embed_rss.py /models/embed_rss.py") < model.index("build_reranker_model.py --out")  # embedder layers untouched


def test_fly_toml_ships_the_flags_off_and_points_at_the_models_directory():
    env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]
    assert env["RETRIEVAL_FUSION"] == "false" and env["RETRIEVAL_RERANK"] == "none"
    assert env["RETRIEVAL_RERANK_MODEL_DIR"] == "/srv/models"


def fake(files):
    return lambda url: files[url.split(f"/resolve/{brm.REVISION}/", 1)[1]]


def good_files():
    return {name: b"x" * (size or 3) for name, (size, _d) in brm.FILES.items()}


def test_build_writes_nothing_and_fails_on_a_hash_mismatch(tmp_path):
    with pytest.raises(ValueError, match="SHA-256|bytes"):
        brm.build(tmp_path / "out", fetch=fake(good_files()))
    assert not (tmp_path / "out").exists()


def test_build_writes_the_layout_when_every_file_verifies(tmp_path, monkeypatch):
    files = {"onnx/model_quint8_avx2.onnx": b"a", "tokenizer.json": b"b", "config.json": b"c"}
    monkeypatch.setattr(brm, "FILES", {n: (None, hashlib.sha256(d).hexdigest()) for n, d in files.items()})
    brm.build(tmp_path / "out", fetch=fake(files))
    assert (tmp_path / "out" / "onnx" / "model_quint8_avx2.onnx").read_bytes() == b"a"
    assert (tmp_path / "out" / "tokenizer.json").read_bytes() == b"b"


def test_one_bad_file_among_good_ones_writes_none(tmp_path, monkeypatch):
    files = {"onnx/model_quint8_avx2.onnx": b"a", "tokenizer.json": b"b", "config.json": b"c"}
    monkeypatch.setattr(brm, "FILES", {n: (None, hashlib.sha256(d).hexdigest()) for n, d in files.items()})
    files["config.json"] = b"tampered"
    with pytest.raises(ValueError):
        brm.build(tmp_path / "out", fetch=fake(files))
    assert not (tmp_path / "out").exists()


def test_the_fetcher_refuses_a_host_other_than_huggingface():
    with pytest.raises(ValueError):
        brm._http_get("https://example.com/model.onnx")


def test_a_redirect_to_a_host_outside_huggingface_is_refused():
    handler = brm._SafeRedirect()
    req = brm.urllib.request.Request("https://huggingface.co/x")
    for bad in ("https://example.com/f", "http://cdn-lfs.hf.co/f", "https://evilhf.co/f"):
        with pytest.raises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, bad)
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://cdn-lfs.hf.co/f") is not None


def test_main_returns_nonzero_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brm, "_http_get", lambda url: b"bad")
    assert brm.main(["--out", str(tmp_path / "o")]) == 1
    assert "failed" in capsys.readouterr().err


def test_the_reranker_download_is_pinned_to_a_commit_sha():
    assert re.fullmatch(r"[0-9a-f]{40}", brm.REVISION)
