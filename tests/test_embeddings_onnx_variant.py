"""semigraph.embeddings_onnx: which model variant is loaded (8-bit or pre-dequantized fp32) and what proved it.

The backend loads either model by path (external data included, ONNX Runtime resolves the sidecar itself) and exposes
``variant`` and ``fidelity``, the compact summary of the ``<model>.fidelity.json`` that ``scripts/predequantize_embedder.py``
writes beside the fp32 model. The shipped 8-bit model has no such file and is told apart by its name. The reading helpers
need only the standard library (the serving image has no ``onnx`` package), so those tests run everywhere; the ones that
load a real model skip without the dev-only ``onnx``.
"""

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from semigraph import embeddings_onnx as eo

REPORT = {"variant": "fp32", "format": 1, "model": "model_fp32.onnx", "data": "model_fp32.onnx.data", "bits": 8,
          "block_size": 128, "dequantized_nodes": 196, "nodes_bit_exact": 196, "max_node_abs_diff": 0.0,
          "source": {"model": "model_q8.onnx", "sha256": "ab" * 32, "files": {"model_q8.onnx": "cd" * 32}},
          "gate": {"questions": 53, "cosine_mean": 0.9999999999, "cosine_min": 0.9999999998, "passed": True},
          "onnxruntime": "1.29.0", "onnx": "1.22.0", "numpy": "2.5.0", "machine": "AMD64"}


def write_report(directory: Path, report, name: str = "model_fp32.fidelity.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
    return directory / name


# ---------------------------------------------------------------- the summary shown on /healthz

def test_the_summary_is_the_few_numbers_that_prove_the_build_and_nothing_else():
    assert eo.fidelity_summary(REPORT) == {"nodes": 196, "max_node_abs_diff": 0.0, "cosine_min": 0.9999999998,
                                           "cosine_mean": 0.9999999999, "source_sha256": "ab" * 8}


@pytest.mark.parametrize("report", [{}, {"gate": "x", "source": 3}, {"dequantized_nodes": "many", "gate": {"cosine_min": True}}],
                         ids=["empty", "wrong-containers", "wrong-types"])
def test_a_summary_of_a_malformed_report_has_the_same_keys_with_none(report):
    assert eo.fidelity_summary(report) == {"nodes": None, "max_node_abs_diff": None, "cosine_min": None,
                                           "cosine_mean": None, "source_sha256": None}


# ---------------------------------------------------------------- reading the fidelity file beside a model

def test_the_fidelity_file_sits_beside_the_model_and_is_named_after_it(tmp_path):
    assert eo.fidelity_path(tmp_path / "x" / "model_fp32.onnx") == tmp_path / "x" / "model_fp32.fidelity.json"


def test_a_valid_fidelity_file_is_returned_as_written(tmp_path):
    write_report(tmp_path, REPORT)

    assert eo.read_fidelity(tmp_path / "model_fp32.onnx") == REPORT


def test_a_missing_fidelity_file_is_normal_and_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="semigraph.embeddings.onnx"):
        assert eo.read_fidelity(tmp_path / "model_q8.onnx") is None
    assert not caplog.records


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", json.dumps({**REPORT, "variant": "int4"}), json.dumps({})],
                         ids=["malformed", "not-an-object", "unknown-variant", "no-variant"])
def test_an_unusable_fidelity_file_is_ignored_with_a_warning_never_an_error(tmp_path, caplog, content):
    write_report(tmp_path, content)

    with caplog.at_level(logging.WARNING, logger="semigraph.embeddings.onnx"):
        assert eo.read_fidelity(tmp_path / "model_fp32.onnx") is None
    assert any("fidelity" in r.getMessage() for r in caplog.records)


def test_a_fidelity_file_that_cannot_be_read_is_ignored_with_a_warning(tmp_path, caplog):
    (tmp_path / "model_fp32.fidelity.json").mkdir()          # a directory where the file should be: OSError on read

    with caplog.at_level(logging.WARNING, logger="semigraph.embeddings.onnx"):
        assert eo.read_fidelity(tmp_path / "model_fp32.onnx") is None
    assert any("fidelity" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- the variant

def test_the_variant_comes_from_the_fidelity_file_when_there_is_one(tmp_path):
    assert eo.variant_of(tmp_path / "model_fp32.onnx", REPORT) == "fp32"


@pytest.mark.parametrize("name, variant", [("model_q8.onnx", "q8"), ("model_q8.unpatched.onnx", "unknown"),
                                           ("model.onnx", "unknown"), ("model_fp32.onnx", "unknown")])
def test_without_a_fidelity_file_only_the_shipped_8_bit_name_is_trusted(tmp_path, name, variant):
    assert eo.variant_of(tmp_path / name, None) == variant


# ---------------------------------------------------------------- the backend

def test_a_backend_that_was_not_initialised_still_reports_an_unknown_variant():
    backend = object.__new__(eo.OnnxBackend)                 # how the token-count tests build one

    assert (backend.variant, backend.fidelity) == ("unknown", None)


def build_pair(tmp_path: Path):
    """(8-bit model, pre-dequantized model) of the same synthetic weights, built by the real script."""
    pytest.importorskip("onnx")
    from test_predequantize_embedder import pq, write_nbits_embedder

    src = write_nbits_embedder(tmp_path / "src")
    out = tmp_path / "out"
    pq.predequantize(src.parent, out)
    return src, out / "model_fp32.onnx"


def test_the_8_bit_model_loads_as_q8_without_a_fidelity_summary(tmp_path):
    src, _ = build_pair(tmp_path)

    backend = eo.OnnxBackend(src)

    assert (backend.variant, backend.fidelity) == ("q8", None) and backend.name == "onnx:model_q8.onnx"


def test_the_pre_dequantized_model_loads_by_path_with_its_external_data_and_reports_its_proof(tmp_path):
    src, fp32 = build_pair(tmp_path)

    backend = eo.OnnxBackend(fp32)
    reference = eo.OnnxBackend(src)

    assert backend.variant == "fp32" and backend.name == "onnx:model_fp32.onnx"
    assert backend.fidelity["nodes"] == 2 and backend.fidelity["max_node_abs_diff"] == 0.0
    assert len(backend.fidelity["source_sha256"]) == 16
    question = "Which foundries does AMD rely on?"
    vector, expected = np.array(backend.encode_query(question)), np.array(reference.encode_query(question))
    assert abs(np.linalg.norm(vector) - 1.0) < 1e-5
    np.testing.assert_allclose(vector, expected, atol=1e-5)


def test_the_pre_dequantized_model_does_not_need_the_source_any_more(tmp_path):
    import shutil

    src, fp32 = build_pair(tmp_path)
    shutil.rmtree(src.parent)

    assert eo.OnnxBackend(fp32).variant == "fp32"


def test_a_missing_external_data_file_is_named_instead_of_an_onnxruntime_error(tmp_path):
    _, fp32 = build_pair(tmp_path)
    fp32.with_name("model_fp32.onnx.data").unlink()

    with pytest.raises(FileNotFoundError, match=r"model_fp32\.onnx\.data"):
        eo.OnnxBackend(fp32)


def test_the_declared_data_file_is_only_ever_a_name_in_the_models_directory(tmp_path):
    _, fp32 = build_pair(tmp_path)
    report = json.loads(fp32.with_name("model_fp32.fidelity.json").read_text(encoding="utf-8"))
    (fp32.parent / "elsewhere").mkdir()
    (fp32.parent / "elsewhere" / "model_fp32.onnx.data").write_bytes(b"x")
    report["data"] = "elsewhere/model_fp32.onnx.data"
    fp32.with_name("model_fp32.fidelity.json").write_text(json.dumps(report), encoding="utf-8")
    fp32.with_name("model_fp32.onnx.data").unlink()

    with pytest.raises(FileNotFoundError, match=r"model_fp32\.onnx\.data"):
        eo.OnnxBackend(fp32)
