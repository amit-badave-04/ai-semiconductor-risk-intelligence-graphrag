"""Provenance of the 'patched' model used by parity_prod_prompt.json, fp32_reference.json and the timing file.

Scratch-only: copy the repo's shipped model_q8.onnx (same quantized weights, no re-quantization), set
accuracy_level=4 on its 196 MatMulNBits nodes with the builder's own functions, then run the builder's in-build gate
(verify_patched) on the copy. Usage, from the repo root: python docs/v2/research/m5-spikes/make_patched_copy.py <out_dir>
The gate's output for the run that produced the evidence is patch_gate_report.json beside this file."""
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("b", REPO / "scripts" / "build_onnx_embedder.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
import onnx  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
src = REPO / "models" / "qwen3-embedding-0.6b-q8" / "model_q8.onnx"
t = time.time()
proto = b.load_graph(src)
print("nodes", b.count_matmul_nbits(proto), "missing level 4 before:", b.count_nodes_missing_accuracy_level(proto, 4), flush=True)
print("changed:", b.set_accuracy_level(proto, 4), flush=True)
onnx.save_model(proto, str(out / "model_q8.onnx"))
shutil.copy(src.parent / "tokenizer.json", out / "tokenizer.json")
print(f"patched copy written in {time.time() - t:.0f}s", flush=True)
t = time.time()
report = b.verify_patched(out, 4)
print("gate report:", json.dumps(report), flush=True)
print(f"gate took {time.time() - t:.0f}s; files left:", sorted(p.name for p in out.iterdir()), flush=True)
