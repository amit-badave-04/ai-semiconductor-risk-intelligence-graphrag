"""Build the torch-free query embedder: 8-bit weight-only ONNX of Qwen3-Embedding-0.6B.

    python scripts/build_onnx_embedder.py [--out models/qwen3-embedding-0.6b-q8]
                                          [--bits 8] [--block-size 128] [--verify]

1. Downloads the public fp32 ONNX export (onnx-community/Qwen3-Embedding-0.6B-ONNX:
   onnx/model.onnx + onnx/model.onnx_data, ~2.4 GB) and its tokenizer.json.
2. Applies MatMulNBits block-wise weight-only quantization (onnxruntime's own
   quantizer; activations stay fp32) -> a single self-contained model_q8.onnx.
3. --verify (default on when the local sentence-transformers model is cached):
   embeds the 20 gold-benchmark questions with BOTH backends and refuses to
   keep the file unless cosine(min) >= 0.98. The result is written next to the
   model as fidelity.json so the number ships with the artifact.

Run in the Docker build (deploy/Dockerfile) and locally. Idempotent: an
existing model_q8.onnx is reused unless --force.
"""

import argparse
import gc
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = "onnx-community/Qwen3-Embedding-0.6B-ONNX"
FP32 = ("onnx/model.onnx", "onnx/model.onnx_data")
COS_MIN = 0.98


def download(repo: str, filename: str) -> Path:
    from huggingface_hub import hf_hub_download
    try:
        return Path(hf_hub_download(repo, filename, local_files_only=True))
    except Exception:
        return Path(hf_hub_download(repo, filename))


def quantize(fp32_model: Path, fp32_data: Path, out_model: Path, bits: int, block_size: int) -> None:
    import onnx
    from onnxruntime.quantization.matmul_nbits_quantizer import (
        DefaultWeightOnlyQuantConfig, MatMulNBitsQuantizer)

    # The Hugging Face cache stores files as symlinks (on Linux); onnx refuses to
    # read external weights through a symlink, so stage real copies first.
    work = Path(tempfile.mkdtemp(prefix="qwen3-fp32-"))
    for src, name in ((fp32_model, "model.onnx"), (fp32_data, "model.onnx_data")):
        shutil.copyfile(Path(src).resolve(), work / name)
    print(f"loading {work / 'model.onnx'} ...", flush=True)
    model = onnx.load(str(work / "model.onnx"), load_external_data=True)
    shutil.rmtree(work, ignore_errors=True)
    config = DefaultWeightOnlyQuantConfig(block_size=block_size, is_symmetric=True, bits=bits)
    quant = MatMulNBitsQuantizer(model, algo_config=config)
    t = time.time()
    quant.process()
    print(f"quantized in {time.time() - t:.0f}s -> saving {out_model}", flush=True)
    out_model.parent.mkdir(parents=True, exist_ok=True)
    # Memory-lean save: the weights go to a sidecar file (model_q8.onnx_data) so the
    # ~1.1 GB proto is never serialized as one in-memory bytes object — the Fly remote
    # builder OOM-killed the single-file save. onnxruntime resolves the sidecar itself.
    quantized = quant.model.model
    del model, quant
    gc.collect()
    onnx.save_model(quantized, str(out_model), save_as_external_data=True,
                    all_tensors_to_one_file=True, location=out_model.name + "_data",
                    size_threshold=1024)


def verify(out_dir: Path) -> dict:
    """Fidelity of the ONNX backend vs sentence-transformers on the benchmark questions."""
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from semigraph.artifacts import load_benchmark
    from semigraph.embeddings import Embedder

    questions = [b["q"] for b in load_benchmark()]
    ref = Embedder(backend="local")
    ref_vecs = np.array([ref.encode_query(q) for q in questions], dtype=np.float32)
    from semigraph.embeddings_onnx import OnnxBackend
    onnx_backend = OnnxBackend(out_dir / "model_q8.onnx", out_dir / "tokenizer.json")
    vecs = np.array([onnx_backend.encode_query(q) for q in questions], dtype=np.float32)
    cos = (vecs * ref_vecs).sum(axis=1)
    report = {"questions": len(questions), "cosine_min": float(cos.min()),
              "cosine_mean": float(cos.mean()), "threshold": COS_MIN,
              "passed": bool(cos.min() >= COS_MIN)}
    print("fidelity:", json.dumps(report), flush=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="models/qwen3-embedding-0.6b-q8")
    ap.add_argument("--bits", type=int, default=8)
    ap.add_argument("--block-size", type=int, default=128)
    ap.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_model = out_dir / "model_q8.onnx"
    if out_model.exists() and not args.force:
        print(f"{out_model} exists — skipping quantization (use --force to rebuild)")
    else:
        fp32 = download(REPO, FP32[0])
        data = download(REPO, FP32[1])  # external weights file
        quantize(fp32, data, out_model, args.bits, args.block_size)
    shutil.copy(download(REPO, "tokenizer.json"), out_dir / "tokenizer.json")
    total = sum(f.stat().st_size for f in out_dir.glob("model_q8.onnx*"))
    print(f"model size: {total / 1e6:.0f} MB ({', '.join(f.name for f in sorted(out_dir.glob('model_q8.onnx*')))})")

    if args.verify:
        report = verify(out_dir)
        (out_dir / "fidelity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if not report["passed"]:
            out_model.unlink()
            sys.exit(f"FIDELITY FAILED (cosine min {report['cosine_min']:.4f} < {COS_MIN}) — model removed")
    print("done:", out_dir)


if __name__ == "__main__":
    main()
